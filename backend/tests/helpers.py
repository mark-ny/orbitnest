from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from tests.asgi_client import AsgiClient


def make_settings(tmp: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "test",
        "data_dir": tmp,
        "log_level": "CRITICAL",
    }
    values.update(overrides)
    return Settings.model_validate(values)


class TempDirMixin:
    """unittest mixin giving each test an isolated data dir."""

    tmp: Path

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        super().setUp()  # type: ignore[misc]

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()  # type: ignore[misc]

    def make_client(self, **overrides: object) -> AsgiClient:
        app = create_app(make_settings(self.tmp, **overrides))
        return AsgiClient(app)


def env(**kwargs: str) -> Mapping[str, str]:
    return {k: v for k, v in kwargs.items()}
