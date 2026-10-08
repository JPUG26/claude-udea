"""
Módulo de descarga: descarga grabaciones con yt-dlp.
Aislado para que cambios en otras partes no lo afecten.
"""

import json
import re
import subprocess
import sys
import shutil
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path


def get_archive_path(download_dir: Path) -> Path:
    return download_dir / ".download-archive.txt"


def is_downloaded(archive_path: Path, rec_id: str) -> bool:
    if not archive_path.exists():
        return False
    content = archive_path.read_text(encoding="utf-8")
    return rec_id in content


def download_one(url, output_dir, archive_path, skip_video=False, dry_run=False):
    """Descarga una grabación. Retorna 'ok', 'processing' o 'error'."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title)s [%(id)s].%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        url,
        "-o", output_template,
        "--write-subs", "--all-subs",
        "--sub-format", "vtt/srt/best",
        "--convert-subs", "vtt",
        "--no-overwrites",
        "--retries", "3",
        "--fragment-retries", "3",
        "--no-warnings",
        "--download-archive", str(archive_path),
    ]

    if skip_video:
        cmd.append("--skip-download")
    else:
        cmd.extend(["--concurrent-fragments", "4"])

    if dry_run:
        cmd.append("--simulate")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        if result.returncode == 0:
            # yt-dlp con --skip-download no escribe al archive, hacerlo manualmente
            if skip_video and not dry_run:
                rec_id = _extract_rec_id_from_url(url)
                if rec_id and not is_downloaded(archive_path, rec_id):
                    with open(archive_path, "a", encoding="utf-8") as f:
                        f.write(f"zoomus {rec_id}\n")
            return "ok"
        # Zoom aún procesando o página no disponible
        stderr = result.stderr or ""
        if "Unable to extract" in stderr or "is not a valid URL" in stderr:
            return "processing"
        return "error"
    except subprocess.TimeoutExpired:
        return "error"
    except Exception:
        return "error"


def _extract_rec_id_from_url(url: str) -> str:
    """Extrae el recording ID de una URL de Zoom: /rec/share/REC_ID o /rec/play/REC_ID."""
    match = re.search(r"/rec/(?:share|play)/([^?\s]+)", url)
    return match.group(1) if match else ""


def _extract_rec_id_from_filename(filename: str) -> str:
    """Extrae el recording ID de un nombre de archivo yt-dlp: 'Titulo [REC_ID].ext'."""
    match = re.search(r"\[([^\]]+)\]", filename)
    return match.group(1) if match else ""


def _parse_recording_datetime(value):
    """Interpreta fechas ISO y formatos habituales de Moodle en español."""
    if not value:
        return None

    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    normalized = unicodedata.normalize("NFKD", text)
    normalized = normalized.encode("ascii", "ignore").decode("ascii").lower()
    months = {
        "enero": "01", "ene": "01", "febrero": "02", "feb": "02",
        "marzo": "03", "mar": "03", "abril": "04", "abr": "04",
        "mayo": "05", "may": "05", "junio": "06", "jun": "06",
        "julio": "07", "jul": "07", "agosto": "08", "ago": "08",
        "septiembre": "09", "setiembre": "09", "sep": "09",
        "octubre": "10", "oct": "10", "noviembre": "11", "nov": "11",
        "diciembre": "12", "dic": "12",
    }
    for month, number in sorted(months.items(), key=lambda item: -len(item[0])):
        normalized = re.sub(rf"\b{month}\b", number, normalized)
    normalized = re.sub(r"^[a-z]+,\s*", "", normalized)
    normalized = re.sub(r"\bde\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    formats = (
        "%d %m %Y, %H:%M", "%d %m %Y %H:%M", "%d/%m/%Y, %H:%M",
        "%d/%m/%Y %H:%M", "%d %m %Y, %I:%M %p", "%d %m %Y %I:%M %p",
        "%d/%m/%Y, %I:%M %p", "%d/%m/%Y %I:%M %p", "%d %m %Y",
        "%d/%m/%Y", "%m %d, %Y, %I:%M %p", "%m %d, %Y",
    )
    for date_format in formats:
        try:
            return datetime.strptime(normalized, date_format)
        except ValueError:
            continue
    return None


def _recording_sort_key(item):
    rec_id, rec_info = item
    recording_date = _parse_recording_datetime(rec_info.get("start_date", ""))
    if recording_date is not None and recording_date.tzinfo is not None:
        recording_date = recording_date.astimezone(timezone.utc).replace(tzinfo=None)
    return (recording_date is None, recording_date or datetime.max, rec_id)


def _datetime_key(value):
    parsed = _parse_recording_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _filename_suffix(filename: str, rec_id: str) -> str:
    """Conserva la extensión y variantes de subtítulo después del ID."""
    marker = f"[{rec_id}]"
    if rec_id and marker in filename:
        return filename.split(marker, 1)[1]
    id_suffix = re.search(
        r"\[[^\]]+\](\.(?:whisper\.)?(?:transcript|chapter|cc)\.vtt|\.[^.]+)$",
        filename,
        re.IGNORECASE,
    )
    if id_suffix:
        return id_suffix.group(1)
    match = re.match(
        r"^(?:Fabrica Escuela - )?Clase #\d+ - (?:\d{4}-\d{2}-\d{2}|sin-fecha)(.*)$",
        filename,
        re.IGNORECASE,
    )
    if not match:
        return Path(filename).suffix

    suffix = match.group(1)
    legacy_type = re.match(r"\s*-\s*(video|chapter|transcript|cc)(.*)$", suffix, re.IGNORECASE)
    if legacy_type:
        kind, extension = legacy_type.groups()
        return f".{kind.lower()}{extension}" if kind.lower() != "video" else extension
    return suffix or Path(filename).suffix


def _class_number_from_filename(filename: str):
    match = re.match(
        r"^(?:Fabrica Escuela - )?Clase #(\d+) - ",
        filename,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _class_category(title: str) -> str:
    normalized_title = unicodedata.normalize("NFKD", title)
    normalized_title = normalized_title.encode("ascii", "ignore").decode("ascii").lower()
    return "fabrica_escuela" if "fabrica de escuela" in normalized_title else "course"


def _class_category_from_filename(filename: str) -> str:
    return "fabrica_escuela" if filename.lower().startswith("fabrica escuela - ") else "course"


def _class_filename(meta: dict, date_str: str) -> str:
    category = "Fabrica Escuela - " if meta.get("class_category") == "fabrica_escuela" else ""
    return f"{category}Clase #{meta['class_number']} - {date_str}"


def _recording_category(filename: str, suffix: str, source: Path | None = None) -> str:
    lowered = (filename + suffix).lower()
    if lowered.endswith(".vtt"):
        is_whisper = ".whisper.transcript." in lowered
        if not is_whisper and source and source.is_file():
            try:
                is_whisper = "generada localmente con faster-whisper" in source.read_text(
                    encoding="utf-8", errors="replace"
                )[:500]
            except OSError:
                pass
        return "transcripts/whisper" if is_whisper else "transcripts/zoom"
    if Path(lowered).suffix in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
        return "videos"
    if "chat" in Path(lowered).name and Path(lowered).suffix in {".txt", ".json", ".vtt", ".csv"}:
        return "chat"
    return ""


def _build_rec_id_map(recordings: dict) -> dict:
    """Mapa de rec_id -> {slug, course_name, start_date, duration, title}."""
    id_map = {}
    for slug, course in recordings.items():
        ordered_recordings = sorted(
            course.get("recordings", {}).items(), key=_recording_sort_key
        )
        class_numbers = {}
        class_by_datetime = {}
        for rec_id, rec_info in ordered_recordings:
            category = _class_category(rec_info.get("title", ""))
            date_key = _datetime_key(rec_info.get("start_date", ""))
            category_date_key = (category, date_key)
            if date_key is not None and category_date_key in class_by_datetime:
                recording_class = class_by_datetime[category_date_key]
            else:
                class_numbers[category] = class_numbers.get(category, 0) + 1
                recording_class = class_numbers[category]
                if date_key is not None:
                    class_by_datetime[category_date_key] = recording_class
            id_map[rec_id] = {
                "slug": slug,
                "course_name": course.get("name", slug),
                "start_date": rec_info.get("start_date", ""),
                "duration_minutes": rec_info.get("duration_minutes", 0),
                "title": rec_info.get("title", ""),
                "class_category": category,
                "class_number": recording_class,
            }
    return id_map


def _parse_date_prefix(start_date: str) -> str:
    """Convierte la fecha de Moodle a YYYY-MM-DD para nombres de archivo."""
    parsed = _parse_recording_datetime(start_date)
    return parsed.strftime("%Y-%m-%d") if parsed else "sin-fecha"


def _inject_vtt_metadata(vtt_content: str, course_name: str, date_str: str,
                         duration_minutes: int, title: str) -> str:
    """Inyecta un bloque NOTE con metadata después del header WEBVTT."""
    note_block = (
        f"\nNOTE\n"
        f"Fecha de clase: {date_str}\n"
        f"Asignatura: {course_name}\n"
        f"Tema: {title}\n"
        f"Duración: {duration_minutes} min\n"
    )
    if vtt_content.startswith("WEBVTT"):
        first_newline = vtt_content.index("\n")
        return vtt_content[:first_newline] + "\n" + note_block + vtt_content[first_newline:]
    return note_block + "\n" + vtt_content


def copy_transcripts(download_dir: Path, recordings: dict = None) -> int:
    """
    Copia VTTs a carpeta centralizada con fecha en el nombre,
    metadata inyectada en el VTT y genera index.json.
    Retorna cantidad procesada.
    """
    transcripts_dir = download_dir / "transcripts"

    # Si no hay recordings, cargar desde disco
    if recordings is None:
        rec_path = download_dir.parent / "recordings.json"
        if rec_path.exists():
            with open(rec_path, "r", encoding="utf-8") as f:
                recordings = json.load(f)
        else:
            recordings = {}

    id_map = _build_rec_id_map(recordings)
    class_map = {
        (meta["slug"], meta["class_category"], meta["class_number"]): meta
        for meta in id_map.values()
    }
    count = 0
    index = {}

    # Limpiar carpeta de transcripts para regenerar con nuevos nombres
    if transcripts_dir.exists():
        for child in transcripts_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            elif child.name != ".gitkeep":
                child.unlink()

    def process_vtt(vtt_file: Path, course_slug: str):
        nonlocal count
        rec_id = _extract_rec_id_from_filename(vtt_file.name)
        meta = id_map.get(rec_id, {})
        if not rec_id:
            class_number = _class_number_from_filename(vtt_file.name)
            if class_number:
                category = _class_category_from_filename(vtt_file.name)
                meta = class_map.get((course_slug, category, class_number), {})
        if rec_id and not meta:
            return

        date_str = _parse_date_prefix(meta.get("start_date", ""))
        course_name = meta.get("course_name", course_slug)
        duration = meta.get("duration_minutes", 0)
        title = meta.get("title", "")

        if meta:
            suffix = _filename_suffix(vtt_file.name, rec_id)
            new_name = f"{_class_filename(meta, date_str)}{suffix}"
        else:
            new_name = f"{date_str}_{vtt_file.name}"
        course_transcripts = transcripts_dir / course_slug
        course_transcripts.mkdir(parents=True, exist_ok=True)
        dest = course_transcripts / new_name
        if dest.exists():
            suffix = _filename_suffix(vtt_file.name, rec_id)
            if not suffix or not new_name.endswith(suffix):
                suffix = Path(new_name).suffix
            prefix = new_name[:-len(suffix)] if suffix else new_name
            duplicate_tag = f" [{rec_id}]" if rec_id else " [duplicate]"
            candidate = course_transcripts / f"{prefix}{duplicate_tag}{suffix}"
            duplicate_number = 2
            while candidate.exists():
                candidate = course_transcripts / (
                    f"{prefix}{duplicate_tag} {duplicate_number}{suffix}"
                )
                duplicate_number += 1
            dest = candidate
            new_name = dest.name

        vtt_content = vtt_file.read_text(encoding="utf-8", errors="replace")
        enriched = _inject_vtt_metadata(vtt_content, course_name, date_str, duration, title)
        dest.write_text(enriched, encoding="utf-8")
        count += 1

        vtt_type = "transcript" if ".transcript." in vtt_file.name else "chapter"

        if course_slug not in index:
            index[course_slug] = {"course_name": course_name, "files": []}
        index[course_slug]["files"].append({
            "file": new_name,
            "date": date_str,
            "class_number": meta.get("class_number"),
            "topic": title,
            "duration_minutes": duration,
            "type": vtt_type,
            "source": "faster-whisper" if ".whisper.transcript." in vtt_file.name.lower() else "zoom",
        })

    for course_dir in download_dir.iterdir():
        if not course_dir.is_dir() or course_dir.name in ("transcripts", ".browser-data"):
            continue
        slug = course_dir.name

        for rec_dir in course_dir.iterdir():
            if not rec_dir.is_dir():
                continue
            for vtt_file in rec_dir.glob("*.vtt"):
                process_vtt(vtt_file, slug)

        for vtt_file in course_dir.glob("*.vtt"):
            process_vtt(vtt_file, slug)

        for vtt_file in course_dir.rglob("*.vtt"):
            if vtt_file.parent != course_dir:
                process_vtt(vtt_file, slug)

    # Ordenar por fecha
    for slug in index:
        index[slug]["files"].sort(key=lambda x: x["date"])

    # Escribir index.json
    if index:
        index_path = transcripts_dir / "index.json"
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)

    return count


def rename_downloads(download_dir: Path, recordings: dict) -> int:
    """Renombra descargas por clase y guarda sus rutas para futuras renumeraciones."""
    id_map = _build_rec_id_map(recordings)
    renamed_count = 0

    for slug, course in recordings.items():
        course_dir = download_dir / slug
        if not course_dir.is_dir():
            continue
        for category_dir in (
            "videos",
            "transcripts/zoom",
            "transcripts/whisper",
            "chat",
        ):
            (course_dir / category_dir).mkdir(parents=True, exist_ok=True)

        plans = []
        record_files = {}
        for rec_id, rec_info in course.get("recordings", {}).items():
            meta = id_map.get(rec_id)
            if not meta:
                continue

            sources = {}
            for relative_name in rec_info.get("organized_files", []):
                source = course_dir / relative_name
                if source.is_file():
                    sources[source] = _filename_suffix(source.name, rec_id)

            for source in course_dir.rglob("*"):
                if source.is_file() and f"[{rec_id}]" in source.name:
                    sources[source] = _filename_suffix(source.name, rec_id)

            organized_files = []
            record_files[rec_id] = (rec_info, organized_files)
            date_str = _parse_date_prefix(meta["start_date"])
            for source, suffix in sources.items():
                category = _recording_category(source.name, suffix, source)
                target_dir = course_dir / category if category else source.parent
                destination = target_dir / f"{_class_filename(meta, date_str)}{suffix}"
                plans.append((source, destination, rec_id, rec_info, organized_files))

        source_paths = {source for source, _, _, _, _ in plans}
        reserved_destinations = set()
        resolved_plans = []
        for source, destination, rec_id, rec_info, organized_files in plans:
            if destination in reserved_destinations or (
                destination.exists() and destination not in source_paths
            ):
                suffix = _filename_suffix(destination.name, "")
                if not suffix or not destination.name.endswith(suffix):
                    suffix = destination.suffix
                base_name = destination.name[:-len(suffix)] if suffix else destination.name
                duplicate_number = 1
                while destination in reserved_destinations or (
                    destination.exists() and destination not in source_paths
                ):
                    duplicate_tag = f" [{rec_id}]" if duplicate_number == 1 else f" [{rec_id} copy {duplicate_number}]"
                    destination = destination.with_name(
                        f"{base_name}{duplicate_tag}{suffix}"
                    )
                    duplicate_number += 1
            reserved_destinations.add(destination)
            resolved_plans.append(
                (source, destination, rec_id, rec_info, organized_files)
            )

        staged_plans = []
        for source, destination, rec_id, rec_info, organized_files in resolved_plans:
            if source == destination:
                organized_files.append(destination.relative_to(course_dir).as_posix())
                continue
            temporary = source.with_name(f".{source.name}.{uuid.uuid4().hex}.tmp")
            source.rename(temporary)
            staged_plans.append(
                (temporary, destination, rec_id, rec_info, organized_files)
            )

        for temporary, destination, rec_id, rec_info, organized_files in staged_plans:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(destination)
            renamed_count += 1
            organized_files.append(destination.relative_to(course_dir).as_posix())

        for rec_id, (rec_info, organized_files) in record_files.items():
            if organized_files:
                rec_info["organized_files"] = organized_files

    return renamed_count


def backfill_recording_metadata(course_dir: Path, course: dict, links: list[dict]) -> int:
    """Asocia archivos antiguos con metadata por creation_time exacto del MP4."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not course_dir.is_dir():
        return 0

    videos_by_datetime = {}
    for video in course_dir.rglob("*.mp4"):
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries",
                    "format_tags=creation_time", "-of", "json", str(video),
                ],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            )
            if result.returncode != 0:
                continue
            tags = json.loads(result.stdout).get("format", {}).get("tags", {})
            date_key = _datetime_key(tags.get("creation_time", ""))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            continue
        if date_key is not None:
            videos_by_datetime.setdefault(date_key, []).append(video)

    if not videos_by_datetime:
        return 0

    owners = {}
    records_by_datetime = {}
    for rec_info in course.get("recordings", {}).values():
        date_key = _datetime_key(rec_info.get("start_date", ""))
        if date_key is not None:
            records_by_datetime.setdefault(date_key, []).append(rec_info)
        for relative_name in rec_info.get("organized_files", []):
            key = str(relative_name).replace("\\", "/").casefold()
            owners.setdefault(key, []).append(rec_info)

    link_dates = {
        _datetime_key(link.get("start_date", ""))
        for link in links
        if _datetime_key(link.get("start_date", "")) is not None
    }
    metadata_links = list(links)
    for date_key, rec_infos in records_by_datetime.items():
        if date_key in link_dates:
            continue
        rec_info = rec_infos[0]
        metadata_links.append({
            "start_date": rec_info.get("start_date", ""),
            "title": rec_info.get("title", ""),
            "duration_minutes": rec_info.get("duration_minutes", 0),
        })

    backfilled = 0
    for link in metadata_links:
        date_key = _datetime_key(link.get("start_date", ""))
        matching_videos = videos_by_datetime.get(date_key, [])
        if not matching_videos:
            continue

        assets = set(matching_videos)
        class_numbers = set()
        recording_ids = set()
        for video in matching_videos:
            class_number = _class_number_from_filename(video.name)
            if class_number is not None:
                class_numbers.add(str(class_number))
            rec_id = _extract_rec_id_from_filename(video.name)
            if rec_id:
                recording_ids.add(rec_id)

        for asset in course_dir.rglob("*"):
            if not asset.is_file():
                continue
            class_number = _class_number_from_filename(asset.name)
            rec_id = _extract_rec_id_from_filename(asset.name)
            if (class_number is not None and str(class_number) in class_numbers) or (
                rec_id and rec_id in recording_ids
            ):
                assets.add(asset)

        asset_names = sorted(
            asset.relative_to(course_dir).as_posix() for asset in assets
        )
        matching_records = {
            id(rec_info): rec_info
            for rec_info in records_by_datetime.get(date_key, [])
        }
        unowned_assets = []
        for relative_name in asset_names:
            asset_owners = owners.get(relative_name.casefold(), [])
            if asset_owners:
                for rec_info in asset_owners:
                    matching_records[id(rec_info)] = rec_info
            else:
                unowned_assets.append(relative_name)

        if matching_records:
            for index, rec_info in enumerate(matching_records.values()):
                rec_info["start_date"] = link["start_date"]
                rec_info["title"] = link.get("title") or rec_info.get("title", "")
                rec_info["duration_minutes"] = link.get(
                    "duration_minutes", rec_info.get("duration_minutes", 0)
                )
                existing_files = set(rec_info.get("organized_files", []))
                if index == 0:
                    existing_files.update(unowned_assets)
                rec_info["organized_files"] = sorted(existing_files)
                if any(Path(name).suffix.lower() == ".mp4" for name in rec_info["organized_files"]):
                    rec_info["downloaded"] = True
                backfilled += 1
        else:
            link["_existing_files"] = asset_names
            link["_already_downloaded"] = True
            backfilled += 1

    return backfilled


def count_transcripts(download_dir: Path, slug: str) -> int:
    """Cuenta VTTs de una asignatura en transcripts/."""
    course_transcripts = download_dir / "transcripts" / slug
    if not course_transcripts.exists():
        return 0
    return len(list(course_transcripts.glob("*.vtt")))
