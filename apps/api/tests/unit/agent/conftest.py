"""루프 테스트용 가짜 백엔드·포트·하네스.

--import-mode=importlib에서는 테스트 모듈이 conftest를 import할 수 없어 픽스처로 넘긴다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agent.child_runs import ChildCall, ChildStarted, ChildStartError
from app.agent.context import StepMessage, StoredMessage, build_history, decode_step, encode_step
from app.agent.interactions import ResumePayload, effective_approval_mode
from app.agent.loop import AgentLoop, FileSummary, LoopConfig, LoopOutcome, ResumeState
from app.agent.tools import tools_for_groups
from app.agent.tools.base import Tool
from app.llm.fake_adapter import FakeAdapter, FakeTurn
from app.llm.types import ModelParams, TextBlock
from app.runner.supervisor import CancelSignal
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
    SandboxError,
    SnapshotInfo,
    WorkspaceHandle,
    WorkspaceStats,
)
from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import EVENT_DATA_MODELS, MessageFileData, UsageTotal
from app.streaming.event_bus import jsonable


class FakeBackend:
    """메모리 위 작업공간. 셸은 스크립트 글자에 따라 정해 둔 출력·종료 코드를 낸다."""

    name = "fake"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.execs: list[ExecSpec] = []
        self.http: list[HttpSpec] = []
        self.killed: list[str] = []
        self.outputs: dict[str, tuple[str, int]] = {}
        self.hang: set[str] = set()
        self.missing = False
        self.unavailable = False
        self.started = asyncio.Event()
        self._kills: dict[str, asyncio.Event] = {}

    async def acquire(self, session_id: uuid.UUID) -> WorkspaceHandle:
        return WorkspaceHandle(session_id=session_id, root=Path("/ws"), backend="local")

    async def release(self, h: WorkspaceHandle, *, delete: bool) -> None:
        return None

    async def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        if self.missing:
            raise SandboxError("workspace_missing", "작업공간 폴더가 없다", status=404)
        if self.unavailable:
            raise SandboxError("sandbox_unavailable", "셸을 찾지 못했다")
        self.execs.append(spec)
        kill = self._kills.setdefault(spec.exec_id, asyncio.Event())
        yield ExecStarted(exec_id=spec.exec_id, pid=4242)
        self.started.set()
        key = spec.script or " ".join(spec.packages)
        if key in self.hang:
            yield ExecOutput(exec_id=spec.exec_id, stream="stdout", data=b"working\n")
            await kill.wait()
            yield ExecExited(
                exec_id=spec.exec_id,
                code=None,
                timed_out=False,
                killed=True,
                duration_ms=5,
                peak_memory_bytes=None,
            )
            return
        text, code = self.outputs.get(key, (f"ran: {key}\n", 0))
        if text:
            yield ExecOutput(exec_id=spec.exec_id, stream="stdout", data=text.encode("utf-8"))
        yield ExecExited(
            exec_id=spec.exec_id,
            code=code,
            timed_out=False,
            killed=False,
            duration_ms=3,
            peak_memory_bytes=1024,
        )

    async def kill(self, h: WorkspaceHandle, exec_id: str) -> bool:
        self.killed.append(exec_id)
        event = self._kills.get(exec_id)
        if event is None:
            return False
        event.set()
        return True

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> FileContent:
        if self.missing:
            raise SandboxError("workspace_missing", "작업공간 폴더가 없다", status=404)
        if path.startswith(".."):
            raise SandboxError("path_outside_workspace", f"작업공간 밖을 가리킨다: {path!r}")
        if path not in self.files:
            raise SandboxError("not_found", f"파일이 없다: {path!r}", status=404)
        data = self.files[path]
        chunk = data[offset : offset + limit]
        end = offset + len(chunk)
        return FileContent(
            content=chunk.decode("utf-8", errors="replace"),
            encoding="utf-8",
            size=len(data),
            truncated=end < len(data),
            next_offset=end if end < len(data) else None,
        )

    async def write_file(
        self,
        h: WorkspaceHandle,
        path: str,
        data: bytes,
        *,
        append: bool = False,
        mkdirs: bool = True,
    ) -> int:
        if self.missing:
            raise SandboxError("workspace_missing", "작업공간 폴더가 없다", status=404)
        self.files[path] = (self.files.get(path, b"") if append else b"") + data
        return len(data)

    async def list_files(
        self,
        h: WorkspaceHandle,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 500,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        now = datetime.now(UTC)
        return [
            FileEntry(path=name, type="file", size=len(data), mtime=now)
            for name, data in sorted(self.files.items())
        ][:max_entries]

    async def stat(self, h: WorkspaceHandle, path: str) -> FileStat:
        return FileStat(type="file", size=len(self.files[path]), mtime=datetime.now(UTC))

    async def changes(
        self, h: WorkspaceHandle, since: datetime, *, max_entries: int = 1000
    ) -> FileChanges:
        entries = await self.list_files(h)
        return FileChanges(entries=entries[:max_entries], truncated=False)

    async def http_request(self, h: WorkspaceHandle, spec: HttpSpec) -> HttpResult:
        self.http.append(spec)
        return HttpResult(
            status=200,
            url=spec.url,
            headers={"content-type": "text/plain", "set-cookie": "token=secret"},
            content_type="text/plain",
            body="ok",
            body_bytes=2,
            truncated=False,
            saved_path=None,
            redirects=[],
        )

    async def snapshot(self, h: WorkspaceHandle, dest: Path) -> SnapshotInfo:
        raise NotImplementedError

    async def restore(
        self, h: WorkspaceHandle, src: Path, *, expected_sha256: str | None = None
    ) -> RestoreInfo:
        raise NotImplementedError

    async def stats(self, h: WorkspaceHandle) -> WorkspaceStats:
        return WorkspaceStats(
            disk_bytes=sum(len(data) for data in self.files.values()),
            disk_estimated=False,
            active_processes=0,
            last_exec_at=None,
        )

    async def purge_deleted(self) -> int:
        return 0

    async def aclose(self) -> None:
        return None


class MemorySink:
    def __init__(self, store: dict[str, bytearray], key: str) -> None:
        self._store = store
        self._key = key
        store[key] = bytearray()

    async def write(self, data: bytes) -> None:
        self._store[self._key].extend(data)

    async def close(self) -> str:
        return self._key

    async def abort(self) -> None:
        self._store.pop(self._key, None)


class RecordingPorts:
    """LoopPorts 구현. 이벤트는 스키마 모델로 검증하면서 모은다."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[uuid.UUID, StepMessage] = {}
        self.rows: list[StoredMessage] = []
        self.summaries: list[str] = []
        self.progress: list[tuple[int, UsageTotal]] = []
        self.sinks: dict[str, bytearray] = {}
        self.recreated = 0
        self.collect_calls = 0
        self.files: list[MessageFileData] = []
        # call_agent가 부르는 start_child. 테스트가 바꿔 끼운다. 기본은 거절이다.
        self.child_calls: list[ChildCall] = []
        self.child_reply: Callable[[ChildCall], ChildStarted | ChildStartError] = lambda call: (
            ChildStartError(code="agent_not_allowed", message="테스트 기본값")
        )

    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        payload = jsonable(dict(data))
        model = EVENT_DATA_MODELS[event_type]
        model.model_validate(payload)
        unknown = set(payload) - set(model.model_fields)
        assert not unknown, f"{event_type}에 스키마에 없는 필드: {unknown}"
        self.events.append((event_type, payload))

    async def save_message(self, message: StepMessage) -> None:
        stored = StoredMessage(
            id=message.id,
            seq=len(self.rows) + 2,
            role="assistant",
            blocks=encode_step(message),
            text=message.text,
            stop_reason=message.stop_reason,
            usage=message.usage,
        )
        if message.id in self.messages:
            index = next(i for i, row in enumerate(self.rows) if row.id == message.id)
            self.rows[index] = stored.model_copy(update={"seq": self.rows[index].seq})
        else:
            self.rows.append(stored)
        self.messages[message.id] = message.model_copy(deep=True)

    async def save_summary(self, text: str, usage: Mapping[str, Any] | None) -> uuid.UUID:
        summary_id = uuid.uuid4()
        self.summaries.append(text)
        self.rows.append(
            StoredMessage(
                id=summary_id,
                seq=len(self.rows) + 2,
                role="system_summary",
                blocks=[{"type": "text", "text": text}],
                text=text,
            )
        )
        return summary_id

    async def save_progress(self, steps: int, usage: UsageTotal) -> None:
        self.progress.append((steps, usage.model_copy()))

    async def open_output_sink(self, tool_call_id: str) -> MemorySink:
        return MemorySink(self.sinks, f"tool-outputs/run/{tool_call_id}.txt")

    async def recreate_workspace(self) -> WorkspaceHandle:
        self.recreated += 1
        self.backend.missing = False
        await self.emit("workspace.recreated", {})
        return WorkspaceHandle(session_id=uuid.uuid4(), root=Path("/ws"), backend="local")

    async def collect_files(self) -> FileSummary:
        self.collect_calls += 1
        return FileSummary(files=list(self.files), truncated=False)

    async def load_image(self, storage_key: str) -> str | None:
        return "aW1hZ2U="

    async def start_child(self, call: ChildCall) -> ChildStarted | ChildStartError:
        self.child_calls.append(call)
        return self.child_reply(call)

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [data for kind, data in self.events if kind == event_type]


