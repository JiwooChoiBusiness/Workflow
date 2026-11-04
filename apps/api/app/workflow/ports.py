# pyright: strict
"""엔진이 밖을 보는 포트(설계 3.1, 3.11).

엔진과 노드는 DB·HTTP 계층을 모른다. 모델 어댑터, 샌드박스, HTTP 클라이언트, 과업 서비스,
파일 저장, 지식 베이스, 데이터 소스(SQL), 시계, 체크포인트 저장은 모두 EnginePorts로 받는다.
이벤트는 포트가 아니라
Engine.run()이 돌려주는 비동기 반복자로 나간다(소비자가 다음 이벤트를 달라고 할 때까지 엔진이
기다리므로 이벤트를 영속한 뒤에 엔진이 다음으로 넘어간다).
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.knowledge.retrieval.rerank import ModelCall
from app.knowledge.sql.sources import SourceView
from app.llm.key_pool import KeyTier
from app.sandbox.base import HttpResult, HttpSpec, SandboxBackend
from app.schemas.citations import RetrieverResourceData
from app.schemas.events import UsageTotal
from app.schemas.knowledge import ProcessRule, RetrievalSettings
from app.schemas.retrieval import MetadataFilter, RetrievalWarning
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

    save_output은 FileRef 모양의 사전(source=wf_file)을 돌려준다. read_bytes·copy_to는 참조의 저장소
    키를 믿지 않고 id로 행을 다시 찾는다. 모두 실패하면 FilePortError다.
    """

    async def save_output(
        self, source: Path, *, filename: str, mime: str | None
    ) -> dict[str, Any]: ...

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes: ...

    # 메모리에 다 올리지 않고 dest 파일로 흘려 쓴다. 쓴 바이트 수를 돌려준다(document_extractor).
    async def copy_to(self, ref: Mapping[str, Any], dest: Path, *, max_bytes: int) -> int: ...


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


class KnowledgePortError(Exception):
    """지식 포트의 실패. 노드가 code를 노드 오류 코드로 쓴다.

    retry_after는 일일 한도처럼 기다리면 풀리는 실패의 남은 초다(workflow.error까지 간다).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class KnowledgeHits:
    """KnowledgePort.retrieve의 결과. resources는 순위(position) 순서다."""

    # 실제로 쓴 질의(NFC, 앞뒤 공백 제거, 임베딩 입력 상한에서 자른 것).
    query: str
    resources: list[RetrieverResourceData]
    warnings: list[RetrievalWarning]
    # 자동 필터·LLM 재순위의 채팅 호출. 질의 임베딩은 세지 않는다(3단계 6.6).
    calls: list[ModelCall]
    settings: RetrievalSettings
    latency_ms: int
    # 자동 필터가 만든 조건. 수동 필터나 필터 없음이면 None.
    auto_filter: MetadataFilter | None = None


# 같은 이름의 문서가 이미 있을 때(3단계 8.2). replace는 있던 문서를 지우고 새로 넣는다.
DuplicatePolicy = Literal["skip", "replace", "add"]


@dataclass(frozen=True)
class IndexedDocuments:
    document_ids: list[uuid.UUID]
    # 업로드 묶음(indexing-status의 batch). skip으로 넣지 않았으면 빈 글이다.
    batch: str
    # on_duplicate=skip이라 넣지 않고 있던 문서를 돌려준다.
    skipped: bool = False


class KnowledgePort(Protocol):
    """knowledge_retrieval·knowledge_index 노드가 지식 베이스를 쓰는 포트(3단계 8.1, 8.2).

    서비스(app/services/workflow_knowledge.py)가 이 런의 워크스페이스로 구현을 넣는다. 실패는
    KnowledgePortError다.
    """

    async def retrieve(
        self,
        kb_ids: Sequence[uuid.UUID],
        query: str,
        settings: RetrievalSettings | None,
        metadata_filter: MetadataFilter | None,
        *,
        tier: KeyTier,
    ) -> KnowledgeHits: ...

    # metadata는 필드 이름 → 렌더한 글. 필드 타입에 맞춰 바꾸는 일은 구현이 한다.
    async def index_text(
        self,
        kb_id: uuid.UUID,
        *,
        name: str,
        text: str,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments: ...

    # files는 FileRef 모양의 사전이다. 참조 속 저장소 키는 믿지 않고 id로 행을 다시 찾는다.
    async def index_files(
        self,
        kb_id: uuid.UUID,
        files: Sequence[Mapping[str, Any]],
        *,
        name: str | None,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments: ...

    # 문서 id → indexing_status. 지운 문서는 빠진다.
    async def statuses(
        self, kb_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]: ...


class SqlPortError(Exception):
    """SQL 포트의 실패. 노드가 code를 노드 오류 코드로 쓴다.

    stage는 validate(검증 거부: sql_invalid, not_select, table_not_allowed, function_not_allowed,
    column_hidden 등), execute(실행 실패: sql_error, sql_timeout, sql_memory_limit, sql_failed),
    source(sql_source_not_found, sql_source_not_prepared, sql_runtime_missing)다.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: Literal["validate", "execute", "source"],
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable


