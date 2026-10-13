import sys
from typing import Any

import pytest
import uvicorn

from app import cli
from app.config import get_settings
from app.logging import configure_logging


def test_serve_warns_about_tools_only_with_reload_on_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    get_settings.cache_clear()
    try:
        assert cli.main(["serve", "--reload"]) == 0
        reload_err = capsys.readouterr().err
        assert cli.main(["serve"]) == 0
        plain_err = capsys.readouterr().err
    finally:
        get_settings.cache_clear()
        configure_logging("INFO")

    assert ("--reload" in reload_err) == (sys.platform == "win32")
    assert plain_err == ""
    assert [(call["reload"], call.get("loop", "auto")) for call in calls] == [
        (True, "auto"),
        (False, "auto"),
    ]
