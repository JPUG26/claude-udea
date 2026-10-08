import json
import tempfile
import unittest
from pathlib import Path

from claude_udea.download import _build_rec_id_map, _class_filename, copy_transcripts


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
            course_dir.mkdir(parents=True)
            (course_dir / "Clase #1 - 2026-02-01.transcript.vtt").write_text(
                "WEBVTT\n\nArquitectura\n",
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


if __name__ == "__main__":
    unittest.main()