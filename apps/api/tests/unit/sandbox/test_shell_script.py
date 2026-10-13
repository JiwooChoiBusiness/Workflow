import base64
from pathlib import Path

import pytest

from app.sandbox.base import OutputDecoder, SandboxError
from app.sandbox.process import WINDOWS_INHERITED_ENV, build_env
from app.sandbox.shell_ps import (
    ENCODED_COMMAND_MAX_CHARS,
    POWERSHELL_EPILOGUE,
    POWERSHELL_PREAMBLE,
    bash_argv,
    build_bash_script,
    build_powershell_script,
    effective_shell,
    encode_command,
    powershell_encoded_argv,
    powershell_file_argv,
    powershell_script_bytes,
)

POWERSHELL_FLAGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
PLATFORM_VARS = {
    "SANDBOX_WORKSPACE",
    "SANDBOX_WORKSPACE_CWD",
    "HOME",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONNOUSERSITE",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONSAFEPATH",
    "PIP_CACHE_DIR",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_NO_INPUT",
    "MPLCONFIGDIR",
}


def test_powershell_script_wraps_command_verbatim() -> None:
    command = "Get-ChildItem\nWrite-Output '한글' | Out-File a.txt"
    script = build_powershell_script(command)

    assert script.startswith(POWERSHELL_PREAMBLE)
    assert f"\n{command}\n" in script
    assert script.endswith(POWERSHELL_EPILOGUE + "\n")
    for line in (
        "[Console]::OutputEncoding = $__utf8",
        "[Console]::InputEncoding = $__utf8",
        "$OutputEncoding = $__utf8",
        "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'",
        "$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'",
        "$PSDefaultParameterValues['Export-Csv:Encoding'] = 'utf8'",
        "$ProgressPreference = 'SilentlyContinue'",
        "Set-Location -LiteralPath $env:SANDBOX_WORKSPACE_CWD",
    ):
        assert line in POWERSHELL_PREAMBLE


def test_epilogue_maps_native_and_cmdlet_failures() -> None:
    assert "$LASTEXITCODE" in POWERSHELL_EPILOGUE
    assert "exit $__code" in POWERSHELL_EPILOGUE
    assert "elseif ($__ok) { exit 0 } else { exit 1 }" in POWERSHELL_EPILOGUE


def test_script_file_has_utf8_bom() -> None:
    data = powershell_script_bytes("Write-Output '한글'")

    assert data.startswith(b"\xef\xbb\xbf")
    assert data[3:].decode("utf-8") == "Write-Output '한글'"


def test_file_mode_argv(tmp_path: Path) -> None:
    script = tmp_path / ".run" / "call_1.ps1"

    assert powershell_file_argv("powershell.exe", script) == [
        "powershell.exe",
        *POWERSHELL_FLAGS,
        "-File",
        str(script),
    ]


def test_encoded_mode_argv_round_trips() -> None:
    command = "Write-Output '한글'"
    argv = powershell_encoded_argv("powershell.exe", command)

    assert argv[:-2] == ["powershell.exe", *POWERSHELL_FLAGS]
    assert argv[-2] == "-EncodedCommand"
    assert base64.b64decode(argv[-1]).decode("utf-16-le") == build_powershell_script(command)
    assert encode_command("a") == base64.b64encode("a".encode("utf-16-le")).decode()


def test_encoded_mode_rejects_long_commands() -> None:
    powershell_encoded_argv("powershell.exe", "x" * ENCODED_COMMAND_MAX_CHARS)
    with pytest.raises(SandboxError) as info:
        powershell_encoded_argv("powershell.exe", "x" * (ENCODED_COMMAND_MAX_CHARS + 1))

    assert info.value.code == "command_too_long"


def test_bash_script_and_argv(tmp_path: Path) -> None:
    script = build_bash_script("echo 한글")

    assert script.splitlines() == [
        "set -o pipefail",
        'cd "$SANDBOX_WORKSPACE_CWD" || exit 1',
        "echo 한글",
    ]
    path = tmp_path / "x.sh"
    assert bash_argv("/bin/bash", path) == ["/bin/bash", "--noprofile", "--norc", str(path)]


