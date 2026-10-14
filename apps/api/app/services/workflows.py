"""워크플로우, 초안, 발행 버전(2단계 설계 3.1, 4.1, 4.3, 6.1).

초안은 검증 오류가 있어도 저장하고 응답에 issues를 싣는다. 실행과 발행은 오류가 하나라도 있으면
거부한다. 발행은 초안을 번호 있는 불변 버전으로 복제하고(1단계 에이전트와 같다) 비밀 env 값은
암호문 그대로 옮긴다. 런(workflow_runs.py)과 실행기(workflow_executor.py)도 여기의 버전 해석
함수(그래프·env·대화 변수·설정)를 쓴다.
"""

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import WfVersion, Workflow
from app.db.models.enums import AgentVersionStatus, WorkflowKind
from app.db.repositories.agents import AgentRepository
from app.db.repositories.workflows import WfVersionRepository, WorkflowRepository
from app.db.types import new_id, utcnow
from app.schemas.agent_version import EnvVarIn
from app.schemas.workflow import (
    ConversationVar,
    Graph,
    Issue,
    NodeTypeList,
    StoredWfEnvVar,
    ValidationOut,
    VarType,
    WfEnvVarOut,
    WorkflowCreate,
    WorkflowDraftCheck,
    WorkflowDraftUpdate,
    WorkflowList,
    WorkflowOut,
    WorkflowPatch,
    WorkflowPublishOut,
    WorkflowSettings,
    WorkflowTemplate,
    WorkflowVersionList,
    WorkflowVersionOut,
    WorkflowVersionSummary,
)
from app.schemas.workflow import (
    WorkflowKind as WorkflowKindName,
)
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.services.agents import slugify
from app.services.errors import Problems, ServiceError, not_found
from app.workflow import graph as graph_check
from app.workflow.graph import MAX_GRAPH_BYTES
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.llm import DEFAULT_SYSTEM_PROMPT, LlmConfig, MemoryConfig, PromptMessage

log = structlog.get_logger("app.services.workflows")

_SLUG_MAX = 64
_SLUG_ATTEMPTS = 1000


