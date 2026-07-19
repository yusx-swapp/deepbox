"""Tests for the structured JSON logging module."""
import json
import logging
import unittest

from server.app.logging import JsonFormatter, configure_logging, log_event


class JsonFormatterTests(unittest.TestCase):
    def _record(self, **extra):
        rec = logging.LogRecord(
            name="deepbox", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        for k, v in extra.items():
            setattr(rec, k, v)
        return rec

    def test_renders_single_line_json(self):
        out = JsonFormatter().format(self._record())
        self.assertNotIn("\n", out)
        data = json.loads(out)
        self.assertEqual(data["message"], "hello")
        self.assertEqual(data["level"], "INFO")
        self.assertEqual(data["logger"], "deepbox")
        self.assertTrue(data["ts"].endswith("Z"))

    def test_surfaces_extra_fields(self):
        data = json.loads(JsonFormatter().format(self._record(event="x", devbox_id="d1")))
        self.assertEqual(data["event"], "x")
        self.assertEqual(data["devbox_id"], "d1")

    def test_non_serialisable_value_is_repr(self):
        obj = object()
        data = json.loads(JsonFormatter().format(self._record(thing=obj)))
        self.assertEqual(data["thing"], repr(obj))

    def test_exc_info_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            rec = self._record()
            rec.exc_info = sys.exc_info()
            data = json.loads(JsonFormatter().format(rec))
        self.assertIn("ValueError", data["exc_info"])


class LogEventTests(unittest.TestCase):
    def test_log_event_drops_none(self):
        logger = logging.getLogger("test.event")
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            log_event(logger, "connector.online", devbox_id="d1", extra_none=None)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].event, "connector.online")
        self.assertEqual(records[0].devbox_id, "d1")
        self.assertFalse(hasattr(records[0], "extra_none"))


class ConfigureLoggingTests(unittest.TestCase):
    def test_idempotent(self):
        configure_logging("INFO")
        configure_logging("DEBUG")
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if getattr(h, "_deepbox_json", False)]
        self.assertEqual(len(json_handlers), 1)


if __name__ == "__main__":
    unittest.main()
