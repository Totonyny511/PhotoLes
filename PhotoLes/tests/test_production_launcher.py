import os
import sys
import unittest
from unittest.mock import patch

from main import configured_port, service_commands


class ProductionLauncherTests(unittest.TestCase):
    def test_port_defaults_to_8000(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_port(), 8000)

    def test_port_uses_railway_environment(self) -> None:
        with patch.dict(os.environ, {"PORT": "3456"}, clear=True):
            self.assertEqual(configured_port(), 3456)

    def test_invalid_port_is_rejected(self) -> None:
        for value in ("not-a-number", "0", "65536"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"PORT": value}, clear=True):
                    with self.assertRaises(ValueError):
                        configured_port()

    def test_commands_start_gunicorn_and_bot(self) -> None:
        dashboard, bot = service_commands(4321)

        self.assertEqual(dashboard[:3], [sys.executable, "-m", "gunicorn"])
        self.assertIn("0.0.0.0:4321", dashboard)
        self.assertEqual(bot, [sys.executable, "bot.py"])


if __name__ == "__main__":
    unittest.main()
