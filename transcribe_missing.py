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
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

from claude_udea.download import (  # noqa: E402
    _build_rec_id_map,
    copy_transcripts,
    recording_filename,
)

MODEL_SIZE = "small"          # mejor balance calidad/velocidad en Pi 5 (~1x tiempo real)
COMPUTE_TYPE = "int8"
CPU_THREADS = 4
LANGUAGE = "es"
# Transcribir por bloques: acota la RAM (audios de 4h enteros mataban el proceso
# en la Pi) y permite reanudar desde el último bloque completado tras un crash.
CHUNK_SECONDS = 1200
WHISPER_MARK = "generada localmente con faster-whisper"


def log(msg):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def has_transcript(course_dir: Path, rec_id: str, rec_info: dict | None = None) -> bool:
    return bool(_recording_transcripts(course_dir, rec_id, rec_info or {}))


def find_missing(work_dir: Path):
    recordings_path = work_dir / "recordings.json"
    with open(recordings_path, encoding="utf-8") as f:
        recordings = json.load(f)

    download_dir = work_dir / "downloads"
    missing = []
    for slug, course in recordings.items():
        course_dir = download_dir / slug
        for rec_id, info in course.get("recordings", {}).items():
            if not has_transcript(course_dir, rec_id, info):
                missing.append({
                    "slug": slug,
                    "rec_id": rec_id,
                    "url": info["url"],
                    "title": info.get("title", slug),
                    "duration": info.get("duration_minutes", 0),
                })
    # Cortas primero: resultados útiles cuanto antes
    missing.sort(key=lambda r: r["duration"])
    return recordings, missing


def download_video(rec, cache_dir: Path) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(cache_dir / "%(title)s [%(id)s].%(ext)s")
    for fmt in (["-f", "view"], []):  # 'view' es el stream más liviano; sin -f como fallback
        cmd = [sys.executable, "-m", "yt_dlp", "--no-update", "-o", out_tpl,
               "--no-overwrites", "--no-playlist", "--retries", "3",
               "--fragment-retries", "3", *fmt, rec["url"]]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            continue
        matches = [
            path for path in cache_dir.glob(f"*{rec['rec_id']}*")
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
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


def transcribe(model, wav: Path, dest_vtt: Path, parts_dir: Path):
    """Transcribe por bloques de CHUNK_SECONDS con resultados parciales en
    parts_dir; si un bloque ya tiene su .json, se salta (reanudación)."""
    total = wav_duration(wav)
    n_chunks = max(1, math.ceil(total / CHUNK_SECONDS))
    parts_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n_chunks):
        part_json = parts_dir / f"part{i:03d}.json"
        if part_json.exists():
            continue
        offset = i * CHUNK_SECONDS
        chunk_wav = parts_dir / "chunk.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(offset),
             "-t", str(CHUNK_SECONDS), "-i", str(wav), "-c:a", "pcm_s16le", str(chunk_wav)],
            capture_output=True, timeout=600, check=True)
        segments, _info = model.transcribe(
            str(chunk_wav),
            language=LANGUAGE,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,  # evita bucles de repetición
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
                    f"faster-whisper ({MODEL_SIZE}, {COMPUTE_TYPE})\n\n")
            for i, seg in enumerate(cues, 1):
                f.write(f"{i}\n{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}\n"
                        f"{seg['text']}\n\n")
        temporary_vtt.replace(dest_vtt)
    finally:
        temporary_vtt.unlink(missing_ok=True)
    shutil.rmtree(parts_dir, ignore_errors=True)


def _recording_transcripts(course_dir: Path, rec_id: str, rec_info: dict) -> list[Path]:
    candidates = set()
    for relative_path in rec_info.get("organized_files", []):
        path = course_dir / relative_path
        if path.is_file() and path.suffix.lower() == ".vtt":
            candidates.add(path)
    for path in course_dir.rglob("*.vtt"):
        if rec_id in path.name:
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
) -> bool:
    candidates = _recording_transcripts(course_dir, rec_id, rec_info)
    if expected_transcript and expected_transcript.is_file():
        candidates.append(expected_transcript)
    return any(_is_correct_whisper_transcript(transcript) for transcript in candidates)


def _existing_video(course_dir: Path, rec_id: str, rec_info: dict) -> Path | None:
    video_extensions = {".mp4", ".mkv", ".webm", ".mov"}
    for relative_path in rec_info.get("organized_files", []):
        candidate = course_dir / relative_path
        if candidate.is_file() and candidate.suffix.lower() in video_extensions:
            return candidate
    for candidate in course_dir.rglob("*"):
        if candidate.is_file() and rec_id in candidate.name and candidate.suffix.lower() in video_extensions:
            return candidate
    return None


