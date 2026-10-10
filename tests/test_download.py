import json
import tempfile
import unittest
import uuid
from pathlib import Path

from claude_udea.download import (
    _build_rec_id_map,
    _class_filename,
    _filename_suffix,
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

    def test_recovers_legacy_long_organization_and_whisper_temps(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory)
            course_dir = download_dir / "comprension-lectora-felipe"
            course_dir.mkdir()
            intended_video = course_dir / "Clase #12 - 2026-09-02.mp4"
            temporary_video = course_dir / f".{intended_video.name}.{uuid.uuid4().hex}.tmp"
            temporary_video.write_bytes(b"video")

            intended_whisper = course_dir / "Clase #13 - 2026-09-04.whisper.transcript.vtt"
            temporary_whisper = intended_whisper.with_name(intended_whisper.name + ".tmp")
            temporary_whisper.write_text(
                "WEBVTT\n\nNOTE\nTranscripción generada localmente con faster-whisper\n",
                encoding="utf-8",
            )

            rename_downloads(download_dir, {})

            self.assertEqual(intended_video.read_bytes(), b"video")
            self.assertTrue(intended_whisper.is_file())
            self.assertFalse(temporary_video.exists())
            self.assertFalse(temporary_whisper.exists())

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


class LooseFileOrganizationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.download_dir = Path(self._temporary.name)
        self.course_dir = self.download_dir / "arquitectura-de-software"
        self.course_dir.mkdir()
        self.recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "factory-3": {
                        "title": "FABRICA DE ESCUELA INGENIERÍA DE SISTEMA (2026-2)",
                        "start_date": "2026-08-26T10:59:13Z",
                        "downloaded": True,
                    },
                },
            },
        }

    def tearDown(self):
        self._temporary.cleanup()

    def test_loose_factory_files_without_id_go_to_their_category_with_standard_name(self):
        loose_video = self.course_dir / "Fabrica Escuela - Clase #1 - 2026-08-26.mp4"
        loose_video.write_bytes(b"video")
        loose_vtt = self.course_dir / "Fabrica Escuela - Clase #1 - 2026-08-26.transcript.vtt"
        loose_vtt.write_text("WEBVTT\n", encoding="utf-8")

        rename_downloads(self.download_dir, self.recordings)

        self.assertTrue((self.course_dir / "videos" / "Fabrica Escuela - Clase #1 - 2026-08-26.mp4").is_file())
        self.assertTrue(
            (self.course_dir / "transcripts" / "zoom" / "Fabrica Escuela - Clase #1 - 2026-08-26.transcript.vtt").is_file()
        )
        self.assertFalse(loose_video.exists())
        self.assertFalse(loose_vtt.exists())

    def test_partial_and_temporary_downloads_are_kept_out_of_videos(self):
        partial = self.course_dir / "Clase #4 - 2026-09-05.mp4.part"
        partial.write_bytes(b"partial")
        legacy_temp = self.course_dir / "Clase #6 - 2026-09-12.mp4.3223.tmp.f1f7.tmp"
        legacy_temp.write_bytes(b"partial")

        rename_downloads(self.download_dir, self.recordings)

        self.assertTrue((self.course_dir / "incompletos" / partial.name).is_file())
        self.assertTrue((self.course_dir / "incompletos" / legacy_temp.name).is_file())
        self.assertEqual(list((self.course_dir / "videos").glob("*.part")), [])

    def test_unresolvable_root_files_are_swept_to_sin_clasificar(self):
        orphan = self.course_dir / "clase #17 - sin-fecha - chapter.vtt"
        orphan.write_text("WEBVTT\n", encoding="utf-8")

        rename_downloads(self.download_dir, self.recordings)

        self.assertFalse(orphan.exists())
        self.assertTrue((self.course_dir / "sin-clasificar" / orphan.name).is_file())

    def test_file_with_id_and_no_recording_is_swept_not_left_in_root(self):
        stray = self.course_dir / "Algo [unknown-id].mp4"
        stray.write_bytes(b"video")

        rename_downloads(self.download_dir, self.recordings)

        self.assertFalse(stray.exists())
        self.assertTrue((self.course_dir / "sin-clasificar" / stray.name).is_file())

