import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claude_udea import auth


class SecureAuthStorageTests(unittest.TestCase):
    def make_keyring(self):
        values = {}
        backend = Mock()
        backend.get_password.side_effect = lambda service, account: values.get(account)
        backend.set_password.side_effect = lambda service, account, value: values.__setitem__(account, value)
        return backend, values

    def test_credentials_are_written_to_keyring_not_plaintext_file(self):
        backend, values = self.make_keyring()
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            with patch.object(auth, "_keyring_backend", return_value=backend):
                auth._save_credentials(work_dir, "student", "password")

            self.assertFalse((work_dir / auth.CREDENTIALS_FILE).exists())
        self.assertEqual(
            json.loads(values[auth.CREDENTIALS_ACCOUNT]),
            {"username": "student", "password": "password"},
        )

    def test_migrates_legacy_json_only_after_verified_keyring_write(self):
        backend, values = self.make_keyring()
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory)
            session_path = work_dir / auth.SESSION_FILE
            credentials_path = work_dir / auth.CREDENTIALS_FILE
            session_path.write_text('[{"name":"sid","value":"secret"}]', encoding="utf-8")
            credentials_path.write_text(
                '{"username":"student","password":"password"}',
                encoding="utf-8",
            )

            with patch.object(auth, "_keyring_backend", return_value=backend):
                migrated = auth.migrate_saved_secrets(work_dir)

            self.assertEqual(migrated, [auth.SESSION_FILE, auth.CREDENTIALS_FILE])
            self.assertFalse(session_path.exists())
            self.assertFalse(credentials_path.exists())

    def test_loads_session_from_keyring_without_json_file(self):
        backend, values = self.make_keyring()
        values[auth.SESSION_ACCOUNT] = json.dumps([
            {"name": "sid", "value": "secret", "domain": "udearroba.udea.edu.co", "path": "/"}
        ])
        session = Mock()
        session.get.return_value = SimpleNamespace(
            url=auth.DASHBOARD_URL,
            status_code=200,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(auth, "_keyring_backend", return_value=backend),
                patch.object(auth.requests, "Session", return_value=session),
            ):
                loaded = auth.load_session(Path(temporary_directory))

        self.assertIs(loaded, session)
        session.cookies.set.assert_called_once_with(
            "sid",
            "secret",
            domain="udearroba.udea.edu.co",
            path="/",
        )


if __name__ == "__main__":
    unittest.main()