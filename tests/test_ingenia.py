import json
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from claude_udea import auth, ingenia


class IngeniaLoginTests(unittest.TestCase):
    def test_login_posts_moodle_token_and_stores_credentials_securely(self):
        login_response = SimpleNamespace(
            url=ingenia.LOGIN_URL,
            text=(
                '<form action="/campus/login/index.php" method="post">'
                '<input name="logintoken" value="csrf-token"></form>'
            ),
            raise_for_status=Mock(),
        )
        post_response = SimpleNamespace(
            url=f"{ingenia.BASE_URL}/my/",
            text="",
            raise_for_status=Mock(),
        )
        session = Mock()
        session.get.return_value = login_response
        session.post.return_value = post_response

        with (
            patch.object(ingenia.requests, "Session", return_value=session),
            patch.object(ingenia, "_session_is_authenticated", return_value=True),
        ):
            result = ingenia._login_request("student", "secret")

        self.assertIs(result, session)
        self.assertEqual(
            session.post.call_args.kwargs["data"],
            {
                "anchor": "",
                "logintoken": "csrf-token",
                "username": "student",
                "password": "secret",
            },
        )

    def test_course_slug_uses_course_id(self):
        with (
            patch.object(ingenia, "login", return_value=Mock()),
            patch.object(ingenia, "scrape_course_materials", return_value={"file_count": 0}) as scrape,
        ):
            ingenia.sync_course_materials(
                "https://ingenia.udea.edu.co/campus/course/view.php?id=215",
                __import__("pathlib").Path("C:/claude-udea"),
            )

        course_info = scrape.call_args.args[1]
        self.assertEqual(course_info["course_url"], "https://ingenia.udea.edu.co/campus/course/view.php?id=215")
        self.assertEqual(scrape.call_args.args[2].name, "ingenia-215")

    @patch("claude_udea.auth.time.sleep")
    def test_ingenia_scraper_retries_a_page_without_hydrated_recordings(self, _sleep):
        recording = {
            "id": "meeting-id",
            "topic": "Clase de prueba",
            "startTime": "2026-10-07T15:01:38.000Z",
            "duration": 45,
            "videoUrl": "https://ingenia.udea.edu.co/rec/play/recording-id",
        }
        payload = json.dumps({"recordings": [recording]}, ensure_ascii=False)
        next_script = f"self.__next_f.push({json.dumps([1, payload])});"
        empty_response = SimpleNamespace(
            text="<html><title>Página sin hidratación</title></html>",
            url="https://ingenia.udea.edu.co/zoom/meeting/12345678",
            raise_for_status=Mock(),
        )
        full_response = SimpleNamespace(
            text=f"<html><script>{next_script}</script></html>",
            url="https://ingenia.udea.edu.co/zoom/meeting/12345678",
            raise_for_status=Mock(),
        )
        session = Mock()
        session.get.side_effect = [empty_response, full_response]

        with patch("builtins.print"):
            slug, links = auth._scrape_ingenia(
                session,
                "course-slug",
                {
                    "name": "Curso de prueba",
                    "moodle_url": "https://ingenia.udea.edu.co/zoom/meeting/12345678",
                },
            )

        self.assertEqual(slug, "course-slug")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["id"], "recording-id")
        self.assertEqual(session.get.call_count, 2)

    @patch("claude_udea.auth.time.sleep")
    def test_ingenia_scraper_stops_after_three_empty_hydration_responses(self, _sleep):
        response = SimpleNamespace(
            text="<html><title>Temporal</title></html>",
            url="https://ingenia.udea.edu.co/zoom/meeting/12345678",
            raise_for_status=Mock(),
        )
        session = Mock()
        session.get.return_value = response

        with patch("builtins.print"):
            slug, links = auth._scrape_ingenia(
                session,
                "course-slug",
                {
                    "name": "Curso de prueba",
                    "moodle_url": "https://ingenia.udea.edu.co/zoom/meeting/12345678",
                },
            )

        self.assertEqual(slug, "course-slug")
        self.assertEqual(links, [])
        self.assertEqual(session.get.call_count, 3)


class IngeniaCourseDiscoveryTests(unittest.TestCase):
    DASHBOARD_HTML = (
        '<div class="course-card">'
        f'<a href="{ingenia.BASE_URL}/course/view.php?id=215"><span>Arquitectura de Software</span></a>'
        '<a href="/campus/course/view.php?id=215">Arquitectura de Software</a>'
        '<a href="/campus/course/view.php?id=310">Fábrica Escuela</a>'
        '<a href="/campus/course/view.php?id=abc">Inválido</a>'
        '<a href="https://otro.example.com/course/view.php?id=999">Externo</a>'
        '</div>'
    )

    def test_list_courses_reads_unique_course_ids_from_dashboard(self):
        response = SimpleNamespace(
            url=ingenia.MY_URL,
            text=self.DASHBOARD_HTML,
            raise_for_status=Mock(),
        )
        session = Mock()
        session.get.return_value = response

        courses = ingenia.list_courses(session)

        self.assertEqual(
            courses,
            [
                {
                    "name": "Arquitectura de Software",
                    "course_url": f"{ingenia.BASE_URL}/course/view.php?id=215",
                },
                {
                    "name": "Fábrica Escuela",
                    "course_url": f"{ingenia.BASE_URL}/course/view.php?id=310",
                },
            ],
        )

    def test_sync_all_keeps_going_when_one_course_fails(self):
        courses = [
            {"name": "Curso A", "course_url": f"{ingenia.BASE_URL}/course/view.php?id=1"},
            {"name": "Curso B", "course_url": f"{ingenia.BASE_URL}/course/view.php?id=2"},
        ]
        ok_manifest = {"course": "Curso B", "course_id": "2", "failures": []}

        def fake_scrape(session, course_info, destination):
            if course_info["name"] == "Curso A":
                raise PermissionError("La sesión de Moodle expiró al abrir el curso.")
            return ok_manifest

        with (
            patch.object(ingenia, "login", return_value=Mock()),
            patch.object(ingenia, "list_courses", return_value=courses),
            patch.object(ingenia, "scrape_course_materials", side_effect=fake_scrape) as scrape,
        ):
            results = ingenia.sync_all_course_materials(Path("C:/claude-udea"))

        self.assertEqual(len(results), 2)
        self.assertIn("error", results[0])
        self.assertEqual(results[0]["course"], "Curso A")
        self.assertEqual(results[1], ok_manifest)
        self.assertEqual(scrape.call_args_list[1].args[2].name, "ingenia-2")


if __name__ == "__main__":
    unittest.main()