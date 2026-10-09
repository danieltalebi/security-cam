import io
import unittest
from unittest.mock import patch

from timestamp_logging import TimestampedStream


class TimestampedStreamTests(unittest.TestCase):
    def test_prefixes_each_complete_line_and_handles_partial_writes(self):
        output = io.StringIO()
        stream = TimestampedStream(output)
        with patch("timestamp_logging.datetime") as clock:
            clock.now.return_value.astimezone.return_value.isoformat.return_value = "2026-10-09T03:00:00-03:00"
            stream.write("first")
            stream.write(" line\nsecond\n")
        self.assertEqual(
            output.getvalue(),
            "[2026-10-09T03:00:00-03:00] first line\n[2026-10-09T03:00:00-03:00] second\n",
        )


if __name__ == "__main__":
    unittest.main()