def has_zoom_transcripts(work_dir: Path, course_slugs: list[str] | None = None) -> bool:
    recordings_path = work_dir / "recordings.json"
    recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
    download_dir = work_dir / "downloads"
    for slug in course_slugs or list(recordings):
        course = recordings.get(slug, {})
        course_dir = download_dir / slug
        for rec_id, rec_info in course.get("recordings", {}).items():
            for transcript in _recording_transcripts(course_dir, rec_id, rec_info):
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
    keep_zoom_transcripts: bool = False,
) -> tuple[int, int]:
    """Transcribe con Whisper todas las grabaciones seleccionadas sin transcript Whisper previo."""
    recordings_path = work_dir / "recordings.json"
    recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
    selected_courses = course_slugs or list(recordings)
    download_dir = work_dir / "downloads"
    cache_dir = work_dir / ".media-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    id_map = _build_rec_id_map(recordings)

    pending = []
    already_done = 0
    for slug in selected_courses:
        course = recordings.get(slug)
        if not course:
            continue
        course_dir = download_dir / slug
        for rec_id, rec_info in course.get("recordings", {}).items():
            metadata = id_map[rec_id]
            expected_transcript = (
                course_dir / "transcripts" / "whisper"
                / recording_filename(metadata, ".whisper.transcript.vtt")
            )
            if not rec_info.get("url"):
                continue
            if _has_whisper_transcript(course_dir, rec_id, rec_info, expected_transcript):
                already_done += 1
            else:
                pending.append((slug, rec_id, rec_info))

    log(f"{already_done} grabaciones ya transcritas correctamente; se omiten.")
    if not pending:
        log("Todas las grabaciones seleccionadas ya tienen transcripción de faster-whisper.")
        return 0, 0

    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            "Falta faster-whisper. Instálalo con: pip install faster-whisper"
        ) from error

    log(f"{len(pending)} grabaciones pendientes de transcripción local.")
    log(f"Cargando modelo {MODEL_SIZE} ({COMPUTE_TYPE}); el primer inicio descarga el modelo.")
    model = WhisperModel(
        MODEL_SIZE,
        device="cpu",
        compute_type=COMPUTE_TYPE,
        cpu_threads=CPU_THREADS,
    )
    ok = failed = 0

    for slug, rec_id, rec_info in pending:
        title = rec_info.get("title", slug)
        duration = rec_info.get("duration_minutes", 0)
        label = f"[{slug}] {title} ({duration} min)"
        course_dir = download_dir / slug
        course_dir.mkdir(parents=True, exist_ok=True)
        video = _existing_video(course_dir, rec_id, rec_info)
        temporary_video = video is None
        if temporary_video:
            log(f"Descargando video para transcribir: {label}")
            video = download_video({"url": rec_info["url"], "rec_id": rec_id}, cache_dir)
        if not video:
            log(f"  ERROR descargando {label}")
            failed += 1
            continue

        wav = cache_dir / f"{slug}-{hashlib.sha256(rec_id.encode()).hexdigest()[:16]}.wav"
        if not wav.exists():
            log(f"  Extrayendo audio: {video.name}")
            extracted = extract_audio(video, remove_video=temporary_video)
            if not extracted:
                log(f"  ERROR extrayendo audio de {label}")
                failed += 1
                continue
            if extracted != wav:
                extracted.replace(wav)

        destination = (
            course_dir / "transcripts" / "whisper"
            / recording_filename(id_map[rec_id], ".whisper.transcript.vtt")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        parts_dir = cache_dir / f"parts-{hashlib.sha256(rec_id.encode()).hexdigest()[:16]}"
        log(f"  Transcribiendo con faster-whisper: {label}")
        try:
            transcribe(model, wav, destination, parts_dir)
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
        for previous_transcript in _recording_transcripts(course_dir, rec_id, rec_info):
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
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    if args.always_whisper:
        keep_zoom = args.keep_zoom_transcripts
        if not keep_zoom and has_zoom_transcripts(work_dir, args.courses):
            keep_zoom = input("¿Conservar también las transcripciones originales de Zoom? [S/n]: ").strip().lower() not in {"n", "no"}
        ok, failed = transcribe_all(work_dir, args.courses, keep_zoom)
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
    recordings, missing = find_missing(work_dir)

    if not missing:
        log("Todas las grabaciones tienen transcripción. Nada que hacer.")
        return

    log(f"{len(missing)} grabaciones sin transcripción:")
    for rec in missing:
        log(f"  - [{rec['slug']}] {rec['title']} ({rec['duration']} min)")

    if args.check:
        return

    cache_dir = work_dir / ".media-cache"
    cache_dir.mkdir(exist_ok=True)

    from faster_whisper import WhisperModel
    log(f"Cargando modelo {MODEL_SIZE} ({COMPUTE_TYPE})...")
    model = WhisperModel(MODEL_SIZE, device="cpu",
                         compute_type=COMPUTE_TYPE, cpu_threads=CPU_THREADS)

    ok, failed = 0, 0
    for rec in missing:
        label = f"[{rec['slug']}] {rec['title']} ({rec['duration']} min)"

        # Reanudación: si el wav ya está en cache (corrida anterior), no
        # volver a descargar ni extraer.
        existing = [p for p in cache_dir.glob("*.wav") if rec["rec_id"] in p.name]
        if existing:
            wav = existing[0]
            log(f"Reutilizando audio en cache: {wav.name}")
        else:
            log(f"Descargando video: {label}")
            video = download_video(rec, cache_dir)
            if not video:
                log(f"  ERROR descargando {label}, se omite")
                failed += 1
                continue
            log(f"  Extrayendo audio de {video.name}")
            wav = extract_audio(video)
            if not wav:
                log(f"  ERROR extrayendo audio de {label}, se omite")
                failed += 1
                continue

        dest = work_dir / "downloads" / rec["slug"] / f"{wav.stem}.transcript.vtt"
        parts_dir = cache_dir / f"parts-{rec['rec_id'].split('.')[0][:16]}"
        log(f"  Transcribiendo (esto puede tardar ~{rec['duration']} min)...")
        try:
            transcribe(model, wav, dest, parts_dir)
        except Exception as e:
            log(f"  ERROR transcribiendo {label}: {e} (el wav y los bloques "
                "quedan en cache para reanudar)")
            failed += 1
            continue
        wav.unlink(missing_ok=True)  # solo tras escribir el VTT completo

        log(f"  OK -> {dest.name}")
        ok += 1
        # Integrar al proceso normal tras cada grabación (progreso incremental)
        copy_transcripts(work_dir / "downloads", recordings)
        log("  index.json y transcripts/ regenerados")

    log(f"Terminado: {ok} transcritas, {failed} fallidas.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