@dataclass(frozen=True)
class SqlSourceInfo:
    """sql_schema·sql_query가 보는 데이터 소스. view는 숨긴 열·표를 뺀 스냅샷이다."""

    id: uuid.UUID
    name: str
    slug: str
    view: SourceView
    glossary: str
    # 노드 config가 비었을 때 쓰는 데이터 소스 기본값(10.3, 10.6).
    approval: Literal["none", "always"]
    max_rows: int
    timeout_sec: int
    llm_rows: int
    llm_bytes: int
    # SQL_MAX_ROWS. 노드의 max_rows는 이 값까지다.
    max_rows_cap: int


@dataclass(frozen=True)
class SqlRun:
    """질의 한 번의 결과(10.3의 출력 재료)."""

    sql_executed: str
    columns: list[str]
    # 열 이름 → 값. max_rows와 변수 1MB 상한 안이다.
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    # rows가 1MB 상한에 걸려 row_count보다 적다.
    rows_cut: bool
    result_text: str
    elapsed_ms: int
    # 잘린 결과를 CSV로 남겼으면 그 파일 참조(save_csv가 돌려준 것).
    file: dict[str, Any] | None = None


# 잘린 결과 CSV를 받아 파일 참조로 바꾼다. 작업 폴더가 지워지기 전에 부른다. 남기지 못했으면 None.
CsvSaver = Callable[[Path], Awaitable[dict[str, Any] | None]]


class SqlPort(Protocol):
    """sql_schema·sql_query 노드가 데이터 소스를 쓰는 포트(3단계 10.3~10.6).

    서비스(app/services/sql_sources.py)가 이 런의 워크스페이스로 구현을 넣는다. 검증·실행·기록은
    콘솔 SQL 시험·query_sql 도구와 같은 경로다. 실패는 SqlPortError다.
    """

    async def describe(self, source_id: uuid.UUID) -> SqlSourceInfo: ...

    # 검증만 한다(승인 폼에 채울 SQL). 거부는 기록된다.
    async def validate(self, source_id: uuid.UUID, sql: str, *, max_rows: int) -> str: ...

    # 다시 검증해 실행한다.
    async def execute(
        self,
        source_id: uuid.UUID,
        sql: str,
        *,
        max_rows: int,
        timeout_sec: int,
        llm_rows: int,
        llm_bytes: int,
        save_csv: CsvSaver | None,
    ) -> SqlRun: ...


@dataclass(frozen=True, slots=True)
class NodeSettings:
    """노드가 읽는 런 바깥의 값. 서버 설정과 워크스페이스 설정에서 온다."""

    # code 노드에 timeout_sec가 없을 때 쓴다(WF_CODE_TIMEOUT_SEC).
    code_timeout_sec: int = 30
    # workspace.settings.model_prices. 모델을 부른 노드가 node.usage의 cost_usd를 계산한다.
    model_prices: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict[str, Mapping[str, Any]]
    )
    # document_extractor가 pdf·docx 파서를 받아 두는 폴더(DATA_DIR/cache/wf-extract). None이면
    # pdf·docx를 읽지 못한다.
    extract_cache_dir: Path | None = None
    # 런의 키 등급(wf_run.limits.tier, 3단계 14.4). 모델을 부르는 노드가 ChatRequest.tier로,
    # agent 노드가 자식 과업의 tier로, 지식 검색이 질의 임베딩 등급으로 넘긴다.
    tier: KeyTier = "free"


class EnginePorts(Protocol):
    # 제어 노드는 아래 일곱을 쓰지 않는다. 쓰는 노드는 None이면 노드 오류로 끝낸다.
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
    def knowledge(self) -> KnowledgePort | None: ...

    @property
    def sql(self) -> SqlPort | None: ...

    @property
    def node_settings(self) -> NodeSettings: ...

    # 이벤트·노드 런 시각과 sys.now(UTC).
    def now(self) -> datetime: ...

    # 소요 시간과 런 시간 상한. time.monotonic과 같은 단위(초).
    def monotonic(self) -> float: ...

    # waiting_input 전환 직전, 런 실패, 선택적 간격(WF_CHECKPOINT_INTERVAL_SEC)에만 부른다(3.7).
    async def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...
