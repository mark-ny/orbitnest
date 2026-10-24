from __future__ import annotations

import io
import json
import logging
import unittest

from app.core.logging import REDACTED, JsonFormatter, RedactingFilter, redact, redact_text


def _capture(msg: str, *args: object, **extra: object) -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("orbitnest.test.capture")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.info(msg, *args, extra=extra)
    return json.loads(stream.getvalue())


class RedactionTests(unittest.TestCase):
    def test_key_value_text(self) -> None:
        out = redact_text("login password=hunter2 token: abc123")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abc123", out)

    def test_bearer_token(self) -> None:
        self.assertNotIn("eyJhbGciOi", redact_text("Authorization: Bearer eyJhbGciOi.payload.sig"))

    def test_url_credentials(self) -> None:
        out = redact_text("connect https://user:pw123@example.com/path")
        self.assertNotIn("pw123", out)

    def test_nested_structures(self) -> None:
        data = {"user": "bob", "password": "p", "nested": {"api_key": "k", "list": [{"csrf_token": "c"}]}}
        out = redact(data)
        self.assertEqual(out["user"], "bob")
        self.assertEqual(out["password"], REDACTED)
        self.assertEqual(out["nested"]["api_key"], REDACTED)
        self.assertEqual(out["nested"]["list"][0]["csrf_token"], REDACTED)

    def test_log_record_message_args_and_extra(self) -> None:
        rec = _capture("user %s sent password=%s", "bob", "s3cret", session_cookie="abc", path="/x")
        flat = json.dumps(rec)
        self.assertNotIn("s3cret", flat)
        self.assertNotIn('"abc"', flat)
        self.assertEqual(rec["path"], "/x")
        self.assertIn("request_id", rec)

    def test_exception_text_is_redacted(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RedactingFilter())
        logger = logging.getLogger("orbitnest.test.exc")
        logger.handlers = [handler]
        logger.propagate = False
        try:
            raise ValueError("bad password=topsecret")
        except ValueError:
            logger.exception("boom")
        self.assertNotIn("topsecret", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
