import unittest

from claude_udea.setup import normalize_for_source, urls_alike


class SetupUrlTests(unittest.TestCase):
    def test_normalizes_ingenia_meeting_id(self):
        url, error = normalize_for_source("96660122811", "ingenia")

        self.assertIsNone(error)
        self.assertEqual(
            url,
            "https://ingenia.udea.edu.co/zoom/meeting/96660122811",
        )

    def test_rejects_ingenia_url_from_another_host(self):
        url, error = normalize_for_source(
            "https://example.com/zoom/meeting/96660122811",
            "ingenia",
        )

        self.assertIsNone(url)
        self.assertIsInstance(error, str)

    def test_accepts_supported_moodle_zoom_activity(self):
        url, error = normalize_for_source(
            "udearroba.udea.edu.co/mod/zoom/view.php?id=12345",
            "moodle",
        )

        self.assertIsNone(error)
        self.assertEqual(
            url,
            "https://udearroba.udea.edu.co/mod/zoom/view.php?id=12345",
        )

    def test_rejects_direct_zoom_recording_link_as_moodle_source(self):
        url, error = normalize_for_source(
            "https://udearroba.zoom.us/rec/play/abc",
            "moodle",
        )

        self.assertIsNone(url)
        self.assertIsNotNone(error)

    def test_detects_same_activity_with_tracking_query(self):
        self.assertTrue(urls_alike(
            "https://udearroba.udea.edu.co/mod/zoom/view.php?id=12345",
            "http://udearroba.udea.edu.co/mod/zoom/view.php?id=12345&utm_source=mail",
        ))


if __name__ == "__main__":
    unittest.main()