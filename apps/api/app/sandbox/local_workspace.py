import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import structlog

from app import sysinfo
from app.config import Settings
from app.sandbox.base import (
    ExecEvent,
    ExecExited,
    ExecOutput,
    ExecSpec,
    ExecStarted,
    FileChanges,
    FileContent,
    FileEntry,
    FileStat,
    HttpResult,
    HttpSpec,
    RestoreInfo,
    RunningExec,
    SandboxError,
    SnapshotInfo,
    WorkspaceHandle,
    WorkspaceStats,
)
from app.sandbox.disk import SCAN_BUDGET_SEC, Scanner, measure, scan_changes
from app.sandbox.paths import (
    is_reserved_name,
    relative_posix,
    resolve_cwd,
    resolve_in_workspace,
)
from app.sandbox.process import (
    IS_WINDOWS,
    ProcessLimits,
    ProcessRunner,
    RunningProcess,
    build_env,
    make_runner,
)
from app.sandbox.python_runner import (
    BOOTSTRAP_NAME,
    BOOTSTRAP_SOURCE,
    REQUIREMENTS_PATH,
    merge_requirements,
    pip_install_argv,
    run_python_argv,
    validate_pip_specs,
)
from app.sandbox.shell_ps import (
    bash_argv,
    build_bash_script,
    build_powershell_script,
    effective_shell,
    powershell_encoded_argv,
    powershell_file_argv,
    powershell_script_bytes,
)

if TYPE_CHECKING:
    from app.sandbox.http_client import HttpClient

log = structlog.get_logger("app.sandbox")

ExecKind = Literal["shell", "python", "pip"]

# 작업공간 골격(3.4). 나머지 폴더는 쓰는 쪽이 필요할 때 만든다.
SKELETON_DIRS = ("outputs", "downloads", ".platform", ".run", ".tmp", ".cache")
WORKSPACE_JSON = ".platform/workspace.json"
OUTPUT_CHUNK = 4096
OUTPUT_QUEUE_SIZE = 64
EXIT_POLL_SEC = 0.05
KILL_WAIT_SEC = 5.0
DRAIN_SEC = 2.0
MAX_READ_BYTES = 8 * 1024 * 1024
DELETE_RETRY_DELAYS = (0.2, 0.5, 1.0)
DELETED_PREFIX = ".deleted-"
# 영숫자로 시작해야 .run/_bootstrap.py 같은 플랫폼 파일과 겹치지 않는다.
_SAFE_EXEC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_BINARY_SNIFF = 8192


@dataclass
class _Exec:
    exec_id: str
    kind: ExecKind
    process: RunningProcess
    started_at: datetime
    kill_requested: asyncio.Event = field(default_factory=asyncio.Event)
    killed: bool = False
    timed_out: bool = False

    def request_kill(self) -> None:
        # 종료보다 먼저 표시한다. Job 종료가 즉시라 감시 태스크가 종료 코드를 먼저 볼 수 있다.
        self.killed = True
        self.kill_requested.set()


def _now() -> datetime:
    return datetime.now(UTC)


def _mtime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def _script_stem(exec_id: str) -> str:
    if _SAFE_EXEC_ID.fullmatch(exec_id) and not is_reserved_name(exec_id):
        return exec_id
    return "x" + hashlib.sha256(exec_id.encode("utf-8")).hexdigest()[:16]


def _utf8_cut(chunk: bytes) -> int:
    """끝에서 잘린 UTF-8 문자를 빼고 온전한 부분의 길이."""
    for back in range(1, min(4, len(chunk)) + 1):
        byte = chunk[-back]
        if byte & 0xC0 == 0x80:
            continue
        if byte >= 0xF0:
            need = 4
        elif byte >= 0xE0:
            need = 3
        elif byte >= 0xC0:
            need = 2
        else:
            need = 1
        return len(chunk) - back if back < need else len(chunk)
    return len(chunk)