@pytest.mark.parametrize(
    ("configured", "windows", "expected"),
    [
        ("powershell", True, "powershell"),
        ("powershell", False, "bash"),
        ("pwsh", False, "pwsh"),
        ("pwsh", True, "pwsh"),
        ("bash", True, "bash"),
    ],
)
def test_effective_shell(configured: str, windows: bool, expected: str) -> None:
    assert effective_shell(configured, windows=windows) == expected  # type: ignore[arg-type]


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    return {
        "workspace": tmp_path / "ws",
        "cwd": tmp_path / "ws" / "sub",
        "python": tmp_path / "Python312",
        "pip_cache": tmp_path / "data" / "cache" / "pip",
        "venv": tmp_path / "repo" / ".venv" / "Scripts",
    }


def windows_parent(venv: Path) -> dict[str, str]:
    return {
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "SystemDrive": "C:",
        "ComSpec": r"C:\Windows\system32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT",
        "TEMP": r"C:\Users\me\AppData\Local\Temp",
        "TMP": r"C:\Users\me\AppData\Local\Temp",
        "USERPROFILE": r"C:\Users\me",
        "APPDATA": r"C:\Users\me\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
        "ProgramData": r"C:\ProgramData",
        "PSModulePath": r"C:\Windows\system32\WindowsPowerShell\v1.0\Modules",
        "NUMBER_OF_PROCESSORS": "8",
        "PROCESSOR_ARCHITECTURE": "AMD64",
        # uv run이 넣은 venv 경로(대소문자·끝 구분자가 달라도 빠져야 한다)와 일반 경로
        "Path": f"{venv};{str(venv).upper()}\\;C:\\Windows\\system32;;C:\\Tools",
        "GEMINI_API_KEYS": "AIza-not-a-real-key",
        "ENCRYPTION_KEY": "not-a-real-key",
        "VIRTUAL_ENV": "C:\\repo\\.venv",
        "USERNAME": "me",
    }


def test_windows_env_is_built_from_a_short_list(layout: dict[str, Path]) -> None:
    env = build_env(
        workspace=layout["workspace"],
        cwd=layout["cwd"],
        sandbox_python_dir=layout["python"],
        pip_cache_dir=layout["pip_cache"],
        agent_env={"MY_SETTING": "값"},
        parent_env=windows_parent(layout["venv"]),
        windows=True,
        excluded_path_dirs=[layout["venv"]],
    )
    ws = layout["workspace"]

    assert set(env) == {*WINDOWS_INHERITED_ENV, *PLATFORM_VARS, "PATH", "MY_SETTING"}
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["TEMP"] == env["TMP"] == str(ws / ".tmp")
    entries = env["PATH"].split(";")
    # Windows 명령이 Git의 GNU 명령(find, sort 등)보다 먼저 잡히도록 System32가 맨 앞이다.
    # 부모 PATH에 있던 system32는 앞에 이미 있으므로 뒤에서 빠진다.
    assert entries == [
        r"C:\Windows\System32",
        r"C:\Windows",
        str(ws / ".packages" / "bin"),
        str(ws / ".packages" / "Scripts"),
        str(layout["python"]),
        r"C:\Tools",
    ]
    assert env["SANDBOX_WORKSPACE"] == env["HOME"] == str(ws)
    assert env["SANDBOX_WORKSPACE_CWD"] == str(layout["cwd"])
    assert env["PYTHONPATH"] == str(ws / ".packages")
    assert env["PIP_CACHE_DIR"] == str(layout["pip_cache"])
    assert env["MPLCONFIGDIR"] == str(ws / ".cache" / "matplotlib")
    assert env["PYTHONSAFEPATH"] == env["PYTHONUTF8"] == "1"
    assert env["MY_SETTING"] == "값"


