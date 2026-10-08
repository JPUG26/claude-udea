import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from transcribe_missing import transcribe_all


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
            (work_dir / "recordings.json").write_text(
                json.dumps({
                    "software-architecture": {
                        "recordings": {
                            "recording-123": {
                                "title": "Architecture",
                                "url": "https://zoom.example/recording-123",
                                "duration_minutes": 55,
                            }
                        }
                    }
                }),
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

            transcripts = list(course_dir.glob("*.vtt"))
            self.assertEqual(result, (1, 0))
            self.assertEqual(len(transcripts), 2)
            whisper_transcripts = [path for path in transcripts if ".whisper.transcript." in path.name]
            zoom_transcripts = [path for path in transcripts if ".whisper.transcript." not in path.name]
            self.assertEqual(len(whisper_transcripts), 1)
            self.assertEqual(len(zoom_transcripts), 1)
            self.assertIn("faster-whisper", whisper_transcripts[0].read_text(encoding="utf-8"))
            self.assertIn("Zoom subtitles", zoom_transcripts[0].read_text(encoding="utf-8"))
            self.assertTrue(video.exists())

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