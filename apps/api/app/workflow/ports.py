# pyright: strict
"""엔진이 밖을 보는 포트(설계 3.1, 3.11).

엔진과 노드는 DB·HTTP 계층을 모른다. 모델 어댑터, 샌드박스, HTTP 클라이언트, 과업 서비스,
파일 저장, 시계, 체크포인트 저장은 모두 EnginePorts로 받는다. 이벤트는 포트가 아니라 Engine.run()이
돌려주는 비동기 반복자로 나간다(소비자가 다음 이벤트를 달라고 할 때까지 엔진이 기다리므로 이벤트를
영속한 뒤에 엔진이 다음으로 넘어간다).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from app.sandbox.base import HttpResult, HttpSpec, SandboxBackend
from app.schemas.events import UsageTotal
from app.workflow.frames import Checkpoint

if TYPE_CHECKING:
    from app.agent.task_service import AgentTaskResult, AgentTaskSpec
    from app.llm.base import ModelAdapter


class CancelToken(Protocol):
    """런 취소 신호(app/runner/supervisor.py의 CancelSignal). reason이 shutdown이면 서버 종료다."""

    reason: str | None

    def is_set(self) -> bool: ...

    async def wait(self) -> None: ...


class HttpPort(Protocol):
    """app/sandbox/http_client.py의 HttpClient.request와 같은 모양(9.2)."""

    async def request(self, spec: HttpSpec, *, workspace_root: Path) -> HttpResult: ...


class FilePortError(Exception):
    """파일 포트의 실패. 노드가 code를 그대로 노드 오류 코드로 쓴다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FilePort(Protocol):
    """런이 만든 파일을 wf_file(kind=output)로 남기고, 파일 참조(FileRef)의 내용을 읽는다.

    save_output은 FileRef 모양의 사전(source=wf_file)을 돌려준다. read_bytes는 참조의 저장소 키를
    믿지 않고 id로 행을 다시 찾는다. 둘 다 실패하면 FilePortError다.
    """

    async def save_output(
        self, source: Path, *, filename: str, mime: str | None
    ) -> dict[str, Any]: ...

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes: ...


class TaskPortError(Exception):
    """과업 포트의 실패(에이전트 없음, 발행 버전 없음 등). 노드가 code를 노드 오류 코드로 쓴다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChildRunView(BaseModel):
    """agent 노드가 기다리는 자식 런의 지금 모습. 포트가 DB에서 읽고 기다리지 않는다."""

    run_id: uuid.UUID
    session_id: uuid.UUID
    # 하위 에이전트를 기다리는 자식(지휘자)은 running이다(오케스트레이션 6.4).
    status: str
    steps: int = 0
    usage: UsageTotal = Field(default_factory=UsageTotal)
    # 에이전트 레인 대기열에서 몇 번째인지(1부터). 큐에 없으면 None.
    position: int | None = None
    # 자식이 남긴 출력 파일(FileRef 모양, source=session_file). 저장하지 않은 파일은 빠진다.
    files: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class TaskPort(Protocol):
    """agent 노드가 자식 런(1단계 과업 런)을 다루는 포트(2단계 3.7, 오케스트레이션 9장).

    start·wait·cancel은 AgentTaskService와 같다. 포트가 workspace_id와 correlation의
    workflow_run_id를 채우고 자식 id를 노드 런에 바로 적는다. 실패는 TaskPortError다.
    """

    async def start(self, spec: "AgentTaskSpec") -> uuid.UUID: ...

    # 끝나거나 사람 입력을 기다릴 때까지. 이미 그렇다면 바로 돌아온다(재개 전에 자식이 끝난 경우).
    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> "AgentTaskResult": ...

    async def cancel(self, run_id: uuid.UUID) -> None: ...

    async def child(self, run_id: uuid.UUID) -> ChildRunView: ...


@dataclass(frozen=True, slots=True)
class NodeSettings:
    """노드가 읽는 런 바깥의 값. 서버 설정과 워크스페이스 설정에서 온다."""

    # code 노드에 timeout_sec가 없을 때 쓴다(WF_CODE_TIMEOUT_SEC).
    code_timeout_sec: int = 30
    # workspace.settings.model_prices. 모델을 부른 노드가 node.usage의 cost_usd를 계산한다.
    model_prices: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict[str, Mapping[str, Any]]
    )


class EnginePorts(Protocol):
    # 제어 노드는 아래 다섯을 쓰지 않는다. 쓰는 노드는 None이면 노드 오류로 끝낸다.
    @property
    def model(self) -> "ModelAdapter | None": ...

    # DATA_DIR/wf-workspaces의 두 번째 백엔드. 런 작업 폴더는 acquire(run_id)로 얻는다(9.1).
    @property
    def sandbox(self) -> SandboxBackend | None: ...

    @property
    def http(self) -> HttpPort | None: ...

    @property
    def files(self) -> FilePort | None: ...

    @property
    def tasks(self) -> TaskPort | None: ...

    @property
    def node_settings(self) -> NodeSettings: ...

    # 이벤트·노드 런 시각과 sys.now(UTC).
    def now(self) -> datetime: ...

    # 소요 시간과 런 시간 상한. time.monotonic과 같은 단위(초).
    def monotonic(self) -> float: ...

    # waiting_input 전환 직전, 런 실패, 선택적 간격(WF_CHECKPOINT_INTERVAL_SEC)에만 부른다(3.7).
    async def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...