def test_windows_path_without_system_root_keeps_workspace_dirs_first(
    layout: dict[str, Path],
) -> None:
    parent = {"Path": r"C:\Git\usr\bin;C:\Tools"}

    env = build_env(
        workspace=layout["workspace"],
        cwd=layout["workspace"],
        sandbox_python_dir=layout["python"],
        pip_cache_dir=layout["pip_cache"],
        agent_env={},
        parent_env=parent,
        windows=True,
    )

    entries = env["PATH"].split(";")
    assert entries[0] == str(layout["workspace"] / ".packages" / "bin")
    assert entries[-2:] == [r"C:\Git\usr\bin", r"C:\Tools"]


def test_agent_env_replaces_platform_names_case_insensitively_on_windows(
    layout: dict[str, Path],
) -> None:
    env = build_env(
        workspace=layout["workspace"],
        cwd=layout["workspace"],
        sandbox_python_dir=layout["python"],
        pip_cache_dir=layout["pip_cache"],
        agent_env={"path": "custom", "pythonpath": "x"},
        parent_env=windows_parent(layout["venv"]),
        windows=True,
    )

    assert env["path"] == "custom"
    assert env["pythonpath"] == "x"
    assert "PATH" not in env
    assert "PYTHONPATH" not in env


def test_posix_env(layout: dict[str, Path]) -> None:
    # POSIX PATH 항목에는 ':'가 없다. Windows에서 돌려도 그렇게 되도록 상대 경로를 쓴다.
    venv = Path("repo") / ".venv" / "bin"
    parent = {
        "PATH": f"{venv}:/usr/local/bin:/usr/bin",
        "HOME": "/home/me",
        "GEMINI_API_KEYS": "AIza-not-a-real-key",
        "LANG": "ko_KR.UTF-8",
    }
    env = build_env(
        workspace=layout["workspace"],
        cwd=layout["workspace"],
        sandbox_python_dir=layout["python"],
        pip_cache_dir=layout["pip_cache"],
        agent_env={},
        parent_env=parent,
        windows=False,
        excluded_path_dirs=[venv],
    )
    ws = layout["workspace"]

    assert set(env) == {*PLATFORM_VARS, "PATH", "LANG", "TMPDIR"}
    # Windows에서 돌리면 임시 경로에 'C:'가 들어가므로 나누지 않고 통째로 비교한다.
    assert env["PATH"] == ":".join(
        [str(ws / ".packages" / "bin"), str(layout["python"]), "/usr/local/bin", "/usr/bin"]
    )
    assert env["LANG"] == "C.UTF-8"
    assert env["TMPDIR"] == str(ws / ".tmp")
    assert env["HOME"] == str(ws)


@pytest.mark.parametrize("agent_env", [{"A=B": "x"}, {"": "x"}, {"X": "a\0b"}, {"Y\0": "a"}])
def test_invalid_agent_env_is_rejected(layout: dict[str, Path], agent_env: dict[str, str]) -> None:
    with pytest.raises(SandboxError) as info:
        build_env(
            workspace=layout["workspace"],
            cwd=layout["workspace"],
            sandbox_python_dir=layout["python"],
            pip_cache_dir=layout["pip_cache"],
            agent_env=agent_env,
            parent_env={},
            windows=False,
        )
    assert info.value.code == "env_invalid"


def decode_in_pieces(data: bytes, size: int) -> str:
    decoder = OutputDecoder()
    text = "".join(decoder.feed(data[i : i + size]) for i in range(0, len(data), size))
    return text + decoder.flush()


@pytest.mark.parametrize("size", [1, 2, 3, 4, 7, 4096])
def test_decoder_joins_characters_split_across_chunks(size: int) -> None:
    data = "한글 출력\r\n두 번째 줄\r\n".encode()

    assert decode_in_pieces(data, size) == "한글 출력\n두 번째 줄\n"


def test_decoder_keeps_lone_carriage_returns() -> None:
    assert decode_in_pieces(b"10%\r50%\r100%\n", 1) == "10%\r50%\r100%\n"
    assert decode_in_pieces(b"end\r", 4) == "end\r"


def test_decoder_replaces_invalid_bytes() -> None:
    assert decode_in_pieces(b"a\xffb", 1) == "a\ufffdb"
    # CP949로 쓴 한글이 섞여 와도 런은 계속된다(12.3).
    assert "\ufffd" in decode_in_pieces("한".encode("cp949"), 4096)