class LoopHarness:
    """런 실행기처럼 히스토리를 만들고 루프를 돌린다. 재개도 같은 방식으로 한다."""

    def __init__(
        self,
        turns: Iterable[FakeTurn],
        *,
        groups: Iterable[str] = ("shell", "files", "http", "ask_user"),
        approval_mode: ApprovalMode = "off",
        limits: RunLimits | None = None,
        backend: FakeBackend | None = None,
        user_text: str = "작업을 해 줘",
        extra_tools: Iterable[Tool] = (),
        context_window: int = 1_000_000,
        delay_sec: float = 0.0,
    ) -> None:
        self.adapter = FakeAdapter(turns, delay_sec=delay_sec)
        self.backend = backend or FakeBackend()
        self.ports = RecordingPorts(self.backend)
        self.cancel = CancelSignal()
        self.tools = [*tools_for_groups(groups), *extra_tools]
        self.approval_mode: ApprovalMode = approval_mode
        self.limits = limits or RunLimits()
        self.context_window = context_window
        self.user_row = StoredMessage(
            id=uuid.uuid4(),
            seq=1,
            role="user",
            blocks=[{"type": "text", "text": user_text}],
            text=user_text,
        )
        self.run_id = uuid.uuid4()
        self.session_id = uuid.uuid4()
        self.override: str | None = None

    def config(self, **overrides: Any) -> LoopConfig:
        values: dict[str, Any] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "system_prompt": "시스템",
            "tools": self.tools,
            "params": ModelParams(max_output_tokens=1000),
            "limits": self.limits,
            "approval_mode": effective_approval_mode(self.approval_mode, self.override),
            "context_window": self.context_window,
            "max_output_cap": 8000,
        }
        values.update(overrides)
        return LoopConfig(**values)

    def loop(self, config: LoopConfig) -> AgentLoop:
        return AgentLoop(
            config=config,
            model=self.adapter,
            backend=self.backend,
            handle=WorkspaceHandle(session_id=self.session_id, root=Path("/ws"), backend="local"),
            ports=self.ports,
            cancel=self.cancel,
        )

    async def run(self, **overrides: Any) -> LoopOutcome:
        history = build_history([self.user_row])
        return await self.loop(self.config(**overrides)).run(history)

    async def resume(self, outcome: LoopOutcome, payload: ResumePayload) -> LoopOutcome:
        pending = outcome.pending
        assert pending is not None
        if payload.session_auto_approve:
            self.override = "off"
        rows = [self.user_row, *self.ports.rows]
        index = next(i for i, row in enumerate(rows) if row.id == pending.message_id)
        config = self.config(
            steps_done=outcome.steps,
            usage=outcome.usage,
            elapsed_ms=pending.elapsed_ms,
            resume=ResumeState(pending=pending, payload=payload),
        )
        self.cancel = CancelSignal()
        return await self.loop(config).run(build_history(rows[:index]), decode_step(rows[index]))

    def history(self) -> list[Any]:
        return build_history([self.user_row, *self.ports.rows])


def text_of(message: Any) -> str:
    return "".join(block.text for block in message.blocks if isinstance(block, TextBlock))


@pytest.fixture
def harness() -> Callable[..., LoopHarness]:
    return LoopHarness


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def recording_ports() -> Callable[[FakeBackend], RecordingPorts]:
    return RecordingPorts