def _looks_binary(sample: bytes, *, complete: bool) -> bool:
    if b"\0" in sample:
        return True
    end = len(sample) if complete else _utf8_cut(sample)
    try:
        sample[:end].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _read_chunk(target: Path, user_path: str, offset: int, limit: int) -> FileContent:
    if not target.exists():
        raise SandboxError("not_found", f"파일이 없다: {user_path[:80]!r}", status=404)
    if target.is_dir():
        raise SandboxError("is_a_directory", f"디렉터리다: {user_path[:80]!r}")
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_READ_BYTES))
    with target.open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        sample = file.read(_BINARY_SNIFF)
        file.seek(offset)
        chunk = file.read(limit)
    binary = _looks_binary(sample, complete=len(sample) >= size)
    at_eof = offset + len(chunk) >= size
    if binary:
        content = base64.b64encode(chunk).decode("ascii")
        consumed = len(chunk)
    else:
        consumed = len(chunk) if at_eof else _utf8_cut(chunk)
        if consumed == 0:
            # limit이 문자 하나보다 작으면 진행이 멈추지 않게 그대로 내보낸다.
            consumed = len(chunk)
        content = chunk[:consumed].decode("utf-8", errors="replace")
    next_offset = offset + consumed
    truncated = next_offset < size
    return FileContent(
        content=content,
        encoding="base64" if binary else "utf-8",
        size=size,
        truncated=truncated,
        next_offset=next_offset if truncated else None,
    )


def _write(target: Path, user_path: str, data: bytes, append: bool, mkdirs: bool) -> int:
    if target.is_dir():
        raise SandboxError("is_a_directory", f"디렉터리다: {user_path[:80]!r}")
    try:
        if not target.parent.is_dir():
            if not mkdirs:
                raise SandboxError(
                    "not_found", f"상위 디렉터리가 없다: {user_path[:80]!r}", status=404
                )
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if append else "wb") as file:
            file.write(data)
    except OSError as exc:
        raise SandboxError("write_failed", f"쓰지 못했다: {user_path[:80]!r}: {exc}") from exc
    return len(data)


def _clear_readonly(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    # Windows에서 읽기 전용 파일(.git 객체 등)은 속성을 풀어야 지울 수 있다.
    if isinstance(exc, PermissionError) and os.path.lexists(path):
        os.chmod(path, stat.S_IWRITE)
        func(path)
        return
    raise exc


def remove_workspace_dir(
    root: Path,
    *,
    delays: tuple[float, ...] = DELETE_RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> Path | None:
    """작업공간 폴더를 지운다. 잠겨서 못 지우면 .deleted-*로 이름을 바꾸고 그 경로를 돌려준다.

    rmtree는 3.12에서 정션·심볼릭 링크를 따라 들어가지 않고 링크만 지운다.
    """
    for attempt, delay in enumerate((0.0, *delays)):
        if delay:
            sleep(delay)
        try:
            shutil.rmtree(root, onexc=_clear_readonly)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.info("workspace.delete_retry", path=str(root), attempt=attempt, error=str(exc))
    renamed = root.with_name(f"{DELETED_PREFIX}{int(time.time())}-{root.name}")
    try:
        root.rename(renamed)
    except OSError as exc:
        raise SandboxError(
            "workspace_delete_failed", f"작업공간을 지우지 못했다: {exc}", status=409
        ) from exc
    log.warning("workspace.delete_deferred", path=str(root), renamed=str(renamed))
    return renamed


def purge_deleted(workspaces_dir: Path) -> int:
    """하우스키핑용. 이름만 바꿔 둔 .deleted-* 폴더를 다시 지운다. 지운 개수를 돌려준다."""
    removed = 0
    if not workspaces_dir.is_dir():
        return 0
    for path in workspaces_dir.iterdir():
        if not path.name.startswith(DELETED_PREFIX):
            continue
        try:
            shutil.rmtree(path, onexc=_clear_readonly)
        except OSError as exc:
            log.info("workspace.purge_failed", path=str(path), error=str(exc))
            continue
        removed += 1
    return removed


def _record_requirements(root: Path, specs: list[str]) -> None:
    path = root / REQUIREMENTS_PATH
    existing = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    merged = merge_requirements(existing, specs)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text("".join(f"{line}\n" for line in merged), encoding="utf-8")
    os.replace(partial, path)


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    name: Literal["stdout", "stderr"],
    exec_id: str,
    queue: "asyncio.Queue[ExecOutput | None]",
) -> None:
    if stream is None:
        return
    while chunk := await stream.read(OUTPUT_CHUNK):
        await queue.put(ExecOutput(exec_id=exec_id, stream=name, data=chunk))


async def _feed_stdin(proc: asyncio.subprocess.Process, text: str) -> None:
    if proc.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()


async def _wait_returncode(
    proc: asyncio.subprocess.Process, timeout: float, event: asyncio.Event | None = None
) -> bool:
    """returncode가 생길 때까지 기다린다. event가 먼저 켜지거나 시간이 다 되면 False.

    3.12의 Process.wait()는 파이프가 모두 닫혀야 돌아온다. 백그라운드 자손이 stdout을 쥐고 있으면
    본 프로세스가 끝나도 기다림이 끝나지 않으므로 returncode를 직접 본다.
    """
    deadline = time.monotonic() + timeout
    while proc.returncode is None:
        if event is not None and event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(EXIT_POLL_SEC, remaining))
    return True


