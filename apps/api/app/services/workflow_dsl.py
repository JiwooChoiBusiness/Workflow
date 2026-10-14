"""워크플로우 DSL 내보내기·가져오기(2단계 설계 5장, 6.1).

YAML 읽기·Dify 변환·그래프 손질은 app/workflow/dsl의 순수 함수가 한다. CPU만 쓰는 일이라 스레드에서
돌린다. 여기서는 에이전트 slug와 id를 잇고, 비밀 환경 변수를 암·복호화하고, 워크플로우·초안을 쓰고
검증한다.

가져오기는 새 워크플로우를 만들거나(slug는 이름에서, 겹치면 -2) into로 준 워크플로우의 초안을
덮어쓴다. 덮어쓸 때 발행 버전과 이름·아이콘·설명은 그대로 두고, 값 없이 들어온 환경 변수는 같은
이름으로 이미 저장된 값을 남긴다.
"""

import asyncio
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import WfVersion, Workflow
from app.db.models.enums import AgentVersionStatus, WorkflowKind
from app.db.repositories.agents import AgentRepository
from app.db.repositories.workflows import WfVersionRepository, WorkflowRepository
from app.db.types import new_id, utcnow
from app.schemas.workflow import (
    DslImportSummary,
    DslWarning,
    Graph,
    StoredWfEnvVar,
    WorkflowImportOut,
)
from app.schemas.workflow import WorkflowKind as WorkflowKindName
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.services.errors import ServiceError, not_found
from app.services.workflows import (
    check_graph,
    conversation_vars,
    free_slug,
    graph_hash,
    graph_json,
    live_agent_ids,
    next_stamp,
    parse_graph,
    parse_settings,
    stored_env,
)
from app.workflow.dsl.export import build_document, dump_yaml
from app.workflow.dsl.import_ import AgentRef, PreparedImport, read_dsl
from app.workflow.dsl.schema import MAX_DSL_BYTES, DslEnvVar, DslError
from app.workflow.graph import MAX_GRAPH_BYTES, agent_references
from app.workflow.nodes.base import NodeRegistry

log = structlog.get_logger("app.services.workflow_dsl")

ExportVersion = Literal["draft", "published"] | uuid.UUID
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
# 에이전트를 찾지 못했는데 파일의 agent_id도 읽을 수 없을 때 넣는 id. 어떤 에이전트와도 겹치지 않아
# 검증이 agent_not_found로 알린다.
MISSING_AGENT_ID = uuid.UUID(int=0)


@dataclass(frozen=True)
class ExportedDsl:
    text: str
    # 워크플로우 이름으로 지은 파일 이름과, 한글을 못 읽는 클라이언트용 slug 이름.
    filename: str
    ascii_filename: str
    # include_secrets로 비밀 환경 변수 값을 하나라도 평문으로 넣었는지.
    secrets_included: bool


def export_filename(name: str) -> str:
    stem = _UNSAFE_FILENAME.sub("_", name).strip(" .")[:100] or "workflow"
    return f"{stem}.yml"


def _kind_name(kind: str) -> WorkflowKindName:
    return "chatflow" if kind == WorkflowKind.CHATFLOW else "workflow"


