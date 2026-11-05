# e2e 잡에서 서버를 띄우기 전에 샌드박스 셸 실행 시간을 잰다. e2e의 셸 도구 시험이 CI에서만 도구
# 시간 상한까지 멈춰서, 서버와 같은 bash 스텝 환경에서 서버와 같은 경로(LocalWorkspaceBackend)로
# 돌려 본다.
#
#   python scripts/ci-sandbox-probe.py <빈 데이터 폴더>
#
# 서버 기본 설정(셸 powershell, 모드 file)으로 작업공간을 하나 만들어 `echo fake-llm`을 두 번
# 실행하고, Windows면 비교로 최소 환경 없이 이 프로세스 환경 그대로 powershell -Command도 한 번
# 돌린다. PSModulePath 항목, LOCALAPPDATA 유무, 데이터 폴더 드라이브도 적는다. 결과는 표준 출력과
# ::notice 주석으로 낸다. 진단용이라 점검이 실패해도 종료 코드는 0이다.
import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.logsetup import configure_logging
from app.sandbox.base import (
    ExecExited,
    ExecOutput,
    ExecSpec,
    ExecStarted,
    OutputDecoder,
    WorkspaceHandle,
)
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.sandbox.process import IS_WINDOWS
from app.sandbox.shell_ps import effective_shell

TITLE = "샌드박스 셸 점검"
COMMAND = "echo fake-llm"
TIMEOUT_SEC = 300
KILL_WAIT_SEC = 10
TAIL_CHARS = 300


@dataclass
class Measured:
    seconds: float = 0.0
    code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    # 샌드박스 실행만 채운다. started·first_output은 exec를 부른 때부터 센 초다.
    started: float | None = None
    first_output: float | None = None
    duration_ms: int | None = None

    def lines(self, name: str) -> list[str]:
        parts = [f"{self.seconds:.2f}초", f"코드 {self.code}", f"timed_out {self.timed_out}"]
        if self.duration_ms is not None:
            parts.append(f"duration_ms {self.duration_ms}")
        if self.started is not None:
            parts.append(f"프로세스 시작 {self.started:.2f}초")
            first = "없음" if self.first_output is None else f"{self.first_output:.2f}초"
            parts.append(f"첫 출력 {first}")
        result = [f"{name}: {', '.join(parts)}"]
        if self.error:
            result.append(f"  오류: {self.error}")
        result.append(f"  stdout 끝: {self.stdout or '(없음)'}")
        result.append(f"  stderr 끝: {self.stderr or '(없음)'}")
        return result


def tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= TAIL_CHARS else "…" + text[-TAIL_CHARS:]


def decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


async def sandbox_exec(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, index: int
) -> Measured:
    spec = ExecSpec(exec_id=f"probe-{index}", kind="shell", script=COMMAND, timeout_sec=TIMEOUT_SEC)
    decoders = {"stdout": OutputDecoder(), "stderr": OutputDecoder()}
    text = {"stdout": "", "stderr": ""}
    result = Measured()
    begin = time.monotonic()
    try:
        async for event in backend.exec(handle, spec):
            if isinstance(event, ExecStarted):
                result.started = time.monotonic() - begin
            elif isinstance(event, ExecOutput):
                if result.first_output is None:
                    result.first_output = time.monotonic() - begin
                text[event.stream] += decoders[event.stream].feed(event.data)
            elif isinstance(event, ExecExited):
                result.code = event.code
                result.timed_out = event.timed_out
                result.duration_ms = event.duration_ms
    except Exception as exc:
        result.error = describe(exc)
    result.seconds = time.monotonic() - begin
    result.stdout = tail(text["stdout"] + decoders["stdout"].flush())
    result.stderr = tail(text["stderr"] + decoders["stderr"].flush())
    return result


async def probe_sandbox(settings: Settings, results: list[Measured]) -> None:
    backend = LocalWorkspaceBackend(settings)
    try:
        handle = await backend.acquire(uuid4())
        for index in (1, 2):
            results.append(await sandbox_exec(backend, handle, index))
        await backend.release(handle, delete=True)
    finally:
        await backend.aclose()


def plain_powershell() -> Measured:
    executable = shutil.which("powershell")
    if executable is None:
        return Measured(error="powershell을 찾지 못했다")
    argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "echo hi"]
    begin = time.monotonic()
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return Measured(error=describe(exc))
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        # subprocess.run은 kill 뒤 파이프가 닫힐 때까지 상한 없이 기다린다. 자손이 파이프를 쥐고
        # 있으면 이 스텝이 끝나지 않아 뒤의 e2e 스텝이 돌지 못하므로 여기서도 상한을 둔다.
        try:
            stdout, stderr = process.communicate(timeout=KILL_WAIT_SEC)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
    return Measured(
        seconds=time.monotonic() - begin,
        code=None if timed_out else process.returncode,
        timed_out=timed_out,
        stdout=tail(decode(stdout)),
        stderr=tail(decode(stderr)),
    )


def environment(settings: Settings | None, data_dir: Path) -> list[str]:
    lines: list[str] = []
    if settings is not None:
        shell = effective_shell(settings.sandbox_shell, windows=IS_WINDOWS)
        found = shutil.which(shell) or "찾지 못함"
        lines.append(f"셸 {shell} ({found}), 모드 {settings.sandbox_shell_mode}")
    lines.append(f"데이터 폴더 드라이브 {data_dir.drive or data_dir.anchor}")
    lines.append(f"LOCALAPPDATA {'있음' if 'LOCALAPPDATA' in os.environ else '없음'}")
    entries = [entry for entry in os.environ.get("PSModulePath", "").split(os.pathsep) if entry]
    lines.append(f"PSModulePath {len(entries)}개")
    lines.extend(f"  {entry}" for entry in entries)
    return lines


def notice(lines: list[str]) -> str:
    text = "\n".join(lines).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::notice title={TITLE}::{text}"


def main() -> int:
    if len(sys.argv) != 2:
        print("python scripts/ci-sandbox-probe.py <빈 데이터 폴더>", file=sys.stderr)
        return 2
    data_dir = Path(sys.argv[1]).resolve()
    settings: Settings | None = None
    results: list[Measured] = []
    failure = ""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # 저장소의 .env(실제 키와 로컬 설정)는 읽지 않는다. CI에는 .env가 없어 서버 설정과 같다.
        # _env_file는 실행 시에 받는 인자라 pyright가 보는 시그니처에는 없다.
        settings = Settings(_env_file=None, data_dir=data_dir)  # pyright: ignore[reportCallIssue]
        # 서버처럼 JSON 로그를 켜 두어 sandbox.exec_* 기록이 서버 로그와 같은 모양으로 표준 오류에
        # 남게 한다.
        configure_logging(settings.log_level, secrets=settings.secret_values())
        asyncio.run(probe_sandbox(settings, results))
    except Exception as exc:
        failure = describe(exc)

    report = [f"{COMMAND} 두 번, timeout {TIMEOUT_SEC}초"]
    for index, result in enumerate(results, 1):
        report.extend(result.lines(f"exec {index}"))
    if failure:
        report.append(f"점검 실패: {failure}")
    if IS_WINDOWS:
        try:
            plain = plain_powershell()
        except Exception as exc:
            plain = Measured(error=describe(exc))
        report.extend(plain.lines('비교 powershell -Command "echo hi"(환경 그대로)'))
    else:
        report.append("비교 powershell 실행: Windows가 아니라 건너뜀")
    facts = environment(settings, data_dir)

    print(TITLE)
    print("\n".join(report))
    print("\n".join(facts))
    print(notice(report))
    print(notice(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