def graph_json(graph: Graph) -> str:
    return json.dumps(
        graph.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def graph_hash(graph: Graph) -> str:
    return hashlib.sha256(graph_json(graph).encode("utf-8")).hexdigest()


def parse_graph(raw: dict[str, Any]) -> Graph | None:
    try:
        return Graph.model_validate(raw)
    except ValidationError:
        return None


def parse_settings(raw: dict[str, Any] | None) -> WorkflowSettings:
    try:
        return WorkflowSettings.model_validate(raw or {})
    except ValidationError:
        log.warning("workflow.settings_invalid")
        return WorkflowSettings()


def stored_env(raw: Sequence[dict[str, Any]]) -> list[StoredWfEnvVar]:
    items: list[StoredWfEnvVar] = []
    for item in raw:
        try:
            items.append(StoredWfEnvVar.model_validate(item))
        except ValidationError:
            log.warning("workflow.env_var_invalid")
    return items


def conversation_vars(raw: Sequence[dict[str, Any]]) -> list[ConversationVar]:
    items: list[ConversationVar] = []
    for item in raw:
        try:
            items.append(ConversationVar.model_validate(item))
        except ValidationError:
            log.warning("workflow.conversation_var_invalid")
    return items


def conversation_types(kind: str, items: Sequence[ConversationVar]) -> dict[str, VarType]:
    # 워크플로우에는 대화 변수가 없다. 빈 사전을 주면 검증이 conv.* 참조를 오류로 잡는다.
    if kind != WorkflowKind.CHATFLOW:
        return {}
    return {item.name: item.type for item in items}


def decrypt_env(
    items: Sequence[StoredWfEnvVar], cipher: Callable[[], SecretCipher]
) -> tuple[dict[str, str], list[str]]:
    """(이름 → 값, 비밀값 이름). 비밀값이 있는데 ENCRYPTION_KEY가 없거나 다르면 CryptoError."""
    env: dict[str, str] = {}
    secrets: list[str] = []
    for item in items:
        if item.secret:
            if item.value_encrypted is None:
                continue
            env[item.name] = cipher().decrypt(item.value_encrypted)
            secrets.append(item.name)
        elif item.value is not None:
            env[item.name] = item.value
    return env, secrets


def version_issues(
    version: WfVersion,
    kind: str,
    *,
    registry: NodeRegistry | None = None,
    agent_ids: Collection[uuid.UUID] | None = None,
) -> list[Issue]:
    graph = parse_graph(version.graph)
    if graph is None:
        return [
            Issue(level="error", code="graph_unreadable", message="저장된 그래프를 읽지 못했다")
        ]
    return check_graph(
        graph,
        kind,
        env_names=[item.name for item in stored_env(version.env_vars)],
        conversation=conversation_vars(version.conversation_vars),
        registry=registry,
        agent_ids=agent_ids,
    )


def check_graph(
    graph: Graph,
    kind: str,
    *,
    env_names: Sequence[str],
    conversation: Sequence[ConversationVar],
    registry: NodeRegistry | None = None,
    agent_ids: Collection[uuid.UUID] | None = None,
) -> list[Issue]:
    """agent_ids(live_agent_ids)를 주면 agent 노드가 없는 에이전트를 가리킬 때 agent_not_found다."""
    workflow_kind: WorkflowKindName = "chatflow" if kind == WorkflowKind.CHATFLOW else "workflow"
    return graph_check.validate(
        graph,
        workflow_kind,
        registry=registry,
        env_names=env_names,
        conversation_vars=conversation_types(kind, conversation),
        agent_ids=agent_ids,
    )


async def live_agent_ids(
    session: AsyncSession, workspace_id: uuid.UUID, graph: Graph | dict[str, Any] | None
) -> set[uuid.UUID]:
    """그래프의 agent 노드가 가리키는 에이전트 중 워크스페이스에 살아 있는 것."""
    if isinstance(graph, dict):
        graph = parse_graph(graph)
    wanted = graph_check.agent_references(graph) if graph is not None else set[uuid.UUID]()
    if not wanted:
        return set()
    rows = await AgentRepository(session, workspace_id).get_many_active(wanted)
    return {row.id for row in rows}


async def free_slug(workflows: WorkflowRepository, name: str) -> str:
    """이름에서 만든 slug. 겹치면 -2, -3을 붙인다. 이름이 한글뿐이면 workflow로 시작한다."""
    base = slugify(name) or "workflow"
    candidate = base
    for number in range(2, _SLUG_ATTEMPTS):
        if not await workflows.slug_taken(candidate):
            return candidate
        suffix = f"-{number}"
        candidate = base[: _SLUG_MAX - len(suffix)].rstrip("-") + suffix
    raise _slug_taken(candidate)


def next_stamp(clock: Callable[[], datetime], previous: datetime) -> datetime:
    # 낙관적 잠금 값이 저장할 때마다 반드시 바뀌어야 한다(Windows 시계 해상도 약 15ms).
    now = clock()
    return now if now > previous else previous + timedelta(microseconds=1)


def has_errors(issues: Sequence[Issue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def issue_details(issues: Sequence[Issue]) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues if issue.level == "error"]


def _node(node_id: str, node_type: str, title: str, x: float, config: dict[str, Any]) -> Any:
    return {
        "id": node_id,
        "type": node_type,
        "title": title,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def default_graph(kind: str, template: WorkflowTemplate) -> Graph:
    """새 워크플로우의 초안 그래프. 그대로 실행할 수 있는 최소 모양이다."""
    start = _node("start", "start", "시작", 0, {"fields": []})
    if kind == WorkflowKind.WORKFLOW:
        nodes = [start, _node("end_1", "end", "끝", 300, {"outputs": []})]
    elif template == "chat_basic":
        config = LlmConfig(
            prompt=[
                PromptMessage(role="system", text=DEFAULT_SYSTEM_PROMPT),
                PromptMessage(role="user", text="{{ sys.query }}"),
            ],
            memory=MemoryConfig(enabled=True, window_turns=10),
        )
        llm = _node("llm_1", "llm", "LLM", 300, config.model_dump(mode="json"))
        nodes = [start, llm, _node("answer_1", "answer", "답변", 600, {"text": "{{ llm_1.text }}"})]
    else:
        nodes = [start, _node("answer_1", "answer", "답변", 300, {"text": ""})]
    edges = [
        {"id": f"e{index}", "source": nodes[index - 1]["id"], "target": nodes[index]["id"]}
        for index in range(1, len(nodes))
    ]
    return Graph.model_validate({"nodes": nodes, "edges": edges})


def workflow_out(workflow: Workflow, published: WfVersion | None) -> WorkflowOut:
    return WorkflowOut(
        id=workflow.id,
        slug=workflow.slug,
        name=workflow.name,
        icon=workflow.icon,
        description=workflow.description,
        kind=workflow.kind,  # pyright: ignore[reportArgumentType]
        webapp_enabled=workflow.webapp_enabled,
        draft_version_id=workflow.draft_version_id,
        published_version_id=workflow.published_version_id,
        published_version_number=published.version_number if published else None,
        published_at=published.published_at if published else None,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _env_out(raw: Sequence[dict[str, Any]]) -> list[WfEnvVarOut]:
    return [
        WfEnvVarOut(
            name=item.name,
            secret=item.secret,
            value=None if item.secret else item.value,
            has_value=(item.value_encrypted if item.secret else item.value) is not None,
        )
        for item in stored_env(raw)
    ]


def _slug_taken(slug: str | None) -> ServiceError:
    if slug is None:
        return ServiceError(409, "slug_taken", "slug가 다른 워크플로우와 겹친다")
    return ServiceError(409, "slug_taken", f"이미 쓰는 slug다: {slug}", details={"slug": slug})


class WorkflowService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        registry: NodeRegistry | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        # None이면 검증이 기본 레지스트리를 쓴다.
        self._registry = registry
        self._clock = clock
        self._cipher_cache: SecretCipher | None = None

    def _cipher(self) -> SecretCipher:
        if self._cipher_cache is None:
            try:
                self._cipher_cache = cipher_from_settings(self._settings)
            except CryptoError as exc:
                raise ServiceError(
                    503, "encryption_key_missing", f"비밀 환경변수를 암호화할 수 없다: {exc}"
                ) from None
        return self._cipher_cache

    def node_types(self) -> NodeTypeList:
        registry = self._registry or default_registry()
        return NodeTypeList(items=registry.describe())

    async def list_workflows(
        self, workspace_id: uuid.UUID, *, kind: str | None = None
    ) -> WorkflowList:
        async with self._db.read() as session:
            rows = await WorkflowRepository(session, workspace_id).list_active(kind=kind)
            published = await WfVersionRepository(session, workspace_id).get_many(
                [row.published_version_id for row in rows if row.published_version_id]
            )
        by_id = {version.id: version for version in published}
        return WorkflowList(
            items=[
                workflow_out(
                    row, by_id.get(row.published_version_id) if row.published_version_id else None
                )
                for row in rows
            ]
        )

    async def get_workflow(self, workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> WorkflowOut:
        async with self._db.read() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            return workflow_out(workflow, await self._published(session, workspace_id, workflow))

    async def create_workflow(self, workspace_id: uuid.UUID, body: WorkflowCreate) -> WorkflowOut:
        if body.template == "chat_basic" and body.kind != WorkflowKind.CHATFLOW:
            raise ServiceError(
                422, "template_kind_mismatch", "chat_basic 템플릿은 chatflow에만 쓴다"
            )
        graph = default_graph(body.kind, body.template)
        try:
            async with self._db.write() as session:
                workflows = WorkflowRepository(session, workspace_id)
                if body.slug is not None:
                    if await workflows.slug_taken(body.slug):
                        raise _slug_taken(body.slug)
                    slug = body.slug
                else:
                    slug = await free_slug(workflows, body.name)
                workflow = workflows.add(
                    Workflow(
                        id=new_id(),
                        slug=slug,
                        name=body.name,
                        icon=body.icon,
                        description=body.description,
                        kind=body.kind,
                        webapp_enabled=False,
                    )
                )
                await workflows.flush()
                versions = WfVersionRepository(session, workspace_id)
                draft = versions.add(
                    WfVersion(
                        id=new_id(),
                        workflow_id=workflow.id,
                        version_number=None,
                        status=AgentVersionStatus.DRAFT,
                        graph=graph.model_dump(mode="json"),
                        env_vars=[],
                        conversation_vars=[],
                        settings=WorkflowSettings().model_dump(mode="json"),
                        graph_hash=graph_hash(graph),
                    )
                )
                await versions.flush()
                workflow.draft_version_id = draft.id
                await workflows.flush()
                out = workflow_out(workflow, None)
        except IntegrityError:
            raise _slug_taken(body.slug) from None
        log.info("workflow.created", workflow_id=str(out.id), kind=out.kind, slug=out.slug)
        return out

    async def update_workflow(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, patch: WorkflowPatch
    ) -> WorkflowOut:
        try:
            async with self._db.write() as session:
                workflows = WorkflowRepository(session, workspace_id)
                workflow = await self._active(session, workspace_id, workflow_id)
                if patch.slug is not None and patch.slug != workflow.slug:
                    if await workflows.slug_taken(patch.slug, exclude_id=workflow.id):
                        raise _slug_taken(patch.slug)
                    workflow.slug = patch.slug
                if patch.name is not None:
                    workflow.name = patch.name
                if "icon" in patch.model_fields_set:
                    workflow.icon = patch.icon
                if patch.description is not None:
                    workflow.description = patch.description
                if patch.webapp_enabled is not None:
                    workflow.webapp_enabled = patch.webapp_enabled
                await workflows.flush()
                out = workflow_out(workflow, await self._published(session, workspace_id, workflow))
        except IntegrityError:
            raise _slug_taken(patch.slug) from None
        return out

    async def delete_workflow(self, workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> None:
        async with self._db.write() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            # 행은 런 로그를 위해 남기고 slug는 다시 쓸 수 있게 된다(부분 유니크 인덱스).
            workflow.deleted_at = self._clock()
        log.info("workflow.deleted", workflow_id=str(workflow_id))

    async def get_draft(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> WorkflowVersionOut:
        async with self._db.read() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            draft = await self._draft(session, workspace_id, workflow)
            agents = await live_agent_ids(session, workspace_id, draft.graph)
        return self._version_out(workflow, draft, agent_ids=agents)

    async def save_draft(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, body: WorkflowDraftUpdate
    ) -> WorkflowVersionOut:
        size = len(graph_json(body.graph).encode("utf-8"))
        if size > MAX_GRAPH_BYTES:
            raise ServiceError(
                422,
                "graph_too_large",
                f"그래프 JSON은 {MAX_GRAPH_BYTES // 2**20}MB까지다({size}바이트)",
            )
        problems = Problems()
        async with self._db.write() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            draft = await self._draft(session, workspace_id, workflow)
            if draft.updated_at != body.updated_at:
                raise ServiceError(
                    409,
                    "draft_conflict",
                    "초안이 다른 곳에서 먼저 저장되었다. 다시 불러온 뒤 고쳐서 저장한다",
                    details={"updated_at": draft.updated_at.isoformat()},
                )
            if body.conversation_vars and workflow.kind != WorkflowKind.CHATFLOW:
                problems.add(
                    ("conversation_vars",),
                    "conversation_vars_not_allowed",
                    "대화 변수는 chatflow에만 둔다",
                )
            env_vars = self._merge_env(draft.env_vars, body.env_vars, problems)
            problems.raise_if_any()
            draft.graph = body.graph.model_dump(mode="json")
            draft.graph_hash = graph_hash(body.graph)
            draft.env_vars = env_vars
            draft.conversation_vars = [
                item.model_dump(mode="json") for item in body.conversation_vars
            ]
            draft.settings = body.settings.model_dump(mode="json")
            draft.updated_at = next_stamp(self._clock, draft.updated_at)
            await WfVersionRepository(session, workspace_id).flush()
            agents = await live_agent_ids(session, workspace_id, body.graph)
        return self._version_out(workflow, draft, agent_ids=agents)

    async def validate_draft(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, body: WorkflowDraftCheck | None
    ) -> ValidationOut:
        async with self._db.read() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            draft = await self._draft(session, workspace_id, workflow)
            graph = draft.graph if body is None else body.graph
            agents = await live_agent_ids(session, workspace_id, graph)
        if body is None:
            issues = version_issues(draft, workflow.kind, registry=self._registry, agent_ids=agents)
        else:
            env_names = (
                [item.name for item in body.env_vars]
                if body.env_vars is not None
                else [item.name for item in stored_env(draft.env_vars)]
            )
            conversation = (
                body.conversation_vars
                if body.conversation_vars is not None
                else conversation_vars(draft.conversation_vars)
            )
            issues = check_graph(
                body.graph,
                workflow.kind,
                env_names=env_names,
                conversation=conversation,
                registry=self._registry,
                agent_ids=agents,
            )
        return ValidationOut(ok=not has_errors(issues), issues=issues)

    async def publish(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, change_note: str | None = None
    ) -> WorkflowPublishOut:
        async with self._db.write() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            draft = await self._draft(session, workspace_id, workflow)
            agents = await live_agent_ids(session, workspace_id, draft.graph)
            issues = version_issues(draft, workflow.kind, registry=self._registry, agent_ids=agents)
            if has_errors(issues):
                raise ServiceError(
                    422,
                    "draft_invalid",
                    "초안에 검증 오류가 있다. 고쳐 저장한 뒤 다시 발행한다",
                    details=issue_details(issues),
                )
            versions = WfVersionRepository(session, workspace_id)
            version = versions.add(
                WfVersion(
                    id=new_id(),
                    workflow_id=workflow.id,
                    version_number=await versions.next_version_number(workflow.id),
                    status=AgentVersionStatus.PUBLISHED,
                    graph=copy.deepcopy(draft.graph),
                    env_vars=copy.deepcopy(draft.env_vars),
                    conversation_vars=copy.deepcopy(draft.conversation_vars),
                    settings=copy.deepcopy(draft.settings),
                    graph_hash=draft.graph_hash,
                    published_at=self._clock(),
                    change_note=change_note or None,
                )
            )
            await versions.flush()
            workflow.published_version_id = version.id
            await WorkflowRepository(session, workspace_id).flush()
            out = WorkflowPublishOut(
                workflow=workflow_out(workflow, version),
                version=self._version_out(workflow, version, issues=issues),
            )
        log.info(
            "workflow.published",
            workflow_id=str(workflow_id),
            version_id=str(out.version.id),
            version_number=out.version.version_number,
        )
        return out

    async def list_versions(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> WorkflowVersionList:
        async with self._db.read() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            rows = await WfVersionRepository(session, workspace_id).list_published(workflow.id)
        return WorkflowVersionList(
            items=[
                WorkflowVersionSummary(
                    id=row.id,
                    version_number=row.version_number or 0,
                    graph_hash=row.graph_hash,
                    published_at=row.published_at,
                    change_note=row.change_note,
                    is_current=row.id == workflow.published_version_id,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )

    async def get_version(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersionOut:
        async with self._db.read() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            version = await WfVersionRepository(session, workspace_id).get_for_workflow(
                workflow.id, version_id
            )
            if version is None:
                raise not_found("워크플로우 버전")
            agents = await live_agent_ids(session, workspace_id, version.graph)
        return self._version_out(workflow, version, agent_ids=agents)

    async def restore_version(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowOut:
        async with self._db.write() as session:
            workflow = await self._active(session, workspace_id, workflow_id)
            version = await WfVersionRepository(session, workspace_id).get_for_workflow(
                workflow.id, version_id
            )
            if version is None or version.version_number is None:
                raise not_found("발행 버전")
            workflow.published_version_id = version.id
            await WorkflowRepository(session, workspace_id).flush()
            out = workflow_out(workflow, version)
        log.info("workflow.restored", workflow_id=str(workflow_id), version_id=str(version_id))
        return out

    async def _active(
        self, session: AsyncSession, workspace_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> Workflow:
        workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
        if workflow is None:
            raise not_found("워크플로우")
        return workflow

    async def _draft(
        self, session: AsyncSession, workspace_id: uuid.UUID, workflow: Workflow
    ) -> WfVersion:
        draft = await WfVersionRepository(session, workspace_id).get_draft(workflow.id)
        if draft is None:
            raise not_found("워크플로우 초안")
        return draft

    async def _published(
        self, session: AsyncSession, workspace_id: uuid.UUID, workflow: Workflow
    ) -> WfVersion | None:
        if workflow.published_version_id is None:
            return None
        return await WfVersionRepository(session, workspace_id).get(workflow.published_version_id)

    def _merge_env(
        self, stored: list[dict[str, Any]], incoming: list[EnvVarIn], problems: Problems
    ) -> list[dict[str, Any]]:
        existing = {item.name: item for item in stored_env(stored)}
        merged: list[dict[str, Any]] = []
        for index, item in enumerate(incoming):
            old = existing.get(item.name)
            if item.value is not None:
                value = item.value
            elif old is not None and old.value is None and old.value_encrypted is None:
                # 이름만 저장된 변수(값 없이 내보낸 비밀값을 가져온 것)는 값이 올 때까지 비워 둔다.
                # 화면이 값을 받기 전에도 초안 자동 저장이 막히지 않게 한다.
                merged.append(StoredWfEnvVar(name=item.name, secret=item.secret).model_dump())
                continue
            elif old is None:
                problems.add(
                    ("env_vars", index, "value"),
                    "env_value_required",
                    "새 환경변수는 값이 필요하다",
                )
                continue
            elif old.secret and not item.secret:
                # 비밀값을 일반 값으로 바꾸면 응답에 값이 보이게 되므로 값을 다시 받는다.
                problems.add(
                    ("env_vars", index, "value"),
                    "env_value_required",
                    "비밀값을 일반 값으로 바꾸려면 값을 다시 입력한다",
                )
                continue
            elif old.secret:
                merged.append(old.model_dump())
                continue
            else:
                value = old.value or ""
            if item.secret:
                entry = StoredWfEnvVar(
                    name=item.name, secret=True, value_encrypted=self._cipher().encrypt(value)
                )
            else:
                entry = StoredWfEnvVar(name=item.name, secret=False, value=value)
            merged.append(entry.model_dump())
        return merged

    def _version_out(
        self,
        workflow: Workflow,
        version: WfVersion,
        *,
        issues: list[Issue] | None = None,
        agent_ids: Collection[uuid.UUID] | None = None,
    ) -> WorkflowVersionOut:
        graph = parse_graph(version.graph) or Graph()
        if issues is None:
            issues = version_issues(
                version, workflow.kind, registry=self._registry, agent_ids=agent_ids
            )
        return WorkflowVersionOut(
            id=version.id,
            workflow_id=version.workflow_id,
            kind=workflow.kind,  # pyright: ignore[reportArgumentType]
            version_number=version.version_number,
            status=version.status,  # pyright: ignore[reportArgumentType]
            graph=graph,
            env_vars=_env_out(version.env_vars),
            conversation_vars=conversation_vars(version.conversation_vars),
            settings=parse_settings(version.settings),
            graph_hash=version.graph_hash,
            published_at=version.published_at,
            change_note=version.change_note,
            issues=issues,
            created_at=version.created_at,
            updated_at=version.updated_at,
        )
