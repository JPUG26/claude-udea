#!/usr/bin/env python3
"""
Genera transcripciones locales (faster-whisper) para grabaciones que no
tienen transcript de Zoom (.transcript.vtt) ni subtítulos (.cc.vtt).

Flujo por grabación:
  1. Descarga el video de Zoom con yt-dlp (formato 'view', el más liviano).
    2. Extrae audio 16 kHz mono con ffmpeg; conserva los videos ya organizados.
  3. Transcribe con faster-whisper (small, int8, VAD) y escribe
     la transcripción en downloads/<asignatura>/transcripts/whisper/.
  4. Borra el audio y regenera downloads/transcripts/ + index.json
     con copy_transcripts() (el mismo paso del proceso normal).

Es idempotente: las grabaciones que ya tienen transcript se saltan,
así que se puede relanzar tras una interrupción o tras nuevas descargas.

Uso:
  .venv/bin/python transcribe_missing.py [--work-dir /home/gabo/claude-udea] [--check]
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

from claude_udea.download import (  # noqa: E402
    _build_rec_id_map,
    _class_number_from_filename,
    _name_conflicts_with,
    _recording_alias_ids,
    _split_part,
    copy_transcripts,
    recording_filename,
)

_SKIP_PARTS = {".claude-udea-backup", "sin-clasificar", "incompletos"}


def _course_options(work_dir: Path) -> dict:
    """Opciones por curso (fabrica_meeting_ids) desde config.json."""
    config_path = work_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        slug: {"fabrica_meeting_ids": info.get("fabrica_meeting_ids", [])}
        for slug, info in data.get("courses", {}).items()
    }


def _in_backup(path: Path, course_dir: Path) -> bool:
    try:
        return any(part in _SKIP_PARTS for part in path.relative_to(course_dir).parts)
    except ValueError:
        return False


def _category_prefix(meta: dict | None) -> str:
    return "fabrica escuela - clase #" if (meta or {}).get("class_category") == "fabrica_escuela" else "clase #"


def _file_matches(path: Path, alias_ids: list[str], date: str, prefix: str, number) -> bool:
    """Empareja un archivo con su grabación por alias de ID o por categoría+número+fecha."""
    name = path.name
    if any(f"[{alias}" in name for alias in alias_ids):
        return True
    if not date or f" - {date}" not in name or not name.casefold().startswith(prefix):
        return False
    if number is not None:
        return _class_number_from_filename(name) == number
    return True

WHISPER_MARK = "generada localmente con faster-whisper"


@dataclass
class WhisperSettings:
    """Parámetros de faster-whisper, configurables por flags.

    Valores por defecto orientados a un buen equilibrio calidad/velocidad en CPU
    (p. ej. Intel i9 + 24 GB): modelo `medium` con `int8` y beam=1, que produce
    resultados en tiempo razonable. Súbelos para más calidad (más lento).
    La GPU Intel (Iris Xe) no la usa CTranslate2; corre en CPU.
    """

    model: str = "medium"
    compute_type: str = "int8"
    device: str = "cpu"
    cpu_threads: int = 8
    beam_size: int = 1
    language: str = "es"
    initial_prompt: str = (
        "Clase de Arquitectura de Software y Fábrica Escuela de Ingeniería de "
        "Sistemas de la Universidad de Antioquia."
    )
    condition_on_previous_text: bool = False
    # Transcribir por bloques: acota la RAM y permite reanudar tras un crash.
    chunk_seconds: int = 1200


def settings_from_args(argv: list[str]) -> WhisperSettings:
    """Construye WhisperSettings a partir de una lista de argumentos.

    Útil para el comando `claude_udea`, que no usa argparse para estas flags.
    """
    flag_map = {
        "--whisper-model": ("model", str),
        "--whisper-compute-type": ("compute_type", str),
        "--whisper-device": ("device", str),
        "--whisper-cpu-threads": ("cpu_threads", int),
        "--whisper-beam-size": ("beam_size", int),
        "--whisper-language": ("language", str),
        "--whisper-initial-prompt": ("initial_prompt", str),
        "--whisper-chunk-seconds": ("chunk_seconds", int),
    }
    values = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in flag_map:
            attribute, cast = flag_map[token]
            if index + 1 < len(argv):
                try:
                    values[attribute] = cast(argv[index + 1])
                except ValueError:
                    pass
                index += 2
                continue
        elif token == "--whisper-no-conditioning":
            values["condition_on_previous_text"] = False
        index += 1
    return WhisperSettings(**values)


def log(msg):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def has_transcript(course_dir: Path, rec_id: str, rec_info: dict | None = None,
                   meta: dict | None = None, video: Path | None = None) -> bool:
    return any(
        _video_slot(transcript) == _video_slot(video)
        and (not _is_whisper_file(transcript) or _is_correct_whisper_transcript(transcript))
        for transcript in _recording_transcripts(course_dir, rec_id, rec_info or {}, meta)
    )


def find_missing(work_dir: Path, course_slugs: list[str] | None = None,
                 class_numbers: set[int] | list[int] | None = None):
    recordings_path = work_dir / "recordings.json"
    with open(recordings_path, encoding="utf-8") as f:
        recordings = json.load(f)

    download_dir = work_dir / "downloads"
    id_map = _build_rec_id_map(recordings, _course_options(work_dir))
    missing = []
    for slug, course in recordings.items():
        if course_slugs and slug not in course_slugs:
            continue
        course_dir = download_dir / slug
        for rec_id, info in course.get("recordings", {}).items():
            meta = id_map[rec_id]
            if class_numbers and meta.get("class_number") not in class_numbers:
                continue
            for video in _existing_videos(course_dir, rec_id, info, meta) or [None]:
                if not has_transcript(course_dir, rec_id, info, meta, video):
                    missing.append({
                        "slug": slug,
                        "rec_id": rec_id,
                        "url": info.get("url"),
                        "title": _whisper_destination(course_dir, meta, video).name,
                        "duration": info.get("duration_minutes", 0),
                    })
    # Cortas primero: resultados útiles cuanto antes
    missing.sort(key=lambda r: r["duration"])
    return recordings, missing


def download_video(rec, cache_dir: Path) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(cache_dir / "%(title)s [%(id)s].%(ext)s")
    audio_video_exts = {
        ".m4a", ".mp3", ".opus", ".ogg", ".aac", ".flac", ".wav",
        ".mp4", ".mkv", ".webm", ".mov",
    }
    # Preferir el mejor audio (mejor transcripción); el video liviano es el respaldo.
    for fmt in (["-f", "bestaudio/best"], ["-f", "view"], []):
        cmd = [sys.executable, "-m", "yt_dlp", "--no-update", "-o", out_tpl,
               "--no-overwrites", "--no-playlist", "--retries", "3",
               "--fragment-retries", "3", *fmt, rec["url"]]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            continue
        matches = [
            path for path in cache_dir.glob(f"*{rec['rec_id']}*")
            if path.suffix.lower() in audio_video_exts
        ]
        if result.returncode == 0 and matches:
            return matches[0]
    return None


def extract_audio(video: Path, remove_video: bool = True) -> Path | None:
    wav = video.with_suffix(".wav")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000", str(wav)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if remove_video and result.returncode == 0 and wav.exists():
        video.unlink(missing_ok=True)
    return wav if result.returncode == 0 and wav.exists() else None


def wav_duration(wav: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(wav)],
        capture_output=True, text=True, timeout=60)
    return float(result.stdout.strip())


def transcribe(model, wav: Path, dest_vtt: Path, parts_dir: Path,
               settings: "WhisperSettings | None" = None):
    """Transcribe por bloques con resultados parciales en parts_dir; si un bloque
    ya tiene su .json, se salta (reanudación). Los parámetros de calidad vienen de
    `settings` (modelo, beam, prompt, condición de contexto)."""
    settings = settings or WhisperSettings()
    total = wav_duration(wav)
    n_chunks = max(1, math.ceil(total / settings.chunk_seconds))
    parts_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n_chunks):
        part_json = parts_dir / f"part{i:03d}.json"
        if part_json.exists():
            continue
        offset = i * settings.chunk_seconds
        chunk_wav = parts_dir / "chunk.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(offset),
             "-t", str(settings.chunk_seconds), "-i", str(wav),
             "-c:a", "pcm_s16le", str(chunk_wav)],
            capture_output=True, timeout=600, check=True)
        segments, _info = model.transcribe(
            str(chunk_wav),
            language=settings.language,
            vad_filter=True,
            beam_size=settings.beam_size,
            initial_prompt=settings.initial_prompt or None,
            condition_on_previous_text=settings.condition_on_previous_text,
        )
        data = [{"start": s.start + offset, "end": s.end + offset,
                 "text": s.text.strip()} for s in segments if s.text.strip()]
        chunk_wav.unlink(missing_ok=True)
        tmp = part_json.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.rename(part_json)  # atómico: nunca queda un parcial corrupto
        log(f"    bloque {i + 1}/{n_chunks} listo ({len(data)} segmentos)")

    cues = []
    for part_json in sorted(parts_dir.glob("part*.json")):
        cues.extend(json.loads(part_json.read_text(encoding="utf-8")))

    dest_vtt.parent.mkdir(parents=True, exist_ok=True)
    temporary_vtt = dest_vtt.with_name(f".whisper-{uuid.uuid4().hex}.part")
    try:
        with open(temporary_vtt, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\nNOTE\nTranscripción generada localmente con "
                    f"faster-whisper ({settings.model}, {settings.compute_type})\n\n")
            for i, seg in enumerate(cues, 1):
                f.write(f"{i}\n{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}\n"
                        f"{seg['text']}\n\n")
        temporary_vtt.replace(dest_vtt)
    finally:
        temporary_vtt.unlink(missing_ok=True)
    shutil.rmtree(parts_dir, ignore_errors=True)


def _recording_transcripts(course_dir: Path, rec_id: str, rec_info: dict, meta: dict | None = None) -> list[Path]:
    """VTTs de una grabación: por organized_files, por alias de ID y por categoría+fecha.

    Las clases ya organizadas tienen nombres limpios (sin rec_id), así que se
    emparejan además por prefijo de categoría y fecha. Se excluye el respaldo.
    """
    meta = meta or {}
    alias_ids = _recording_alias_ids(rec_id, rec_info)
    date = (rec_info.get("start_date", "") or "")[:10]
    prefix = _category_prefix(meta)
    number = meta.get("class_number")

    candidates = set()
    # organized_files es la asociación propia de cada grabación, salvo rutas
    # obsoletas cuyo nombre trae la fecha/categoría de otra clase.
    for relative_path in rec_info.get("organized_files", []):
        path = course_dir / relative_path
        if meta and _name_conflicts_with(path.name, meta):
            continue
        if path.is_file() and not _in_backup(path, course_dir):
            candidates.add(path)
    # Además, cualquier VTT con el alias de ID o categoría+número+fecha.
    for path in course_dir.rglob("*.vtt"):
        if _in_backup(path, course_dir) or (meta and _name_conflicts_with(path.name, meta)):
            continue
        if _file_matches(path, alias_ids, date, prefix, number):
            candidates.add(path)
    return [
        path for path in candidates
        if ".transcript." in path.name.lower() or ".cc." in path.name.lower()
    ]


def _is_whisper_file(transcript: Path) -> bool:
    try:
        return WHISPER_MARK in transcript.read_text(encoding="utf-8", errors="replace")[:500]
    except OSError:
        return False


def _is_correct_whisper_transcript(transcript: Path) -> bool:
    """Correcta = cabecera de faster-whisper y al menos un cue con tiempos.
    Un VTT solo con cabecera (transcripción vacía o truncada) se vuelve a procesar."""
    if not _is_whisper_file(transcript):
        return False
    try:
        return "-->" in transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _has_whisper_transcript(
    course_dir: Path,
    rec_id: str,
    rec_info: dict,
    expected_transcript: Path | None = None,
    meta: dict | None = None,
) -> bool:
    candidates = _recording_transcripts(course_dir, rec_id, rec_info, meta)
    if expected_transcript and expected_transcript.is_file():
        candidates.append(expected_transcript)
    return any(
        _video_slot(transcript) == _video_slot(expected_transcript)
        and _is_correct_whisper_transcript(transcript)
        for transcript in candidates
    )


def _existing_videos(course_dir: Path, rec_id: str, rec_info: dict, meta: dict | None = None) -> list[Path]:
    """Videos por slot: organized_files, alias de ID o categoría+fecha.

    El fallback por categoría+fecha evita transcribir el video equivocado cuando
    las clases ya tienen nombres limpios (sin rec_id). Se excluye el respaldo.
    """
    meta = meta or {}
    video_extensions = {".mp4", ".mkv", ".webm", ".mov"}
    alias_ids = _recording_alias_ids(rec_id, rec_info)
    date = (rec_info.get("start_date", "") or "")[:10]
    prefix = _category_prefix(meta)
    number = meta.get("class_number")

    organized = []
    for relative_path in rec_info.get("organized_files", []):
        candidate = course_dir / relative_path
        if meta and _name_conflicts_with(candidate.name, meta):
            continue
        if candidate.is_file() and candidate.suffix.lower() in video_extensions and not _in_backup(candidate, course_dir):
            organized.append(candidate)
    alias_candidates = []
    category_candidates = []
    for candidate in course_dir.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in video_extensions:
            continue
        if _in_backup(candidate, course_dir) or (meta and _name_conflicts_with(candidate.name, meta)):
            continue
        if any(f"[{alias}" in candidate.name for alias in alias_ids):
            alias_candidates.append(candidate)
        elif _file_matches(candidate, alias_ids, date, prefix, number):
            category_candidates.append(candidate)
    # Prioridad: alias de ID (inequívoco), luego categoría+número+fecha (nombres
    # limpios) y por último organized_files (asociación propia).
    slots = {}
    for group in (alias_candidates, category_candidates, organized):
        for path in sorted(group):
            slots.setdefault(_video_slot(path), path)
    return [slots[slot] for slot in sorted(slots, key=lambda slot: slot or 0)]


def _video_slot(path: Path | None) -> int | None:
    parts = _split_part(path.name)[1] if path is not None else ()
    return parts[-1] if parts else None


def _whisper_destination(course_dir: Path, meta: dict, video: Path | None) -> Path:
    """El whisper sigue el slot de su video: '... - Parte N.whisper.transcript.vtt'."""
    part = _video_slot(video)
    label = f" - Parte {part}" if part else ""
    return (
        course_dir / "transcripts" / "whisper"
        / f"{recording_filename(meta)}{label}.whisper.transcript.vtt"
    )


def has_zoom_transcripts(work_dir: Path, course_slugs: list[str] | None = None) -> bool:
    recordings_path = work_dir / "recordings.json"
    recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
    download_dir = work_dir / "downloads"
    id_map = _build_rec_id_map(recordings, _course_options(work_dir))
    for slug in course_slugs or list(recordings):
        course = recordings.get(slug, {})
        course_dir = download_dir / slug
        for rec_id, rec_info in course.get("recordings", {}).items():
            for transcript in _recording_transcripts(course_dir, rec_id, rec_info, id_map.get(rec_id)):
                try:
                    is_whisper = "generada localmente con faster-whisper" in transcript.read_text(
                        encoding="utf-8", errors="replace"
                    )[:500]
                except OSError:
                    continue
                if not is_whisper:
                    return True
    return False


def transcribe_all(
    work_dir: Path,
    course_slugs: list[str] | None = None,
    keep_zoom_transcripts: bool = True,
    settings: "WhisperSettings | None" = None,
    class_numbers: "set[int] | list[int] | None" = None,
    *,
    only_missing: bool = False,
    check_only: bool = False,
) -> tuple[int, int]:
    """Transcribe cada slot de video seleccionado sin transcript Whisper previo.

    `course_slugs` limita a cursos concretos; `class_numbers` limita a clases concretas.
    `only_missing` conserva los slots con subtitulos Zoom; `check_only` solo lista.
    """
    settings = settings or WhisperSettings()
    class_numbers = set(class_numbers) if class_numbers else None
    recordings_path = work_dir / "recordings.json"
    recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
    selected_courses = course_slugs or list(recordings)
    download_dir = work_dir / "downloads"
    cache_dir = work_dir / ".media-cache"
    id_map = _build_rec_id_map(recordings, _course_options(work_dir))

    pending = []
    already_done = 0
    for slug in selected_courses:
        course = recordings.get(slug)
        if not course:
            continue
        course_dir = download_dir / slug
        for rec_id, rec_info in course.get("recordings", {}).items():
            metadata = id_map[rec_id]
            if class_numbers is not None and metadata.get("class_number") not in class_numbers:
                continue
            for video in _existing_videos(course_dir, rec_id, rec_info, metadata) or [None]:
                destination = _whisper_destination(course_dir, metadata, video)
                done = (
                    has_transcript(course_dir, rec_id, rec_info, metadata, video)
                    if only_missing else
                    _has_whisper_transcript(course_dir, rec_id, rec_info, destination, metadata)
                )
                if done:
                    already_done += 1
                elif video is not None or rec_info.get("url"):
                    pending.append((slug, rec_id, rec_info, video, destination))

    log(f"{already_done} slots de video ya transcritos correctamente; se omiten.")
    if not pending:
        log("Todas las grabaciones seleccionadas ya tienen transcripción de faster-whisper.")
        return 0, 0

    if check_only:
        for slug, _rec_id, _rec_info, _video, destination in pending:
            log(f"  - [{slug}] {destination.name}")
        return 0, 0

    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            "Falta faster-whisper. Instálalo con: pip install faster-whisper"
        ) from error

    log(f"{len(pending)} slots de video pendientes de transcripción local.")
    log(f"Cargando modelo {settings.model} ({settings.compute_type}, "
        f"device={settings.device}); el primer inicio descarga el modelo.")
    model = WhisperModel(
        settings.model,
        device=settings.device,
        compute_type=settings.compute_type,
        cpu_threads=settings.cpu_threads,
    )
    ok = failed = 0

    for slug, rec_id, rec_info, video, destination in pending:
        title = rec_info.get("title", slug)
        duration = rec_info.get("duration_minutes", 0)
        label = f"[{slug}] {title} ({duration} min): {destination.name}"
        course_dir = download_dir / slug
        course_dir.mkdir(parents=True, exist_ok=True)
        temporary_video = video is None
        cache_key = hashlib.sha256(
            f"{slug}\0{rec_id}\0{destination.name}".encode()
        ).hexdigest()[:16]
        wav = cache_dir / f"{slug}-{cache_key}.wav"
        parts_dir = cache_dir / f"parts-{cache_key}"
        if temporary_video and not wav.exists():
            log(f"Descargando video para transcribir: {label}")
            video = download_video({"url": rec_info["url"], "rec_id": rec_id}, cache_dir)
        if not video and not wav.exists():
            log(f"  ERROR descargando {label}")
            failed += 1
            continue

        if not wav.exists():
            log(f"  Extrayendo audio: {video.name}")
            extracted = extract_audio(video, remove_video=temporary_video)
            if not extracted:
                log(f"  ERROR extrayendo audio de {label}")
                failed += 1
                continue
            if extracted != wav:
                extracted.replace(wav)

        destination.parent.mkdir(parents=True, exist_ok=True)
        log(f"  Transcribiendo con faster-whisper: {label}")
        try:
            transcribe(model, wav, destination, parts_dir, settings)
        except Exception as error:
            log(f"  ERROR transcribiendo {label}: {error}; se conserva audio para reanudar")
            failed += 1
            continue
        if not _is_correct_whisper_transcript(destination):
            # Sin cues: no se considera correcta; se conserva el audio para reintentar
            # en la próxima ejecución y no se borran los subtítulos de Zoom.
            destination.unlink(missing_ok=True)
            log(f"  ERROR {label}: transcripción vacía; se reintentará en la próxima ejecución")
            failed += 1
            continue
        wav.unlink(missing_ok=True)
        for previous_transcript in _recording_transcripts(course_dir, rec_id, rec_info, id_map[rec_id]):
            if _video_slot(previous_transcript) != _video_slot(destination):
                continue
            is_whisper = _is_whisper_file(previous_transcript)
            if previous_transcript != destination and (is_whisper or not keep_zoom_transcripts):
                previous_transcript.unlink(missing_ok=True)
        ok += 1
        log(f"  OK -> {destination.name}")

    log(f"Transcripción local terminada: {ok} completas, {failed} fallidas.")
    return ok, failed


def main():
    parser = argparse.ArgumentParser()
    default_work_dir = Path("C:/claude-udea") if os.name == "nt" else Path.home() / "claude-udea"
    parser.add_argument("--work-dir", default=str(default_work_dir))
    parser.add_argument("--check", action="store_true",
                        help="solo listar grabaciones sin transcript, no procesar")
    parser.add_argument("--always-whisper", action="store_true",
                        help="transcribe todas las grabaciones sin un VTT local de Whisper previo")
    parser.add_argument("--keep-zoom-transcripts", action="store_true",
                        help="conservar la VTT original de Zoom junto con la de Whisper")
    parser.add_argument("--course", action="append", dest="courses",
                        help="slug del curso; se puede repetir")
    parser.add_argument("--class", action="append", type=int, dest="class_numbers",
                        help="número de clase a transcribir (se puede repetir)")
    default = WhisperSettings()
    parser.add_argument("--whisper-model", default=default.model,
                        help=f"modelo faster-whisper (default: {default.model})")
    parser.add_argument("--whisper-compute-type", default=default.compute_type,
                        help=f"tipo de cómputo (default: {default.compute_type})")
    parser.add_argument("--whisper-device", default=default.device,
                        help=f"cpu o cuda (default: {default.device})")
    parser.add_argument("--whisper-cpu-threads", type=int, default=default.cpu_threads,
                        help=f"hilos de CPU (default: {default.cpu_threads})")
    parser.add_argument("--whisper-beam-size", type=int, default=default.beam_size,
                        help=f"beam size (default: {default.beam_size})")
    parser.add_argument("--whisper-language", default=default.language,
                        help=f"idioma (default: {default.language})")
    parser.add_argument("--whisper-initial-prompt", default=default.initial_prompt,
                        help="texto de contexto para mejorar términos propios")
    parser.add_argument("--whisper-chunk-seconds", type=int, default=default.chunk_seconds,
                        help=f"segundos por bloque (default: {default.chunk_seconds})")
    parser.add_argument("--whisper-no-conditioning", action="store_true",
                        help="desactivar condition_on_previous_text (evita bucles)")
    args = parser.parse_args()

    settings = WhisperSettings(
        model=args.whisper_model,
        compute_type=args.whisper_compute_type,
        device=args.whisper_device,
        cpu_threads=args.whisper_cpu_threads,
        beam_size=args.whisper_beam_size,
        language=args.whisper_language,
        initial_prompt=args.whisper_initial_prompt,
        condition_on_previous_text=not args.whisper_no_conditioning,
        chunk_seconds=args.whisper_chunk_seconds,
    )

    work_dir = Path(args.work_dir)
    if args.always_whisper:
        keep_zoom = args.keep_zoom_transcripts
        if not args.check and not keep_zoom and has_zoom_transcripts(work_dir, args.courses):
            keep_zoom = input("¿Conservar también las transcripciones originales de Zoom? [S/n]: ").strip().lower() not in {"n", "no"}
        ok, failed = transcribe_all(
            work_dir, args.courses, keep_zoom, settings, args.class_numbers,
            check_only=args.check,
        )
        if ok:
            recordings_path = work_dir / "recordings.json"
            recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
            download_dir = work_dir / "downloads"
            from claude_udea.download import rename_downloads
            rename_downloads(download_dir, recordings)
            copy_transcripts(download_dir, recordings)
            recordings_path.write_text(
                json.dumps(recordings, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        sys.exit(1 if failed else 0)
    recordings, missing = find_missing(work_dir, args.courses, args.class_numbers)

    if not missing:
        log("Todas las grabaciones tienen transcripción. Nada que hacer.")
        return

    log(f"{len(missing)} slots de video sin transcripción:")
    for rec in missing:
        log(f"  - [{rec['slug']}] {rec['title']} ({rec['duration']} min)")

    if args.check:
        return

    ok, failed = transcribe_all(
        work_dir, args.courses, True, settings, args.class_numbers, only_missing=True,
    )
    if ok:
        copy_transcripts(work_dir / "downloads", recordings, _course_options(work_dir))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
