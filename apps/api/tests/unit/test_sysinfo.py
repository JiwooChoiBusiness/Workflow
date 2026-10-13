import asyncio
import shutil
import socket
import sys
from pathlib import Path

import pytest

from app import sysinfo
from app.config import Settings


def test_rss_bytes_reports_this_process() -> None:
    rss = sysinfo.rss_bytes()

    assert rss is not None
    assert 10 * 2**20 < rss < 4 * 2**30


def test_rss_grows_with_allocation() -> None:
    before = sysinfo.rss_bytes()
    # 0으로 채운 할당은 페이지가 커밋되지 않아 RSS에 안 잡힐 수 있으므로 값을 써 넣는다.
    block = b"\x01" * (64 * 2**20)
    after = sysinfo.rss_bytes()

    assert before is not None and after is not None
    assert after - before > 32 * 2**20
    del block


@pytest.mark.skipif(sys.platform != "win32", reason="레지스트리 정책은 Windows에만 있다")
def test_long_paths_policy_is_read_on_windows() -> None:
    assert sysinfo.long_paths_enabled() in (True, False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows가 아닐 때의 동작")
def test_long_paths_policy_is_not_applicable_elsewhere() -> None:
    assert sysinfo.long_paths_enabled() is None
    assert sysinfo.check_long_paths(Settings.model_validate({})).ok


def test_default_sandbox_python_is_base_interpreter() -> None:
    python = sysinfo.default_sandbox_python()

    assert python.is_file()
    assert python.is_relative_to(Path(sys.base_prefix))


def test_data_dir_length_check(tmp_path: Path) -> None:
    short = Settings.model_validate({"data_dir": Path(tmp_path.anchor) / "wf-data"})
    long = Settings.model_validate({"data_dir": tmp_path / ("x" * sysinfo.DATA_DIR_MAX_LENGTH)})

    assert sysinfo.check_data_dir(short).ok
    assert not sysinfo.check_data_dir(long).ok


def test_port_check_detects_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        assert not sysinfo.check_port(port).ok


def test_shell_check_follows_path_lookup() -> None:
    for shell in ("powershell", "pwsh", "bash"):
        settings = Settings.model_validate({"sandbox_shell": shell})

        assert sysinfo.check_shell(settings).ok == (shutil.which(shell) is not None), shell


async def test_subprocess_check_passes_on_the_running_loop() -> None:
    check = await sysinfo.check_subprocess()

    assert check.ok, check.detail


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows의 SelectorEventLoop만 서브프로세스를 못 띄운다"
)
def test_subprocess_check_fails_on_the_windows_selector_loop() -> None:
    # uvicorn --reload가 Windows에서 쓰는 루프다.
    loop = asyncio.SelectorEventLoop()
    try:
        check = loop.run_until_complete(sysinfo.check_subprocess())
    finally:
        loop.close()

    assert not check.ok
    assert "SelectorEventLoop" in check.detail
