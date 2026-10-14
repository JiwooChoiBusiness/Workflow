import asyncio
import base64
import hashlib
import os
import secrets
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


def test_encryption_key_check_shows_only_a_fingerprint() -> None:
    raw = secrets.token_bytes(32)
    value = base64.b64encode(raw).decode()
    # crypto.SecretCipher처럼 앞뒤 공백은 무시한다.
    settings = Settings.model_validate({"encryption_key": f" {value}\n"})

    check = sysinfo.check_encryption_key(settings)

    fingerprint = hashlib.sha256(raw).hexdigest()[:8]
    assert sysinfo.encryption_key_fingerprint(settings) == fingerprint
    assert check == sysinfo.Check("암호화 키", True, f"설정됨, fingerprint {fingerprint}")
    assert value not in check.detail


@pytest.mark.parametrize(
    "value", [None, "not base64!", base64.b64encode(b"sixteen-byte-key").decode()]
)
def test_missing_or_malformed_encryption_key_is_a_warning(value: str | None) -> None:
    settings = Settings.model_validate({"encryption_key": value})

    assert not sysinfo.check_encryption_key(settings).ok
    assert sysinfo.encryption_key_fingerprint(settings) is None


def test_doctor_ends_with_the_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def subprocess_ok() -> sysinfo.Check:
        return sysinfo.Check("서브프로세스", True, "-")

    # PowerShell·pip를 띄우는 점검은 빼고 목록 구성만 본다.
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    monkeypatch.setattr(
        sysinfo, "check_execution_policy", lambda _settings: sysinfo.Check("실행 정책", True, "-")
    )
    monkeypatch.setattr(sysinfo, "check_subprocess", subprocess_ok)

    checks = sysinfo.doctor_checks(Settings.model_validate({}))

    assert [check.name for check in checks] == ["실행 정책", "포트", "서브프로세스", "암호화 키"]


@pytest.mark.skipif(sys.platform != "win32", reason="확장 길이 접두는 Windows 경로에만 붙는다")
def test_long_path_reaches_past_260_characters_on_windows(tmp_path: Path) -> None:
    top = tmp_path / ("d" * 120)
    deep = top / ("e" * 120) / "f.txt"
    try:
        os.makedirs(sysinfo.long_path(deep.parent))
        with open(sysinfo.long_path(deep), "w", encoding="utf-8") as file:
            file.write("x")

        assert len(str(deep)) > 260
        assert sysinfo.long_path(deep) == "\\\\?\\" + str(deep)
        assert os.path.getsize(sysinfo.long_path(deep)) == 1
    finally:
        shutil.rmtree(sysinfo.long_path(top), ignore_errors=True)
    assert sysinfo.long_path(Path(r"\\server\share\x")) == r"\\?\UNC\server\share\x"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows가 아닐 때의 동작")
def test_long_path_is_just_absolute_elsewhere(tmp_path: Path) -> None:
    assert sysinfo.long_path(tmp_path / "a" / ".." / "b") == str(tmp_path / "b")