class RecordingsStoreTests(unittest.TestCase):
    def test_load_keeps_recordings_that_share_a_start_date(self):
        from claude_udea.cli import load_recordings

        data = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "class-1": {"title": "Arquitectura de Software", "start_date": "2026-09-01T17:01:34Z"},
                    "factory-1": {"title": "FABRICA DE ESCUELA", "start_date": "2026-09-01T17:01:34Z"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "recordings.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            loaded = load_recordings(path)

        self.assertEqual(set(loaded["arquitectura-de-software"]["recordings"]), {"class-1", "factory-1"})


class FilenameSuffixTests(unittest.TestCase):
    def test_extracts_suffix_after_rec_id_bracket(self):
        self.assertEqual(
            _filename_suffix("Clase [rec123].mp4", "rec123"),
            ".mp4",
        )
        self.assertEqual(
            _filename_suffix("Titulo [recording-1].transcript.vtt", "recording-1"),
            ".transcript.vtt",
        )

    def test_extracts_suffix_after_rec_id_with_copy_tag(self):
        self.assertEqual(
            _filename_suffix(
                "Clase #1 - 2026-09-01 [rec123 copy 2].mp4", "rec123"
            ),
            ".mp4",
        )
        self.assertEqual(
            _filename_suffix(
                "Fabrica Escuela - Clase #1 - 2026-08-26 [factory-3 copy 2].whisper.transcript.vtt",
                "factory-3",
            ),
            ".whisper.transcript.vtt",
        )

    def test_extracts_suffix_from_clean_filename_without_brackets(self):
        self.assertEqual(
            _filename_suffix("Clase #1 - 2026-02-01.mp4", ""),
            ".mp4",
        )
        self.assertEqual(
            _filename_suffix("Clase #1 - 2026-02-01.transcript.vtt", ""),
            ".transcript.vtt",
        )


class CleanupLegacyNamesTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.download_dir = Path(self._temporary.name)
        self.course_dir = self.download_dir / "arquitectura-de-software"
        self.course_dir.mkdir(parents=True)

    def tearDown(self):
        self._temporary.cleanup()

    def _recordings(self, rec_id, title, start_date, organized_files=None):
        return {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    rec_id: {
                        "title": title,
                        "start_date": start_date,
                        "organized_files": organized_files or [],
                    },
                },
            },
        }

    def test_removes_simple_rec_id_tag(self):
        legacy = self.course_dir / "Clase #1 - 2026-09-01 [rec123].mp4"
        legacy.write_bytes(b"video")
        recordings = self._recordings(
            "rec123", "Arquitectura de Software", "2026-09-01T10:00:00Z"
        )

        rename_downloads(self.download_dir, recordings)

        self.assertFalse(legacy.exists())
        self.assertTrue(
            (self.course_dir / "videos" / "Clase #1 - 2026-09-01.mp4").is_file()
        )

    def test_removes_copy_tag(self):
        legacy = self.course_dir / "Clase #1 - 2026-09-01 [rec123 copy 2].mp4"
        legacy.write_bytes(b"video")
        recordings = self._recordings(
            "rec123", "Arquitectura de Software", "2026-09-01T10:00:00Z"
        )

        rename_downloads(self.download_dir, recordings)

        self.assertFalse(legacy.exists())
        self.assertTrue(
            (self.course_dir / "videos" / "Clase #1 - 2026-09-01.mp4").is_file()
        )

    def test_handles_fabrica_escuela_prefix(self):
        legacy = (
            self.course_dir
            / "Fabrica Escuela - Clase #1 - 2026-08-26 [factory-3 copy 2].transcript.vtt"
        )
        legacy.write_text("WEBVTT\n", encoding="utf-8")
        recordings = self._recordings(
            "factory-3", "FABRICA DE ESCUELA INGENIERÍA DE SISTEMA", "2026-08-26T10:59:13Z"
        )

        rename_downloads(self.download_dir, recordings)

        self.assertFalse(legacy.exists())
        self.assertTrue(
            (
                self.course_dir
                / "transcripts" / "zoom"
                / "Fabrica Escuela - Clase #1 - 2026-08-26.transcript.vtt"
            ).is_file()
        )

    def test_avoids_collision_when_clean_name_exists(self):
        clean = self.course_dir / "videos" / "Clase #1 - 2026-09-01.mp4"
        clean.parent.mkdir(parents=True)
        clean.write_bytes(b"original")
        legacy = self.course_dir / "Clase #1 - 2026-09-01 [rec123 copy 2].mp4"
        legacy.write_bytes(b"duplicate")
        recordings = self._recordings(
            "rec123",
            "Arquitectura de Software",
            "2026-09-01T10:00:00Z",
            organized_files=["videos/Clase #1 - 2026-09-01.mp4"],
        )

        rename_downloads(self.download_dir, recordings)

        self.assertTrue(clean.is_file())
        self.assertEqual(clean.read_bytes(), b"original")
        self.assertFalse(legacy.exists())
        self.assertTrue(
            (self.course_dir / "videos" / "Clase #1 - 2026-09-01 - Parte 1.mp4").is_file()
        )

    def test_skips_files_without_legacy_tags(self):
        clean = self.course_dir / "videos" / "Clase #1 - 2026-09-01.mp4"
        clean.parent.mkdir(parents=True)
        clean.write_bytes(b"video")
        recordings = self._recordings(
            "rec123",
            "Arquitectura de Software",
            "2026-09-01T10:00:00Z",
            organized_files=["videos/Clase #1 - 2026-09-01.mp4"],
        )

        rename_downloads(self.download_dir, recordings)

        self.assertTrue(clean.is_file())
        self.assertEqual(clean.read_bytes(), b"video")


