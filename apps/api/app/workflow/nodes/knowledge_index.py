# pyright: strict
"""knowledge_index 노드(3단계 설계 8.2, 지식 쓰기).

글 변수나 파일 변수를 지식 베이스의 새 문서로 넣는다. 문서 만들기는 KnowledgePort(서비스의 문서
생성 경로, data_source_type=workflow)가 하고 색인은 지식 레인이 뒤에서 한다. wait를 켜면 5초마다
상태를 읽어 끝날 때까지 기다린다. 인제스트는 분당 80건에 묶이므로 긴 문서는 기다리지 않고 다음
런에서 쓰는 편이 낫다.

on_duplicate는 문서 이름 기준이다. replace(기본)는 같은 이름의 문서를 지우고 새로 넣고, skip은 있던
문서를 그대로 돌려주며, add는 이름이 겹쳐도 넣는다.
"""

import asyncio
import uuid
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.knowledge import FieldName, ProcessRule
from app.schemas.workflow import Issue, IssueLevel
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
    VarRef,
)
from app.workflow.nodes.knowledge_retrieval import paid_issue
from app.workflow.ports import DuplicatePolicy, IndexedDocuments, KnowledgePort, KnowledgePortError
from app.workflow.variables import is_file

POLL_SEC = 5.0
DEFAULT_WAIT_SEC = 600
MAX_WAIT_SEC = 3600
# 기다리지 않으면 원본 저장과 문서 행 만들기뿐이다.
STORE_TIMEOUT_SEC = 120
# 노드 시간 상한은 스스로 기다리기를 끝낼 시간보다 조금 길게 둔다. 엔진의 시간 초과는 재시도
# 대상이라 문서를 두 번 만들 수 있다.
WAIT_MARGIN_SEC = 30
DEFAULT_NAME = "{{ sys.now }}"
MAX_NAME_CHARS = 255
MAX_METADATA = 50
FINAL_STATUSES = frozenset({"completed", "error", "paused"})
# 출력 status가 갖는 값. wait를 끄면 queued·skipped, 켜면 완료 상태까지 간다.
INDEX_STATUSES = ("queued", "skipped", "completed", "error", "paused")


class IndexSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # string 변수. 글은 .txt 원본으로 저장된다.
    text_var: str | None = None
    # file 또는 array[file] 변수(txt·md·csv·html·pdf·docx).
    files_var: str | None = None


class IndexMetadataValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName
    # 템플릿. 렌더한 글을 필드 타입(string·number·time)에 맞춰 바꾼다. 비면 값을 두지 않는다.
    value: str = ""


class KnowledgeIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: uuid.UUID | None = None
    source: IndexSource = Field(default_factory=IndexSource)
    # 문서 이름 템플릿. 비우면 글은 {{ sys.now }}, 파일은 파일 이름이다. 파일이 여럿이면
    # 쓰지 않는다.
    name: str = ""
    # 이 문서만 다른 규칙을 쓸 때. 비우면 KB의 규칙.
    process_rule: ProcessRule | None = None
    metadata: list[IndexMetadataValue] = Field(
        default_factory=list[IndexMetadataValue], max_length=MAX_METADATA
    )
    on_duplicate: DuplicatePolicy = "replace"
    wait: bool = False
    # wait일 때 기다리는 상한(초).
    timeout_sec: int = Field(default=DEFAULT_WAIT_SEC, ge=10, le=MAX_WAIT_SEC)


def _issue(level: IssueLevel, code: str, message: str, field: str) -> Issue:
    return Issue(level=level, code=code, message=message, field=field)


def _file_refs(value: object) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if is_file(value):
        return [cast(dict[str, Any], value)]
    if isinstance(value, list):
        items = cast(list[Any], value)
        if all(is_file(item) for item in items):
            return [cast(dict[str, Any], item) for item in items]
    return None


def final_status(statuses: dict[uuid.UUID, str], document_ids: list[uuid.UUID]) -> str | None:
    """모든 문서가 끝났으면 묶은 상태(completed·error·paused), 아니면 None.

    사라진(지운) 문서는 error로 본다.
    """
    found = [statuses.get(document_id, "error") for document_id in document_ids]
    if any(status not in FINAL_STATUSES for status in found):
        return None
    if all(status == "completed" for status in found):
        return "completed"
    return "error" if "error" in found else "paused"