class WorkflowDslService:
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
        # None이면 기본 레지스트리(노드 버전 판정·검증).
        self._registry = registry
        self._clock = clock
        self._cipher_cache: SecretCipher | None = None

    def _cipher(self) -> SecretCipher:
        if self._cipher_cache is None:
            try:
                self._cipher_cache = cipher_from_settings(self._settings)
            except CryptoError as exc:
                raise ServiceError(
                    503, "encryption_key_missing", f"비밀 환경변수를 다룰 수 없다: {exc}"
                ) from None
        return self._cipher_cache

    async def export(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        version: ExportVersion = "draft",
        include_secrets: bool = False,
    ) -> ExportedDsl:
        """초안(기본)·현재 발행 버전·버전 id 하나를 우리 YAML로. 비밀값은 include_secrets일 때만."""
        async with self._db.read() as session:
            workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
            if workflow is None:
                raise not_found("워크플로우")
            row = await self._version(session, workspace_id, workflow, version)
            graph = parse_graph(row.graph)
            if graph is None:
                raise ServiceError(422, "graph_unreadable", "저장된 그래프를 읽지 못했다")
            agents = await AgentRepository(session, workspace_id).get_many_active(
                agent_references(graph)
            )
        env, included = self._export_env(stored_env(row.env_vars), include_secrets)
        document = build_document(
            kind=_kind_name(workflow.kind),
            name=workflow.name,
            icon=workflow.icon,
            description=workflow.description,
            graph=graph,
            env_vars=env,
            conversation_vars=conversation_vars(row.conversation_vars),
            settings=parse_settings(row.settings),
            agent_slugs={agent.id: agent.slug for agent in agents},
        )
        text = await asyncio.to_thread(dump_yaml, document)
        log.info(
            "workflow.exported",
            workflow_id=str(workflow_id),
            version_id=str(row.id),
            secrets_included=included,
        )
        return ExportedDsl(
            text=text,
            filename=export_filename(workflow.name),
            ascii_filename=f"{workflow.slug}.yml",
            secrets_included=included,
        )

    async def import_text(
        self, workspace_id: uuid.UUID, text: str, *, into: uuid.UUID | None = None
    ) -> WorkflowImportOut:
        """우리 DSL·Dify DSL YAML을 새 워크플로우로, 또는 into 워크플로우의 초안으로 들인다.

        저장한 뒤 검증하고 결과를 issues로 돌려준다(오류가 있어도 저장한다). 읽을 수 없는 YAML,
        2MB 초과, 더 새 버전 같은 거부 사유는 422다.
        """
        size = len(text.encode("utf-8"))
        if size > MAX_DSL_BYTES:
            raise ServiceError(
                422,
                "dsl_too_large",
                f"DSL 파일은 {MAX_DSL_BYTES // 2**20}MB까지다({size}바이트)",
                details={"max_bytes": MAX_DSL_BYTES},
            )
        try:
            prepared = await asyncio.to_thread(read_dsl, text, self._registry)
        except DslError as exc:
            raise ServiceError(422, exc.code, exc.message, details=exc.details) from None
        document = prepared.document
        size = len(graph_json(document.graph).encode("utf-8"))
        if size > MAX_GRAPH_BYTES:
            raise ServiceError(
                422,
                "graph_too_large",
                f"그래프 JSON은 {MAX_GRAPH_BYTES // 2**20}MB까지다({size}바이트)",
            )
        try:
            async with self._db.write() as session:
                graph, linked = await self._link_agents(
                    AgentRepository(session, workspace_id), document.graph, prepared.agents
                )
                if into is None:
                    workflow = await self._create(session, workspace_id, prepared, graph)
                else:
                    workflow = await self._overwrite(session, workspace_id, into, prepared, graph)
                agents = await live_agent_ids(session, workspace_id, graph)
        except IntegrityError:
            raise ServiceError(409, "slug_taken", "slug가 다른 워크플로우와 겹친다") from None
        issues = check_graph(
            graph,
            document.kind,
            env_names=[item.name for item in document.env_vars],
            conversation=document.conversation_vars,
            registry=self._registry,
            agent_ids=agents,
        )
        log.info(
            "workflow.imported",
            workflow_id=str(workflow.id),
            source=prepared.source,
            into=into is not None,
            nodes=len(graph.nodes),
            unsupported=len(prepared.unsupported),
        )
        return WorkflowImportOut(
            workflow_id=workflow.id,
            issues=issues,
            converted=DslImportSummary(
                source=prepared.source,
                nodes=len(graph.nodes),
                unsupported=prepared.unsupported,
                warnings=[*prepared.warnings, *linked],
            ),
        )

    async def _version(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        workflow: Workflow,
        version: ExportVersion,
    ) -> WfVersion:
        versions = WfVersionRepository(session, workspace_id)
        if version == "draft":
            row, what = await versions.get_draft(workflow.id), "워크플로우 초안"
        elif version == "published":
            published = workflow.published_version_id
            row = await versions.get(published) if published is not None else None
            what = "발행 버전"
        else:
            row, what = await versions.get_for_workflow(workflow.id, version), "워크플로우 버전"
        if row is None:
            raise not_found(what)
        return row

    def _export_env(
        self, items: Sequence[StoredWfEnvVar], include_secrets: bool
    ) -> tuple[list[DslEnvVar], bool]:
        env: list[DslEnvVar] = []
        included = False
        for item in items:
            if not item.secret:
                env.append(DslEnvVar(name=item.name, value=item.value))
                continue
            value: str | None = None
            if include_secrets and item.value_encrypted is not None:
                try:
                    value = self._cipher().decrypt(item.value_encrypted)
                except CryptoError:
                    raise ServiceError(
                        500,
                        "env_decrypt_failed",
                        f"환경변수 {item.name}을(를) 복호화하지 못했다. ENCRYPTION_KEY가 "
                        "바뀌었는지 확인한다",
                    ) from None
                included = True
            env.append(DslEnvVar(name=item.name, value=value, secret=True))
        return env, included

    def _env_entries(
        self, incoming: Sequence[DslEnvVar], stored: Sequence[StoredWfEnvVar]
    ) -> list[dict[str, Any]]:
        existing = {item.name: item for item in stored}
        entries: list[dict[str, Any]] = []
        for item in incoming:
            if item.value is None:
                old = existing.get(item.name)
                if old is not None and old.secret == item.secret:
                    kept = old.value_encrypted if old.secret else old.value
                    if kept is not None:
                        entries.append(old.model_dump())
                        continue
                entries.append(StoredWfEnvVar(name=item.name, secret=item.secret).model_dump())
            elif item.secret:
                sealed = self._cipher().encrypt(item.value)
                entries.append(
                    StoredWfEnvVar(name=item.name, secret=True, value_encrypted=sealed).model_dump()
                )
            else:
                entries.append(StoredWfEnvVar(name=item.name, value=item.value).model_dump())
        return entries

    async def _link_agents(
        self, agents: AgentRepository, graph: Graph, refs: Sequence[AgentRef]
    ) -> tuple[Graph, list[DslWarning]]:
        """agent 노드의 agent_id가 이 워크스페이스에 없으면 파일에 적힌 slug로 찾아 바꾼다."""
        if not refs:
            return graph, []
        wanted = {ref.agent_id for ref in refs if ref.agent_id is not None}
        alive = {row.id for row in await agents.get_many_active(wanted)}
        fixed: dict[str, str] = {}
        warnings: list[DslWarning] = []
        for ref in refs:
            if ref.agent_id is not None and ref.agent_id in alive:
                continue
            found = await agents.get_active_by_slug(ref.slug) if ref.slug else None
            if found is not None:
                fixed[ref.node_id] = str(found.id)
                message = f"에이전트를 slug {ref.slug}(으)로 찾아 이었다"
                warnings.append(
                    DslWarning(code="agent_relinked", message=message, node_id=ref.node_id)
                )
                continue
            if ref.agent_id is None:
                fixed[ref.node_id] = str(MISSING_AGENT_ID)
            who = ref.slug or (str(ref.agent_id) if ref.agent_id is not None else "(id 없음)")
            message = f"에이전트 {who}을(를) 찾지 못했다. 노드에서 에이전트를 다시 고른다"
            warnings.append(
                DslWarning(code="agent_not_found", message=message, node_id=ref.node_id)
            )
        nodes = [
            node.model_copy(update={"config": {**node.config, "agent_id": fixed[node.id]}})
            if node.id in fixed
            else node
            for node in graph.nodes
        ]
        return graph.model_copy(update={"nodes": nodes}), warnings

    async def _create(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        prepared: PreparedImport,
        graph: Graph,
    ) -> Workflow:
        document = prepared.document
        workflows = WorkflowRepository(session, workspace_id)
        workflow = workflows.add(
            Workflow(
                id=new_id(),
                slug=await free_slug(workflows, document.name),
                name=document.name,
                icon=document.icon,
                description=document.description,
                kind=document.kind,
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
                env_vars=self._env_entries(document.env_vars, []),
                conversation_vars=[
                    item.model_dump(mode="json") for item in document.conversation_vars
                ],
                settings=document.settings.model_dump(mode="json"),
                graph_hash=graph_hash(graph),
            )
        )
        await versions.flush()
        workflow.draft_version_id = draft.id
        await workflows.flush()
        return workflow

    async def _overwrite(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        prepared: PreparedImport,
        graph: Graph,
    ) -> Workflow:
        document = prepared.document
        workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
        if workflow is None:
            raise not_found("워크플로우")
        if workflow.kind != document.kind:
            raise ServiceError(
                409,
                "kind_mismatch",
                f"{document.kind} DSL은 {workflow.kind} 워크플로우에 덮어쓸 수 없다"
                "(kind는 바꿀 수 없다)",
            )
        versions = WfVersionRepository(session, workspace_id)
        draft = await versions.get_draft(workflow.id)
        if draft is None:
            raise not_found("워크플로우 초안")
        draft.graph = graph.model_dump(mode="json")
        draft.graph_hash = graph_hash(graph)
        draft.env_vars = self._env_entries(document.env_vars, stored_env(draft.env_vars))
        draft.conversation_vars = [
            item.model_dump(mode="json") for item in document.conversation_vars
        ]
        draft.settings = document.settings.model_dump(mode="json")
        draft.updated_at = next_stamp(self._clock, draft.updated_at)
        await versions.flush()
        return workflow