class DuplicateTagFormatTests(unittest.TestCase):
    def test_rename_downloads_labels_distinct_duplicates_as_parts(self):
        recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "recording-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-09-01T10:00:00Z",
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory)
            course_dir = download_dir / "arquitectura-de-software"
            videos_dir = course_dir / "videos"
            videos_dir.mkdir(parents=True)

            (videos_dir / "Clase #1 - 2026-09-01.mp4").write_bytes(b"first")
            duplicate = course_dir / "Duplicado [recording-1].mp4"
            duplicate.write_bytes(b"second")

            rename_downloads(download_dir, recordings)

            organized = sorted(
                path.relative_to(course_dir).as_posix()
                for path in course_dir.rglob("*")
                if path.is_file()
            )

            self.assertIn("videos/Clase #1 - 2026-09-01.mp4", organized)
            self.assertIn("videos/Clase #1 - 2026-09-01 - Parte 1.mp4", organized)
            self.assertFalse(duplicate.exists())

    def test_rename_downloads_preserves_cleanly_named_files_on_rerun(self):
        recordings = {
            "arquitectura-de-software": {
                "name": "Arquitectura de Software",
                "recordings": {
                    "recording-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-09-01T10:00:00Z",
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory)
            course_dir = download_dir / "arquitectura-de-software"
            videos_dir = course_dir / "videos"
            videos_dir.mkdir(parents=True)

            video_path = videos_dir / "Clase #1 - 2026-09-01.mp4"
            video_path.write_bytes(b"video")

            recordings["arquitectura-de-software"]["recordings"]["recording-1"][
                "organized_files"
            ] = ["videos/Clase #1 - 2026-09-01.mp4"]

            rename_downloads(download_dir, recordings)

            self.assertTrue(video_path.is_file())
            self.assertEqual(video_path.read_bytes(), b"video")

            videos = list(videos_dir.iterdir())
            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0].name, "Clase #1 - 2026-09-01.mp4")


class VideoDuplicateSafetyTests(unittest.TestCase):
    def test_backups_only_byte_identical_video_and_keeps_distinct_content_as_part(self):
        recordings = {
            "arquitectura-de-software": {
                "recordings": {
                    "rec-1": {
                        "title": "Arquitectura de Software",
                        "start_date": "2026-09-01T10:00:00Z",
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_dir = Path(temporary_directory)
            course_dir = download_dir / "arquitectura-de-software"
            videos = course_dir / "videos"
            videos.mkdir(parents=True)
            (videos / "Clase #1 - 2026-09-01.mp4").write_bytes(b"same")
            duplicate = course_dir / "Clase #1 - 2026-09-01 [rec-1 copy 2].mp4"
            duplicate.write_bytes(b"same")
            distinct = course_dir / "Clase #1 - 2026-09-01 [rec-1 copy 3].mp4"
            distinct.write_bytes(b"part two")
            recordings["arquitectura-de-software"]["recordings"]["rec-1"][
                "organized_files"
            ] = [
                "videos/Clase #1 - 2026-09-01.mp4",
                duplicate.name,
                distinct.name,
            ]

            rename_downloads(download_dir, recordings)

            canonical = videos / "Clase #1 - 2026-09-01.mp4"
            part = videos / "Clase #1 - 2026-09-01 - Parte 1.mp4"
            backup = course_dir / ".claude-udea-backup" / "identical-videos" / duplicate.name
            self.assertEqual(canonical.read_bytes(), b"same")
            self.assertTrue(backup.is_file())
            self.assertEqual(part.read_bytes(), b"part two")
            self.assertIn("videos/Clase #1 - 2026-09-01 - Parte 1.mp4", recordings[
                "arquitectura-de-software"]["recordings"]["rec-1"]["organized_files"])


class RecordingMergeTests(unittest.TestCase):
    def test_keeps_distinct_recording_ids_with_same_date_and_category(self):
        from claude_udea.cli import _merge_scraped

        existing = {}
        config = {
            "download_dir": ".",
            "courses": {"architecture": {"name": "Architecture"}},
        }
        links = [
            {
                "url": f"https://zoom.example/rec/share/{rec_id}",
                "full_url": f"https://zoom.example/rec/share/{rec_id}",
                "text": title,
                "topic": title,
                "start_date": "2026-09-01T10:00:00Z",
                "duration_minutes": 60,
            }
            for rec_id, title in (("rec-1", "Architecture"), ("rec-2", "Architecture"))
        ]

        pending = _merge_scraped(existing, config, "architecture", links)

        self.assertEqual(set(existing["architecture"]["recordings"]), {"rec-1", "rec-2"})
        self.assertEqual({item[1] for item in pending}, {"rec-1", "rec-2"})

    def test_duplicate_id_in_one_scrape_is_queued_once(self):
        from claude_udea.cli import _merge_scraped

        existing = {}
        config = {"download_dir": ".", "courses": {"architecture": {"name": "Architecture"}}}
        link = {
            "url": "https://zoom.example/rec/share/rec-1",
            "full_url": "https://zoom.example/rec/share/rec-1",
            "text": "Architecture",
            "topic": "Architecture",
            "start_date": "2026-09-01T10:00:00Z",
            "duration_minutes": 60,
        }

        pending = _merge_scraped(existing, config, "architecture", [link, link])

        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