class LocalWorkspaceBackend:
    name = "local"

    def __init__(
        self,
        settings: Settings,
        *,
        runner: ProcessRunner | None = None,
        http_client: "HttpClient | None" = None,
        workspaces_dir: Path | None = None,
    ) -> None:
        # 워크플로우 code 노드용 두 번째 인스턴스는 DATA_DIR/wf-workspaces를 쓴다(2단계 9.1).
        # 1단계 하우스키핑이 세션 없는 폴더를 지우는 DATA_DIR/workspaces와 섞이지 않게 한다.
        self._workspaces = workspaces_dir or settings.data_dir / "workspaces"
        self._workspaces_resolved: Path | None = None
        self._pip_cache = settings.data_dir / "cache" / "pip"
        self._shell = effective_shell(settings.sandbox_shell, windows=IS_WINDOWS)
        self._shell_mode = settings.sandbox_shell_mode
        self._python = sysinfo.sandbox_python(settings)
        self._memory_limit_mb = settings.sandbox_memory_limit_mb
        self._max_processes = settings.sandbox_max_processes
        self._max_timeout = settings.sandbox_tool_max_timeout_sec
        self._keep_scripts = settings.keep_run_scripts
        self._snapshot_max_bytes = settings.snapshot_max_bytes
        self._allowlist_file = settings.allowlist_file
        self._http_max_body = settings.http_max_body_bytes
        self._runner = runner or make_runner()
        self._http_client = http_client
        self._active: dict[str, dict[str, _Exec | None]] = {}
        # 프로세스가 뜨기 전에 들어온 kill. 뜨자마자 끝낸다.
        self._early_kills: set[tuple[str, str]] = set()
        self._last_exec_at: dict[str, datetime] = {}
        # uv run이 PATH 앞에 넣는 플랫폼 venv 경로. 서버 의존성이 도구에 보이지 않게 뺀다(S15).
        self._venv_dirs: tuple[Path, ...] = (
            (Path(sys.prefix) / "Scripts", Path(sys.prefix) / "bin")
            if sys.prefix != sys.base_prefix
            else ()
        )

    @property
    def workspaces_dir(self) -> Path:
        return self._workspaces

    async def _kill_all(self, key: str) -> None:
        for exec_id, state in list(self._active.get(key, {}).items()):
            if state is None:
                self._early_kills.add((key, exec_id))
            elif state.process.proc.returncode is None:
                state.request_kill()
                await self._runner.kill(state.process)

    async def aclose(self) -> None:
        for key in list(self._active):
            await self._kill_all(key)
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def purge_deleted(self) -> int:
        return await asyncio.to_thread(purge_deleted, self._workspaces)

    def _workspace_root(self, session_id: UUID) -> Path:
        if self._workspaces_resolved is None:
            self._workspaces.mkdir(parents=True, exist_ok=True)
            self._workspaces_resolved = self._workspaces.resolve()
        return self._workspaces_resolved / str(session_id)

    def _root(self, h: WorkspaceHandle, *, must_exist: bool = True) -> Path:
        if h.backend != "local" or h.root != self._workspace_root(h.session_id):
            raise SandboxError("workspace_invalid", "이 백엔드가 만든 작업공간 핸들이 아니다")
        if must_exist and not h.root.is_dir():
            raise SandboxError("workspace_missing", "작업공간 폴더가 없다", status=404)
        return h.root

    def _prepare_workspace(self, session_id: UUID) -> Path:
        root = self._workspace_root(session_id)
        root.mkdir(exist_ok=True)
        for name in SKELETON_DIRS:
            (root / name).mkdir(exist_ok=True)
        info = root / WORKSPACE_JSON
        if not info.exists():
            data = {
                "session_id": str(session_id),
                "created_at": _now().isoformat(),
                "restored_from_snapshot": None,
            }
            info.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._ensure_bootstrap(root)
        return root.resolve()

    def _ensure_bootstrap(self, root: Path) -> Path:
        bootstrap = root / ".run" / BOOTSTRAP_NAME
        if not bootstrap.is_file() or bootstrap.read_text(encoding="utf-8") != BOOTSTRAP_SOURCE:
            bootstrap.parent.mkdir(parents=True, exist_ok=True)
            bootstrap.write_text(BOOTSTRAP_SOURCE, encoding="utf-8")
        return bootstrap

    def _update_workspace_json(self, root: Path, **fields: object) -> None:
        info = root / WORKSPACE_JSON
        try:
            data: dict[str, object] = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.update(fields)
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def acquire(self, session_id: UUID) -> WorkspaceHandle:
        root = await asyncio.to_thread(self._prepare_workspace, session_id)
        return WorkspaceHandle(session_id=session_id, root=root, backend="local")

    async def release(self, h: WorkspaceHandle, *, delete: bool) -> None:
        root = self._root(h, must_exist=False)
        await self._kill_all(str(root))
        if delete:
            await asyncio.to_thread(remove_workspace_dir, root)

    async def kill(self, h: WorkspaceHandle, exec_id: str) -> bool:
        root = self._root(h, must_exist=False)
        active = self._active.get(str(root), {})
        if exec_id not in active:
            return False
        state = active[exec_id]
        if state is None:
            self._early_kills.add((str(root), exec_id))
            return True
        if state.process.proc.returncode is not None:
            return False
        state.request_kill()
        await self._runner.kill(state.process)
        return True

    def _limits(self, spec: ExecSpec) -> ProcessLimits:
        memory_mb = self._memory_limit_mb
        if spec.memory_limit_mb is not None:
            memory_mb = max(1, min(memory_mb, spec.memory_limit_mb))
        processes = self._max_processes
        if spec.max_processes is not None:
            processes = max(1, min(processes, spec.max_processes))
        return ProcessLimits(memory_bytes=memory_mb * 2**20, max_processes=processes)

    def _shell_argv(self, root: Path, spec: ExecSpec, stem: str) -> tuple[list[str], list[Path]]:
        executable = shutil.which(self._shell)
        if executable is None:
            raise SandboxError("sandbox_unavailable", f"셸 {self._shell}을 찾지 못했다")
        run_dir = root / ".run"
        if self._shell == "bash":
            script = run_dir / f"{stem}.sh"
            script.write_text(build_bash_script(spec.script), encoding="utf-8", newline="\n")
            return bash_argv(executable, script), [script]
        if self._shell_mode == "encoded":
            return powershell_encoded_argv(executable, spec.script), []
        script = run_dir / f"{stem}.ps1"
        script.write_bytes(powershell_script_bytes(build_powershell_script(spec.script)))
        return powershell_file_argv(executable, script), [script]

    def _prepare_exec(self, root: Path, spec: ExecSpec) -> tuple[list[str], list[Path]]:
        for name in (".run", ".tmp", ".cache"):
            (root / name).mkdir(exist_ok=True)
        stem = _script_stem(spec.exec_id)
        if spec.kind == "shell":
            return self._shell_argv(root, spec, stem)
        if not self._python.is_file():
            raise SandboxError("sandbox_unavailable", f"샌드박스 파이썬이 없다: {self._python}")
        if spec.kind == "python":
            script = root / ".run" / f"{stem}.py"
            script.write_text(spec.script, encoding="utf-8")
            bootstrap = self._ensure_bootstrap(root)
            return run_python_argv(self._python, bootstrap, script), [script]
        specs = validate_pip_specs(spec.packages)
        return pip_install_argv(self._python, root / ".packages", specs), []

    async def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        root = self._root(h)
        key = str(root)
        active = self._active.setdefault(key, {})
        if spec.exec_id in active:
            raise SandboxError(
                "exec_conflict", f"같은 exec_id가 이미 실행 중이다: {spec.exec_id}", status=409
            )
        # 첫 await 전에 자리를 잡아 같은 exec_id가 동시에 들어오지 못하게 한다.
        active[spec.exec_id] = None
        state: _Exec | None = None
        scripts: list[Path] = []
        try:
            specs = validate_pip_specs(spec.packages) if spec.kind == "pip" else []
            cwd = resolve_cwd(root, spec.cwd)
            argv, scripts = await asyncio.to_thread(self._prepare_exec, root, spec)
            env = build_env(
                workspace=root,
                cwd=cwd,
                sandbox_python_dir=self._python.parent,
                pip_cache_dir=self._pip_cache,
                agent_env=spec.env,
                parent_env=os.environ,
                windows=IS_WINDOWS,
                excluded_path_dirs=self._venv_dirs,
            )
            timeout = max(1, min(spec.timeout_sec, self._max_timeout))
            process = await self._runner.spawn(
                argv, cwd=cwd, env=env, stdin=spec.stdin is not None, limits=self._limits(spec)
            )
            state = _Exec(spec.exec_id, spec.kind, process, _now())
            active[spec.exec_id] = state
            self._last_exec_at[key] = state.started_at
            log.info("sandbox.exec_started", exec_id=spec.exec_id, kind=spec.kind, pid=process.pid)
            if (key, spec.exec_id) in self._early_kills:
                state.request_kill()
                await self._runner.kill(process)
            yield ExecStarted(exec_id=spec.exec_id, pid=process.pid)

            # 소비자가 중간에 그만두면 안쪽 제너레이터도 바로 닫아 리더·감시 태스크를 거둔다.
            async with contextlib.aclosing(self._drive(state, spec, timeout)) as events:
                async for event in events:
                    yield event

            code = process.proc.returncode
            if state.timed_out or state.killed:
                code = None
            peak = self._runner.stats(process).peak_memory_bytes
            if spec.kind == "pip" and code == 0:
                await asyncio.to_thread(_record_requirements, root, specs)
            duration_ms = int((time.monotonic() - process.started_at) * 1000)
            log.info(
                "sandbox.exec_finished",
                exec_id=spec.exec_id,
                code=code,
                timed_out=state.timed_out,
                killed=state.killed,
                duration_ms=duration_ms,
            )
            yield ExecExited(
                exec_id=spec.exec_id,
                code=code,
                timed_out=state.timed_out,
                killed=state.killed,
                duration_ms=duration_ms,
                peak_memory_bytes=peak,
            )
        finally:
            if state is not None:
                # 소비자가 중간에 그만두면(aclose, 취소) 본 프로세스가 끝났어도 자손이 남을 수 있다.
                # 이미 다 끝났으면 곧바로 돌아온다.
                await self._runner.kill(state.process)
                self._runner.close(state.process)
            active.pop(spec.exec_id, None)
            self._early_kills.discard((key, spec.exec_id))
            if not self._keep_scripts:
                for script in scripts:
                    with contextlib.suppress(OSError):
                        script.unlink(missing_ok=True)

    async def _drive(
        self, state: _Exec, spec: ExecSpec, timeout: int
    ) -> AsyncGenerator[ExecOutput, None]:
        proc = state.process.proc
        queue: asyncio.Queue[ExecOutput | None] = asyncio.Queue(maxsize=OUTPUT_QUEUE_SIZE)
        readers = [
            asyncio.create_task(_pump_stream(proc.stdout, "stdout", spec.exec_id, queue)),
            asyncio.create_task(_pump_stream(proc.stderr, "stderr", spec.exec_id, queue)),
        ]
        tasks = [*readers]
        if spec.stdin is not None:
            tasks.append(asyncio.create_task(_feed_stdin(proc, spec.stdin)))
        supervisor = asyncio.create_task(self._supervise(state, timeout, readers, queue))
        tasks.append(supervisor)
        try:
            while (item := await queue.get()) is not None:
                yield item
            await supervisor
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(
        self,
        state: _Exec,
        timeout: int,
        readers: list[asyncio.Task[None]],
        queue: "asyncio.Queue[ExecOutput | None]",
    ) -> None:
        proc = state.process.proc
        try:
            exited = await _wait_returncode(proc, timeout, state.kill_requested)
            if not exited and not state.killed:
                state.timed_out = True
            # 본 프로세스가 끝나도 백그라운드 자손(Start-Process, &)이 남을 수 있다. 도구 호출이
            # 끝나면 트리 전체를 끝낸다. 자손이 파이프를 쥐고 있으면 리더가 EOF를 받지 못한다.
            await self._runner.kill(state.process)
            await _wait_returncode(proc, KILL_WAIT_SEC)
            _done, pending = await asyncio.wait(readers, timeout=DRAIN_SEC)
            for task in pending:
                task.cancel()
        finally:
            await queue.put(None)

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> FileContent:
        root = self._root(h)
        target = resolve_in_workspace(root, path)
        return await asyncio.to_thread(_read_chunk, target, path, offset, limit)

    async def write_file(
        self,
        h: WorkspaceHandle,
        path: str,
        data: bytes,
        *,
        append: bool = False,
        mkdirs: bool = True,
    ) -> int:
        root = self._root(h)
        target = resolve_in_workspace(root, path, for_write=True)
        return await asyncio.to_thread(_write, target, path, data, append, mkdirs)

    def _list(
        self,
        root: Path,
        target: Path,
        user_path: str,
        recursive: bool,
        max_entries: int,
        hidden: bool,
    ) -> list[FileEntry]:
        if not target.exists():
            raise SandboxError("not_found", f"경로가 없다: {user_path[:80]!r}", status=404)
        if not target.is_dir():
            raise SandboxError("not_a_directory", f"디렉터리가 아니다: {user_path[:80]!r}")
        prefix = relative_posix(root, target)
        entries: list[FileEntry] = []
        scanner = Scanner(
            target, budget_sec=SCAN_BUDGET_SEC, include_hidden=hidden, recursive=recursive
        )
        for entry in scanner:
            if len(entries) >= max_entries:
                break
            entries.append(
                FileEntry(
                    path=entry.rel if prefix == "." else f"{prefix}/{entry.rel}",
                    type="link" if entry.is_link else "dir" if entry.is_dir else "file",
                    size=entry.size,
                    mtime=_mtime(entry.mtime),
                )
            )
        return entries

    async def list_files(
        self,
        h: WorkspaceHandle,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 500,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        root = self._root(h)
        target = resolve_in_workspace(root, path)
        return await asyncio.to_thread(
            self._list, root, target, path, recursive, max(0, max_entries), include_hidden
        )

    async def stat(self, h: WorkspaceHandle, path: str) -> FileStat:
        root = self._root(h)
        target = resolve_in_workspace(root, path)
        try:
            info = await asyncio.to_thread(target.stat)
        except FileNotFoundError as exc:
            raise SandboxError("not_found", f"경로가 없다: {path[:80]!r}", status=404) from exc
        return FileStat(
            type="dir" if stat.S_ISDIR(info.st_mode) else "file",
            size=0 if stat.S_ISDIR(info.st_mode) else info.st_size,
            mtime=_mtime(info.st_mtime),
        )

    async def changes(
        self, h: WorkspaceHandle, since: datetime, *, max_entries: int = 1000
    ) -> FileChanges:
        root = self._root(h)
        found, truncated = await asyncio.to_thread(
            scan_changes, root, since.timestamp(), max_entries=max(0, max_entries)
        )
        return FileChanges(
            entries=[
                FileEntry(path=entry.rel, type="file", size=entry.size, mtime=_mtime(entry.mtime))
                for entry in found
            ],
            truncated=truncated,
        )

    def _http(self) -> "HttpClient":
        if self._http_client is None:
            from app.sandbox.allowlist import Allowlist
            from app.sandbox.http_client import HttpClient

            self._http_client = HttpClient(
                Allowlist(self._allowlist_file), max_body_bytes=self._http_max_body
            )
        return self._http_client

    async def http_request(self, h: WorkspaceHandle, spec: HttpSpec) -> HttpResult:
        root = self._root(h)
        return await self._http().request(spec, workspace_root=root)

    async def snapshot(self, h: WorkspaceHandle, dest: Path) -> SnapshotInfo:
        root = self._root(h)
        from app.sandbox.snapshot import create_snapshot

        return await asyncio.to_thread(
            create_snapshot, root, dest, max_bytes=self._snapshot_max_bytes
        )

    async def restore(
        self, h: WorkspaceHandle, src: Path, *, expected_sha256: str | None = None
    ) -> RestoreInfo:
        root = self._root(h)
        from app.sandbox.snapshot import restore_snapshot

        count, raw, digest, requirements = await asyncio.to_thread(
            restore_snapshot, root, src, expected_sha256=expected_sha256
        )
        await asyncio.to_thread(self._update_workspace_json, root, restored_from_snapshot=src.stem)
        return RestoreInfo(
            file_count=count, raw_bytes=raw, sha256=digest, requirements=requirements
        )

    async def stats(self, h: WorkspaceHandle) -> WorkspaceStats:
        root = self._root(h)
        usage = await asyncio.to_thread(measure, root)
        running: list[RunningExec] = []
        active_processes = 0
        now = time.monotonic()
        for state in self._active.get(str(root), {}).values():
            if state is None:
                continue
            process_stats = self._runner.stats(state.process)
            running.append(
                RunningExec(
                    exec_id=state.exec_id,
                    kind=state.kind,
                    pid=state.process.pid,
                    started_at=state.started_at,
                    elapsed_ms=int((now - state.process.started_at) * 1000),
                    peak_memory_bytes=process_stats.peak_memory_bytes,
                    active_processes=process_stats.active_processes,
                )
            )
            active_processes += process_stats.active_processes or 1
        return WorkspaceStats(
            disk_bytes=usage.total_bytes,
            disk_estimated=usage.estimated,
            active_processes=active_processes,
            last_exec_at=self._last_exec_at.get(str(root)),
            running=running,
        )
