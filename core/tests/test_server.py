import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

from server import _listener_arguments


class ServerListenerTests(unittest.TestCase):
    def test_prefers_an_inherited_unix_listener(self):
        with patch.dict(
            os.environ,
            {
                "TOOLFRAME_LISTEN_FD": "12",
                "TOOLFRAME_HOST": "127.0.0.1",
                "TOOLFRAME_PORT": "8000",
            },
            clear=True,
        ):
            self.assertEqual(_listener_arguments(), {"fd": 12})

    def test_falls_back_to_host_and_port_for_windows(self):
        with patch.dict(
            os.environ,
            {"TOOLFRAME_HOST": "::1", "TOOLFRAME_PORT": "8123"},
            clear=True,
        ):
            self.assertEqual(_listener_arguments(), {"host": "::1", "port": 8123})

    def test_rejects_invalid_listener_settings(self):
        invalid_environments = (
            {"TOOLFRAME_LISTEN_FD": "not-a-number"},
            {"TOOLFRAME_HOST": "127.0.0.1", "TOOLFRAME_PORT": "0"},
            {"TOOLFRAME_HOST": "127.0.0.1", "TOOLFRAME_PORT": "65536"},
        )
        for environment in invalid_environments:
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                self.assertRaises(RuntimeError),
            ):
                _listener_arguments()


if __name__ == "__main__":
    unittest.main()
