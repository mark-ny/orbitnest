"""A browser tab closing mid-stream must not be logged as an application error."""

from __future__ import annotations

import unittest

from app.core.middleware import _is_client_gone


class WebSocketDisconnect(Exception):  # same class name Starlette uses
    pass


class ClientGoneTests(unittest.TestCase):
    def test_direct_and_chained_disconnects_are_recognised(self) -> None:
        self.assertTrue(_is_client_gone(WebSocketDisconnect()))
        try:
            try:
                raise WebSocketDisconnect()
            except Exception:
                raise RuntimeError("while handling") from None
        except RuntimeError as exc:
            self.assertTrue(_is_client_gone(exc))  # raised while handling the disconnect

    def test_real_errors_are_not_swallowed(self) -> None:
        self.assertFalse(_is_client_gone(ValueError("boom")))
        self.assertFalse(_is_client_gone(None))


if __name__ == "__main__":
    unittest.main()
