import tempfile
import unittest
from pathlib import Path

from telegram_notify import TelegramNotifier, format_duration, parse_duration


class TelegramCommandTests(unittest.TestCase):
    def test_duration_formats(self):
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("/pause 2h"), 7200)
        self.assertEqual(parse_duration("1d 2h"), 93600)
        self.assertIsNone(parse_duration("ten minutes"))
        self.assertIsNone(parse_duration("1h later"))

    def test_duration_display(self):
        self.assertEqual(format_duration(5400), "1h 30m")

    def test_mute_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            state_file = str(Path(folder) / "mute.json")
            notifier = TelegramNotifier("token", "123", state_file)
            notifier.mute_for(600)

            restored = TelegramNotifier("token", "123", state_file)
            muted, remaining = restored.is_muted()
            self.assertTrue(muted)
            self.assertGreater(remaining, 590)

            restored.resume()
            self.assertFalse(TelegramNotifier("token", "123", state_file).is_muted()[0])


if __name__ == "__main__":
    unittest.main()
