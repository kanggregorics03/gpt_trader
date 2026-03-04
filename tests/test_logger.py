import io
import json
import tempfile
import unittest
from pathlib import Path

from utils.logger import setup_logger


class TestLogger(unittest.TestCase):
    def test_text_logger_writes_human_readable_lines(self):
        stdout_buffer = io.StringIO()
        tmpdir = tempfile.TemporaryDirectory()
        file_path = str(Path(tmpdir.name) / "app.log")
        try:
            test_logger = setup_logger(
                log_level="INFO",
                log_format="text",
                stdout_sink=stdout_buffer,
                file_sink=file_path,
            )

            test_logger.info("text log example")

            stdout_value = stdout_buffer.getvalue()
            self.assertIn("INFO", stdout_value)
            self.assertIn("text log example", stdout_value)
        finally:
            test_logger.remove()
            setup_logger()
            tmpdir.cleanup()

    def test_json_logger_writes_json_lines(self):
        stdout_buffer = io.StringIO()
        tmpdir = tempfile.TemporaryDirectory()
        file_path = str(Path(tmpdir.name) / "app.json.log")
        try:
            test_logger = setup_logger(
                log_level="INFO",
                log_format="json",
                stdout_sink=stdout_buffer,
                file_sink=file_path,
            )

            test_logger.warning("json log example")

            stdout_value = stdout_buffer.getvalue().strip()
            payload = json.loads(stdout_value)
            self.assertEqual(payload["record"]["level"]["name"], "WARNING")
            self.assertEqual(payload["record"]["message"], "json log example")
        finally:
            test_logger.remove()
            setup_logger()
            tmpdir.cleanup()
