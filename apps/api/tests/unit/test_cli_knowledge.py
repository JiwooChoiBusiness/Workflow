"""platform knowledge rebuild-index의 안전장치.

실제로 다시 만드는 흐름은 integration/test_knowledge_ingest.py가 본다.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app import cli, config, sysinfo
from app.config import Settings
from app.logsetup import configure_logging


@pytest.fixture
def index_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    settings = Settings.model_validate({"data_dir": tmp_path, "server_port": 8599})
    # 실제 .env를 읽지 않게 설정을 바꿔 끼운다.
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    path = tmp_path / "knowledge" / "index.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old index")
    try:
        yield path
    finally:
        configure_logging("INFO")


def test_refuses_while_the_server_is_up(
    index_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sysinfo, "check_port", lambda port: sysinfo.Check("포트", False, "-"))

    assert cli.main(["knowledge", "rebuild-index", "--yes"]) == 1

    assert "서버를 끄고" in capsys.readouterr().err
    assert index_file.read_bytes() == b"old index"


def test_stops_unless_confirmed(
    index_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sysinfo, "check_port", lambda port: sysinfo.Check("포트", True, "-"))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert cli.main(["knowledge", "rebuild-index"]) == 1

    assert "그만뒀다" in capsys.readouterr().out
    assert index_file.read_bytes() == b"old index"


def test_needs_app_db(
    index_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sysinfo, "check_port", lambda port: sysinfo.Check("포트", True, "-"))

    assert cli.main(["knowledge", "rebuild-index", "--yes"]) == 1

    assert "app.db가 없다" in capsys.readouterr().err
    # app.db가 없으면 지우기 전에 멈춘다.
    assert index_file.read_bytes() == b"old index"
