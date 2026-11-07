import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

    def test_local_alarm_handler_receives_commands(self):
        notifier = TelegramNotifier("token", "123")
        handler = MagicMock(return_value="local response")
        notifier.set_command_handler(handler)
        notifier.send_message = MagicMock()

        notifier._handle_command({
            "message": {"chat": {"id": 123}, "text": "/away"},
        })

        handler.assert_called_once_with("/away")
        notifier.send_message.assert_called_once_with("local response")

    def test_full_control_commands_are_delegated_to_local_policy(self):
        notifier = TelegramNotifier("token", "123")
        handler = MagicMock(side_effect=lambda command: f"handled {command}")
        notifier.set_command_handler(handler)
        notifier.send_message = MagicMock()

        commands = (
            "/help", "/auto", "/manual", "/away", "/home", "/disarm",
            "/status", "/resume", "/until8", "10m", "1h30m", "OK",
        )
        for command in commands:
            notifier._handle_command({
                "message": {"chat": {"id": 123}, "text": command},
            })

        self.assertEqual(
            [call.args[0] for call in handler.call_args_list],
            [command.lower() for command in commands],
        )
        self.assertEqual(notifier.send_message.call_count, len(commands))


if __name__ == "__main__":
    unittest.main()
