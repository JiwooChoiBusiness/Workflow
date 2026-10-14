import sys
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import uvicorn

from app import cli, config
from app.config import Settings
from app.logsetup import configure_logging
from app.runner import reloader


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[Any]]]:
    recorded: dict[str, list[Any]] = {"uvicorn": [], "reloader": []}
    settings = Settings.model_validate({"server_port": 8599})
    # 실제 .env를 읽지 않게 설정을 바꿔 끼운다.
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: recorded["uvicorn"].append(kwargs))

    def run_reloader(command: Sequence[str], **kwargs: Any) -> int:
        recorded["reloader"].append(list(command))
        return 0

    monkeypatch.setattr(reloader, "run", run_reloader)
    monkeypatch.delenv(reloader.CHILD_ENV, raising=False)
    try:
        yield recorded
    finally:
        configure_logging("INFO")


def test_serve_runs_uvicorn_in_this_process(calls: dict[str, list[Any]]) -> None:
    assert cli.main(["serve"]) == 0

    assert calls["reloader"] == []
    (options,) = calls["uvicorn"]
    assert options["factory"] is True
    assert (options["host"], options["port"]) == ("127.0.0.1", 8599)
    # uvicorn 리로더는 Windows에서 Selector 루프를 써 도구가 돌지 않으므로 쓰지 않는다.
    assert "reload" not in options


def test_serve_reload_hands_over_to_the_watcher(calls: dict[str, list[Any]]) -> None:
    assert cli.main(["serve", "--reload"]) == 0

    assert calls["uvicorn"] == []
    assert calls["reloader"] == [[sys.executable, "-m", "app.cli", "serve", "--reload"]]


def test_reload_child_serves_directly(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(reloader.CHILD_ENV, "1")

    assert cli.main(["serve", "--reload"]) == 0

    assert calls["reloader"] == []
    assert len(calls["uvicorn"]) == 1
