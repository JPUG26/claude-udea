import json
import tempfile
import unittest
from pathlib import Path

from claude_udea.download import (
    _build_rec_id_map,
    _class_filename,
    _recording_category,
    copy_transcripts,
    rename_downloads,
)


class ClassNumberingTests(unittest.TestCase):
    def test_fabrica_escuela_and_architecture_have_independent_sequences(self):
        recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "arch-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                    "factory-1": {
                        "title": "Fábrica de Escuela",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                    "arch-2": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-02-08T10:00:00Z",
                    },
                    "factory-2": {
                        "title": "Fábrica de Escuela",
                        "start_date": "2026-02-08T10:00:00Z",
                    },
                },
            },
        }

        metadata = _build_rec_id_map(recordings)

        self.assertEqual(metadata["arch-1"]["class_number"], 1)
        self.assertEqual(metadata["factory-1"]["class_number"], 1)
        self.assertEqual(metadata["arch-2"]["class_number"], 2)
        self.assertEqual(metadata["factory-2"]["class_number"], 2)
        self.assertEqual(
            _class_filename(metadata["arch-1"], "2026-02-01"),
            "Clase #1 - 2026-02-01",
        )
        self.assertEqual(
            _class_filename(metadata["factory-1"], "2026-02-01"),
            "Fabrica Escuela - Clase #1 - 2026-02-01",
        )

    def test_recordings_on_same_date_share_number_within_category(self):
        recordings = {
            "arquitectura-de-software": {
                "recordings": {
                    "arch-part-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                    "arch-part-2": {
                        "title": "Arquitectura de Software, continuación",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                    "factory": {
                        "title": "Fábrica de Escuela",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                },
            },
        }

        metadata = _build_rec_id_map(recordings)

        self.assertEqual(metadata["arch-part-1"]["class_number"], 1)
        self.assertEqual(metadata["arch-part-2"]["class_number"], 1)
        self.assertEqual(metadata["factory"]["class_number"], 1)

    def test_organizing_legacy_class_names_keeps_categories_distinct(self):
        recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "arch-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                    "factory-1": {
                        "title": "Fábrica de Escuela",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory) / "downloads"
            course_dir = download_dir / "arquitectura-de-software"
            zoom_dir = course_dir / "transcripts" / "zoom"
            whisper_dir = course_dir / "transcripts" / "whisper"
            zoom_dir.mkdir(parents=True)
            whisper_dir.mkdir(parents=True)
            (zoom_dir / "Clase #1 - 2026-02-01.transcript.vtt").write_text(
                "WEBVTT\n\nArquitectura\n",
                encoding="utf-8",
            )
            (whisper_dir / "Clase #1 - 2026-02-01.whisper.transcript.vtt").write_text(
                "WEBVTT\n\nArquitectura Whisper\n",
                encoding="utf-8",
            )
            (course_dir / "Fabrica Escuela - Clase #1 - 2026-02-01.transcript.vtt").write_text(
                "WEBVTT\n\nFabrica\n",
                encoding="utf-8",
            )

            copy_transcripts(download_dir, recordings)
            index = json.loads(
                (download_dir / "transcripts" / "index.json").read_text(encoding="utf-8")
            )

        topics_by_file = {
            entry["file"]: entry["topic"]
            for entry in index["arquitectura-de-software"]["files"]
        }
        self.assertEqual(
            topics_by_file["Clase #1 - 2026-02-01.transcript.vtt"],
            "Arquitectura de Software",
        )
        self.assertEqual(
            topics_by_file["Fabrica Escuela - Clase #1 - 2026-02-01.transcript.vtt"],
            "Fábrica de Escuela",
        )
        sources_by_file = {
            entry["file"]: entry["source"]
            for entry in index["arquitectura-de-software"]["files"]
        }
        self.assertEqual(sources_by_file["Clase #1 - 2026-02-01.transcript.vtt"], "zoom")
        self.assertEqual(
            sources_by_file["Clase #1 - 2026-02-01.whisper.transcript.vtt"],
            "faster-whisper",
        )

    def test_recategorizes_recording_assets_without_changing_class_names(self):
        recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "recording-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-02-01T10:00:00Z",
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory)
            course_dir = download_dir / "arquitectura-de-software"
            course_dir.mkdir()
            names = [
                "Architecture [recording-1].mp4",
                "Architecture [recording-1].transcript.vtt",
                "Architecture [recording-1].whisper.transcript.vtt",
                "Architecture [recording-1].chat.txt",
            ]
            for name in names:
                (course_dir / name).write_text("WEBVTT\n" if name.endswith(".vtt") else "chat", encoding="utf-8")

            rename_downloads(download_dir, recordings)
            organized = {
                path.relative_to(course_dir).as_posix()
                for path in course_dir.rglob("*")
                if path.is_file()
            }

        self.assertIn("videos/Clase #1 - 2026-02-01.mp4", organized)
        self.assertIn("transcripts/zoom/Clase #1 - 2026-02-01.transcript.vtt", organized)
        self.assertIn(
            "transcripts/whisper/Clase #1 - 2026-02-01.whisper.transcript.vtt",
            organized,
        )
        self.assertIn("chat/Clase #1 - 2026-02-01.chat.txt", organized)

    def test_asset_category_classifier(self):
        self.assertEqual(_recording_category("Clase [id].mp4", ".mp4"), "videos")
        self.assertEqual(_recording_category("Clase [id].transcript.vtt", ".transcript.vtt"), "transcripts/zoom")
        self.assertEqual(_recording_category("Clase [id].whisper.transcript.vtt", ".whisper.transcript.vtt"), "transcripts/whisper")
        self.assertEqual(_recording_category("Clase [id].chat.txt", ".chat.txt"), "chat")

    def test_migrates_older_whisper_vtt_by_its_note_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy_vtt = Path(temporary_directory) / "Clase [id].transcript.vtt"
            legacy_vtt.write_text(
                "WEBVTT\n\nNOTE\nTranscripción generada localmente con faster-whisper\n",
                encoding="utf-8",
            )

            self.assertEqual(
                _recording_category(legacy_vtt.name, ".transcript.vtt", legacy_vtt),
                "transcripts/whisper",
            )


if __name__ == "__main__":
    unittest.main()