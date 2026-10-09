import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from transcribe_missing import transcribe_all
from claude_udea.download import copy_transcripts, rename_downloads


class WhisperTranscriptionTests(unittest.TestCase):
    def test_whisper_replaces_zoom_transcript_and_preserves_video(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            course_dir = work_dir / "downloads" / "software-architecture"
            course_dir.mkdir(parents=True)
            video = course_dir / "Architecture [recording-123].mp4"
            video.write_bytes(b"video")
            zoom_vtt = course_dir / "Architecture [recording-123].transcript.vtt"
            zoom_vtt.write_text("WEBVTT\n\nZoom subtitles\n", encoding="utf-8")
            recordings = {
                "software-architecture": {
                    "name": "Software Architecture",
                    "recordings": {
                        "recording-123": {
                            "title": "Architecture",
                            "url": "https://zoom.example/recording-123",
                            "start_date": "2026-02-01T10:00:00Z",
                            "duration_minutes": 55,
                        }
                    },
                }
            }
            (work_dir / "recordings.json").write_text(
                json.dumps(recordings),
                encoding="utf-8",
            )

            faster_whisper = ModuleType("faster_whisper")
            faster_whisper.WhisperModel = Mock(return_value=Mock())

            def extract_audio_side_effect(source, remove_video=True):
                wav = source.with_suffix(".wav")
                wav.write_bytes(b"audio")
                return wav

            def transcribe_side_effect(model, wav, destination, parts_dir):
                destination.write_text(
                    "WEBVTT\n\nNOTE\nTranscripción generada localmente con faster-whisper\n\n",
                    encoding="utf-8",
                )

            with (
                patch.dict("sys.modules", {"faster_whisper": faster_whisper}),
                patch("transcribe_missing.extract_audio", side_effect=extract_audio_side_effect),
                patch("transcribe_missing.transcribe", side_effect=transcribe_side_effect),
            ):
                result = transcribe_all(work_dir, keep_zoom_transcripts=True)

            whisper_transcript = (
                course_dir / "transcripts" / "whisper"
                / "Clase #1 - 2026-02-01.whisper.transcript.vtt"
            )
            self.assertEqual(result, (1, 0))
            self.assertTrue(whisper_transcript.is_file())
            self.assertFalse(list(course_dir.glob("*.whisper.transcript.vtt")))
            self.assertIn("faster-whisper", whisper_transcript.read_text(encoding="utf-8"))
            self.assertTrue(video.exists())

            rename_downloads(work_dir / "downloads", recordings)
            copy_transcripts(work_dir / "downloads", recordings)
            centralized = (
                work_dir / "downloads" / "transcripts" / "software-architecture"
                / whisper_transcript.name
            )
            self.assertTrue(centralized.is_file())
            self.assertIn("Zoom subtitles", (
                course_dir / "transcripts" / "zoom"
                / "Clase #1 - 2026-02-01.transcript.vtt"
            ).read_text(encoding="utf-8"))
            index = json.loads(
                (work_dir / "downloads" / "transcripts" / "index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                next(
                    entry["source"]
                    for entry in index["software-architecture"]["files"]
                    if entry["file"] == whisper_transcript.name
                ),
                "faster-whisper",
            )

    def test_fabrica_escuela_uses_its_class_name_and_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            course_dir = work_dir / "downloads" / "arquitectura-de-software"
            course_dir.mkdir(parents=True)
            (work_dir / "recordings.json").write_text(
                json.dumps({
                    "arquitectura-de-software": {
                        "name": "Arquitectura de Software",
                        "recordings": {
                            "fabrica-1": {
                                "title": "Fábrica Escuela - Introducción",
                                "url": "https://zoom.example/fabrica-1",
                                "start_date": "2026-02-01T10:00:00Z",
                            }
                        },
                    }
                }),
                encoding="utf-8",
            )
            (course_dir / "Clase [fabrica-1].mp4").write_bytes(b"video")
            faster_whisper = ModuleType("faster_whisper")
            faster_whisper.WhisperModel = Mock(return_value=Mock())

            def extract_audio_side_effect(source, remove_video=True):
                wav = source.with_suffix(".wav")
                wav.write_bytes(b"audio")
                return wav

            def transcribe_side_effect(_model, _wav, destination, _parts_dir):
                destination.write_text(
                    "WEBVTT\n\nNOTE\nTranscripción generada localmente con faster-whisper\n",
                    encoding="utf-8",
                )

            with (
                patch.dict("sys.modules", {"faster_whisper": faster_whisper}),
                patch("transcribe_missing.extract_audio", side_effect=extract_audio_side_effect),
                patch("transcribe_missing.transcribe", side_effect=transcribe_side_effect),
            ):
                self.assertEqual(transcribe_all(work_dir), (1, 0))

            self.assertTrue(
                (
                    course_dir / "transcripts" / "whisper"
                    / "Fabrica Escuela - Clase #1 - 2026-02-01.whisper.transcript.vtt"
                ).is_file()
            )

    def test_renamed_whisper_transcript_is_not_reprocessed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            course_dir = work_dir / "downloads" / "software-architecture"
            course_dir.mkdir(parents=True)
            transcript = course_dir / "Clase #1 - 2026-01-01.transcript.vtt"
            transcript.write_text(
                "WEBVTT\n\nNOTE\nTranscripción generada localmente con faster-whisper\n",
                encoding="utf-8",
            )
            (work_dir / "recordings.json").write_text(
                json.dumps({
                    "software-architecture": {
                        "recordings": {
                            "recording-123": {
                                "url": "https://zoom.example/recording-123",
                                "organized_files": [transcript.name],
                            }
                        }
                    }
                }),
                encoding="utf-8",
            )

            result = transcribe_all(work_dir)

        self.assertEqual(result, (0, 0))

    def test_zoom_transcript_retention_is_opt_in(self):
        from transcribe_missing import has_zoom_transcripts

        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            course_dir = work_dir / "downloads" / "software-architecture" / "transcripts" / "zoom"
            course_dir.mkdir(parents=True)
            zoom_vtt = course_dir / "Clase #1.transcript.vtt"
            zoom_vtt.write_text("WEBVTT\n", encoding="utf-8")
            (work_dir / "recordings.json").write_text(
                json.dumps({
                    "software-architecture": {
                        "recordings": {
                            "recording-123": {
                                "url": "https://zoom.example/recording-123",
                                "organized_files": ["transcripts/zoom/Clase #1.transcript.vtt"],
                            }
                        }
                    }
                }),
                encoding="utf-8",
            )

            self.assertTrue(has_zoom_transcripts(work_dir))


if __name__ == "__main__":
    unittest.main()