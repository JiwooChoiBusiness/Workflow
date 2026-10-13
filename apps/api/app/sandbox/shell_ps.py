import base64
from pathlib import Path
from typing import Literal

from app.sandbox.base import SandboxError

ShellName = Literal["powershell", "pwsh", "bash"]

# -EncodedCommand는 UTF-16 base64라 명령줄 32K 안에 8,000자 정도만 들어간다(3.6).
ENCODED_COMMAND_MAX_CHARS = 8000

# 시스템 코드 페이지(CP949)로 파이프에 쓰면 한글이 깨지고, 5.1의 > 리다이렉션은 UTF-16 파일을
# 만든다. 콘솔 인코딩 설정은 콘솔이 없는 환경에서 예외가 날 수 있어 try로 감싼다.
POWERSHELL_PREAMBLE = """\
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try {
    $__utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $__utf8
    [Console]::OutputEncoding = $__utf8
    $OutputEncoding = $__utf8
} catch { }
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'
$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'
$PSDefaultParameterValues['Export-Csv:Encoding'] = 'utf8'
Set-Location -LiteralPath $env:SANDBOX_WORKSPACE_CWD
"""

# 네이티브 명령의 종료 코드와 cmdlet 실패를 모두 프로세스 종료 코드로 옮긴다.
POWERSHELL_EPILOGUE = (
    "$__ok = $?; $__code = $LASTEXITCODE; "
    "if ($null -ne $__code) { exit $__code } elseif ($__ok) { exit 0 } else { exit 1 }"
)

BASH_PREAMBLE = """\
set -o pipefail
cd "$SANDBOX_WORKSPACE_CWD" || exit 1
"""

_POWERSHELL_FLAGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]


def effective_shell(configured: ShellName, *, windows: bool) -> ShellName:
    # Windows PowerShell 5.1은 Windows에만 있다. 비Windows(CI)는 bash로 돈다.
    if configured == "powershell" and not windows:
        return "bash"
    return configured


def build_powershell_script(command: str) -> str:
    return f"{POWERSHELL_PREAMBLE}{command}\n{POWERSHELL_EPILOGUE}\n"


def powershell_script_bytes(script: str) -> bytes:
    # 5.1은 BOM이 없으면 시스템 코드 페이지로 읽어 한글 리터럴이 깨진다.
    return script.encode("utf-8-sig")


def encode_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell_file_argv(executable: str, script_path: Path) -> list[str]:
    return [executable, *_POWERSHELL_FLAGS, "-File", str(script_path)]


def powershell_encoded_argv(executable: str, command: str) -> list[str]:
    if len(command) > ENCODED_COMMAND_MAX_CHARS:
        raise SandboxError(
            "command_too_long",
            f"encoded 모드에서는 명령이 {ENCODED_COMMAND_MAX_CHARS}자를 넘을 수 없다"
            f"({len(command)}자). 명령을 나눠 실행한다",
        )
    script = build_powershell_script(command)
    return [executable, *_POWERSHELL_FLAGS, "-EncodedCommand", encode_command(script)]


def build_bash_script(command: str) -> str:
    return f"{BASH_PREAMBLE}{command}\n"


def bash_argv(executable: str, script_path: Path) -> list[str]:
    return [executable, "--noprofile", "--norc", str(script_path)]