class KnowledgeIndexRunner(NodeRunner[KnowledgeIndexConfig]):
    type = "knowledge_index"
    Config = KnowledgeIndexConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (
        OutputSpec("document_ids", "array[string]"),
        OutputSpec("batch", "string"),
        OutputSpec("status", "string"),
    )

    def __init__(self, poll_sec: float = POLL_SEC) -> None:
        self.poll_sec = poll_sec

    def var_refs(self, config: KnowledgeIndexConfig) -> list[VarRef]:
        refs: list[VarRef] = []
        if config.source.text_var:
            refs.append(VarRef("source.text_var", config.source.text_var, "string"))
        if config.source.files_var:
            refs.append(VarRef("source.files_var", config.source.files_var))
        return refs

    def template_fields(self, config: KnowledgeIndexConfig) -> list[TemplateField]:
        fields = [TemplateField("name", config.name)] if config.name else []
        fields.extend(
            TemplateField(f"metadata[{index}].value", item.value)
            for index, item in enumerate(config.metadata)
        )
        return fields

    def validate(self, config: KnowledgeIndexConfig, graph: GraphView) -> list[Issue]:
        issues: list[Issue] = []
        if config.kb_id is None:
            issues.append(_issue("error", "kb_required", "글을 넣을 지식 베이스를 고른다", "kb_id"))
        source = config.source
        if not source.text_var and not source.files_var:
            issues.append(
                _issue(
                    "error",
                    "source_required",
                    "넣을 글 변수(text_var)나 파일 변수(files_var)를 고른다",
                    "source",
                )
            )
        elif source.text_var and source.files_var:
            issues.append(
                _issue(
                    "error",
                    "invalid_config",
                    "text_var와 files_var 중 하나만 고른다",
                    "source",
                )
            )
        names = [item.name for item in config.metadata]
        for index, name in enumerate(names):
            if name in names[:index]:
                issues.append(
                    _issue(
                        "error",
                        "duplicate_name",
                        f"메타데이터 필드 {name}이(가) 두 번 들어 있다",
                        f"metadata[{index}].name",
                    )
                )
        known = graph.knowledge_bases()
        if known is None or config.kb_id is None:
            return issues
        ref = known.get(config.kb_id)
        if ref is None:
            issues.append(
                _issue(
                    "error",
                    "knowledge_base_not_found",
                    "지식 베이스를 찾을 수 없다. 지워졌거나 다른 PC에서 가져온 노드다",
                    "kb_id",
                )
            )
            return issues
        if ref.paid:
            issues.append(paid_issue(ref, "kb_id"))
        for index, name in enumerate(names):
            if name not in ref.fields:
                issues.append(
                    _issue(
                        "error",
                        "metadata_field_not_found",
                        f"지식 베이스 {ref.name}에 없는 메타데이터 필드다: {name}",
                        f"metadata[{index}].name",
                    )
                )
        return issues

    def timeout_sec(self, config: KnowledgeIndexConfig) -> float | None:
        if config.wait:
            return config.timeout_sec + WAIT_MARGIN_SEC
        return STORE_TIMEOUT_SEC

    async def run(
        self, ctx: NodeContext, config: KnowledgeIndexConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        port = ctx.ports.knowledge
        if port is None:
            raise NodeFailure("knowledge_unavailable", "지식 베이스를 쓸 수 없는 실행 환경이다")
        kb_id = config.kb_id
        if kb_id is None:
            raise NodeFailure("kb_required", "글을 넣을 지식 베이스가 없다")
        started = ctx.ports.monotonic()
        metadata = {item.name: ctx.render(item.value).strip() for item in config.metadata}
        inputs: dict[str, Any] = {"kb_id": str(kb_id), "metadata": metadata}
        try:
            if config.source.text_var:
                indexed = await self._index_text(ctx, port, kb_id, config, metadata, inputs)
            elif config.source.files_var:
                indexed = await self._index_files(ctx, port, kb_id, config, metadata, inputs)
            else:
                raise NodeFailure("source_required", "넣을 글 변수나 파일 변수가 없다")
        except KnowledgePortError as exc:
            raise NodeFailure(
                exc.code, exc.message, retryable=exc.retryable, retry_after=exc.retry_after
            ) from None
        status = "skipped" if indexed.skipped else "queued"
        warnings: list[str] = []
        if indexed.skipped:
            warnings.append("duplicate_skipped: 같은 이름의 문서가 있어 넣지 않았다")
        elif config.wait and indexed.document_ids:
            status = await self._wait(ctx, port, kb_id, indexed.document_ids, config, started)
        return NodeResult(
            status="succeeded",
            outputs={
                "document_ids": [str(document_id) for document_id in indexed.document_ids],
                "batch": indexed.batch,
                "status": status,
            },
            inputs=inputs,
            process={
                "on_duplicate": config.on_duplicate,
                "wait": config.wait,
                "elapsed_sec": round(ctx.ports.monotonic() - started, 1),
            },
            warnings=warnings,
        )

    async def _index_text(
        self,
        ctx: NodeContext,
        port: KnowledgePort,
        kb_id: uuid.UUID,
        config: KnowledgeIndexConfig,
        metadata: dict[str, str],
        inputs: dict[str, Any],
    ) -> IndexedDocuments:
        selector = config.source.text_var or ""
        value = ctx.resolve(selector, "string")
        text = "" if value is None else str(value)
        if not text.strip():
            raise NodeFailure("source_empty", f"{selector}의 글이 비어 넣을 것이 없다")
        name = ctx.render(config.name or DEFAULT_NAME).strip()[:MAX_NAME_CHARS]
        if not name:
            raise NodeFailure("name_empty", "렌더한 문서 이름이 비었다")
        inputs.update({"source": "text", "name": name, "text_chars": len(text)})
        return await port.index_text(
            kb_id,
            name=name,
            text=text,
            process_rule=config.process_rule,
            metadata=metadata,
            on_duplicate=config.on_duplicate,
        )

    async def _index_files(
        self,
        ctx: NodeContext,
        port: KnowledgePort,
        kb_id: uuid.UUID,
        config: KnowledgeIndexConfig,
        metadata: dict[str, str],
        inputs: dict[str, Any],
    ) -> IndexedDocuments:
        selector = config.source.files_var or ""
        refs = _file_refs(ctx.resolve(selector))
        if refs is None:
            raise NodeFailure("type_mismatch", f"{selector}는 파일이나 파일 배열이어야 한다")
        if not refs:
            raise NodeFailure("source_empty", f"{selector}에 넣을 파일이 없다")
        name: str | None = None
        if config.name and len(refs) == 1:
            name = ctx.render(config.name).strip()[:MAX_NAME_CHARS] or None
        elif config.name:
            ctx.warn("name_ignored: 파일이 여럿이라 문서 이름은 파일 이름 그대로다")
        inputs.update(
            {
                "source": "files",
                "files": [str(ref.get("filename") or "") for ref in refs],
                "name": name,
            }
        )
        return await port.index_files(
            kb_id,
            refs,
            name=name,
            process_rule=config.process_rule,
            metadata=metadata,
            on_duplicate=config.on_duplicate,
        )

    async def _wait(
        self,
        ctx: NodeContext,
        port: KnowledgePort,
        kb_id: uuid.UUID,
        document_ids: list[uuid.UUID],
        config: KnowledgeIndexConfig,
        started: float,
    ) -> str:
        deadline = started + config.timeout_sec
        while True:
            try:
                statuses = await port.statuses(kb_id, document_ids)
            except KnowledgePortError as exc:
                raise NodeFailure(exc.code, exc.message, retryable=exc.retryable) from None
            found = final_status(statuses, document_ids)
            if found is not None:
                return found
            if ctx.ports.monotonic() + self.poll_sec > deadline:
                raise NodeFailure(
                    "index_wait_timeout",
                    f"{config.timeout_sec}초 안에 색인이 끝나지 않았다. 문서는 계속 색인된다",
                )
            await asyncio.sleep(self.poll_sec)
