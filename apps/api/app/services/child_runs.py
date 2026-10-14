"""call_agent로 만든 자식 런의 DB 쪽 일(오케스트레이션 설계 3.3~3.8, 4장, 9장).

자식 만들기(버전 해석·검사·파일 스테이징·TaskService.create(enqueue=False)), 큐 투입, 자식이 끝났을
때 부모를 다시 넣는 훅, 하위 트리 중지, 고아 취소, 자동 승인 전파, 결과 파일 복사,
안전망(reconcile)을 모은다. 결과 요약·tool_result 문구 같은 순수 규칙은 app/agent/child_runs.py에
있다.

자식 종료 훅은 두 단계다. claim_parent는 자식의 끝을 쓰는 쓰기 트랜잭션 안에서 부모를 queued +
resume_payload로 바꾸기만 하고, child_run.completed 발행과 큐 투입(launch_parent)은 트랜잭션 밖에서
한다. Database.write()는 재진입하지 않고, 트랜잭션 안에서 버스에 발행하면 writer의 flush가 같은
잠금을 기다려 교착한다. 부모 상태를 조건으로 보고 바꾸므로 어느 경로(실행기
종료·중지·만료·복구·reconcile)에서 불려도 부모는 한 번만 다시 들어간다.

워크플로우 런이 부모인 자식(2단계 agent 노드, 9장의 ParentRef{kind: wf_run})은 start_node_child가
만든다. 버전 규칙(2.4)과 승인 끄기는 call_agent의 자식과 같고, run.parent_run_id는 비운다(부모는
wf_node_run.child_run_id가 가리킨다). 부모 재투입은 claim_parent가 아니라 M12의 WorkflowParents가
맡는다. call_agent의 부모는 자식이 끝나야 이어 가지만 워크플로우 런은 자식이 입력 대기에서 벗어나는
순간 다시 들어가 자식을 기다리고, 자식이 또 멈추면 새 승인 카드로 다시 멈춰야 하기 때문이다.
고아 취소는 같은 orphan_runs·cancel_orphans를 쓴다(recovery.recover_workflows).
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.child_runs import (
    MAX_CHILD_FILE_BYTES,
    MAX_CHILD_FILES,
    MAX_CHILD_FILES_BYTES,
    RESULT_FILE,
    ChildCall,
    ChildCorrelation,
    ChildError,
    ChildFile,
    ChildResultSummary,
    ChildStarted,
    ChildStartError,
    SubAgentChoice,
    VersionKind,
    child_files_dir,
    summarize_output,
    task_preview,
)
from app.agent.file_collector import guess_mime
from app.agent.interactions import SESSION_AUTO_APPROVE, PendingInteraction, ResumePayload
from app.agent.task_service import AgentTaskSpec, TaskSpecError, check_output_schema
from app.config import Settings
from app.db.engine import Database
from app.db.models import Agent, Run, SessionFile
from app.db.models.enums import RunStatus, SessionFileKind
from app.db.repositories.agents import AgentRepository
from app.db.repositories.runs import ACTIVE_STATUSES, RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.runner.supervisor import Supervisor
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.sandbox.paths import relative_posix, resolve_in_workspace
from app.sandbox.provisioning import WorkspaceProvisioner
from app.schemas.agent_version import ResultSpec, SubAgentRef
from app.schemas.common import RunLimits
from app.schemas.events import UsageTotal
from app.services.errors import ServiceError, not_found
from app.services.runs import (
    TERMINAL_STATUSES,
    ChildRunListener,
    RunService,
    StartedRun,
    completed_data,
    pending_of,
    run_key,
    run_ref,
    task_input_of,
    task_output,
)
from app.services.tasks import ChildTaskOrigin, TaskService
from app.storage.base import StorageBackend, StorageError, safe_tool_call_id, staged_file_key
from app.streaming.event_bus import EventBus, RunRef

log = structlog.get_logger("app.services.child_runs")

ORPHANED_MESSAGE = "이 런을 기다리던 부모 런이 끝나 함께 취소했다"
TITLE_TASK_CHARS = 50
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ParentRun:
    """call_agent를 부른 런. 실행기가 런 행·세션·버전에서 채운다."""

    workspace_id: uuid.UUID
    run_id: uuid.UUID
    session_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    # 초안(미리보기 세션)으로 도는 런인지. 하위 에이전트의 버전을 고르는 규칙이 다르다(2.4).
    draft: bool
    depth: int
    root_run_id: uuid.UUID
    # 뿌리부터 이 런 자신까지의 에이전트 id. 여기에 든 에이전트는 다시 부를 수 없다(순환).
    ancestors: tuple[uuid.UUID, ...]
    max_child_calls: int
    approval_override: str | None
    external_user: str | None
    api_key_id: uuid.UUID | None
    workspace_root: Path


@dataclass(frozen=True)
class NodeParent:
    """agent 노드의 자식을 만드는 워크플로우 런(9장 ParentRef{kind: wf_run})."""

    workspace_id: uuid.UUID
    wf_run_id: uuid.UUID
    workflow_id: uuid.UUID
    # 자식 세션 제목의 앞부분(워크플로우 이름).
    label: str
    # 초안으로 도는 런(미리보기·단일 노드 실행)인지. 자식 버전 규칙이 다르다(2.4).
    draft: bool
    # 버전 settings.approval_mode=off(2단계 9.1). 자식의 승인을 모두 끈다.
    approval_off: bool


@dataclass(frozen=True)
class ClaimedParent:
    """claim_parent가 queued로 돌린 부모. launch_parent가 트랜잭션 밖에서 발행·투입한다."""

    ref: RunRef
    tool_call_id: str
    summary: ChildResultSummary


def _error(code: str, message: str) -> ChildStartError:
    return ChildStartError(code=code, message=message)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _file_size(path: Path) -> int | None:
    return path.stat().st_size if path.is_file() else None


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def child_title(parent_agent: str, task: str) -> str:
    """자식 세션 제목(7장): "<부모 에이전트 이름> → <task 첫 줄 50자>"."""
    return f"{parent_agent} → {_first_line(task)[:TITLE_TASK_CHARS] or '과업'}"


def resolve_version(agent: Agent, *, draft_parent: bool) -> tuple[uuid.UUID, VersionKind] | None:
    """자식이 쓸 버전(2.4). 발행 버전이 우선이고, 초안 부모만 자식의 초안으로 내려간다."""
    if agent.published_version_id is not None:
        return agent.published_version_id, "published"
    if draft_parent and agent.draft_version_id is not None:
        return agent.draft_version_id, "draft"
    return None


async def summarize_child(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    child: Run,
    pending: PendingInteraction,
    now: datetime,
) -> ChildResultSummary:
    output = child.output
    if output is None and task_input_of(child) is not None:
        # 러너를 거치지 않고 끝난 자식(복구가 failed로 닫은 런 등)은 output이 비어 있다.
        output = await task_output(session, workspace_id, child, status=child.status, result=None)
    ended = child.ended_at or now
    result = pending.result or ResultSpec()
    return summarize_output(
        run_id=child.id,
        session_id=child.session_id,
        agent_slug=pending.agent_slug or "",
        agent_name=pending.agent_name or "",
        result_format=result.format,
        status=child.status,
        output=output,
        error=child.error,
        steps=child.steps,
        usage=child.usage,
        duration_ms=int((ended - child.queued_at).total_seconds() * 1000),
    )


def _claim(parent: Run, pending: PendingInteraction, summary: ChildResultSummary) -> ClaimedParent:
    payload = ResumePayload(interaction_id=pending.interaction_id, kind="agent_call", child=summary)
    parent.status = RunStatus.QUEUED
    parent.resume_payload = payload.model_dump(mode="json")
    parent.cancel_requested_at = None
    return ClaimedParent(ref=run_ref(parent), tool_call_id=pending.tool_call_id, summary=summary)


async def claim_parent(
    session: AsyncSession, workspace_id: uuid.UUID, child: Run, now: datetime
) -> ClaimedParent | None:
    """호출자의 쓰기 트랜잭션 안에서 부른다(3.3). 끝난 child를 기다리는 부모만 queued로 돌린다.

    부모가 이미 끝났거나(중지) 다른 자식을 기다리면 None이다.
    """
    if child.parent_run_id is None or child.status not in TERMINAL_STATUSES:
        return None
    parent = await RunRepository(session, workspace_id).get(child.parent_run_id)
    if parent is None or parent.status != RunStatus.WAITING_INPUT:
        return None
    pending = pending_of(parent)
    if pending is None or pending.kind != "agent_call" or pending.child_run_id != child.id:
        return None
    summary = await summarize_child(session, workspace_id, child, pending, now)
    log.info("child.parent_claimed", parent_run_id=str(parent.id), child_run_id=str(child.id))
    return _claim(parent, pending, summary)


def _orphan(run: Run, now: datetime, message: str) -> None:
    run.status = RunStatus.CANCELLED
    run.error = {"code": "orphaned", "message": message, "retryable": False}
    run.pending_interaction = None
    run.resume_payload = None
    run.ended_at = now


async def orphan_runs(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    runs: Sequence[Run],
    now: datetime,
    *,
    message: str = ORPHANED_MESSAGE,
) -> list[tuple[RunRef, dict[str, object]]]:
    """기다릴 부모가 없는 자식을 cancelled(code=orphaned)로 닫는다.

    트랜잭션 밖에서 발행할 (ref, run.completed data)를 준다.
    """
    closed: list[tuple[RunRef, dict[str, object]]] = []
    for run in runs:
        if run.status not in (RunStatus.QUEUED, RunStatus.WAITING_INPUT):
            continue
        _orphan(run, now, message)
        if task_input_of(run) is not None:
            run.output = await task_output(
                session, workspace_id, run, status=run.status, result=None
            )
        closed.append((run_ref(run), completed_data(run, status=RunStatus.CANCELLED)))
    return closed


async def cancel_orphans(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    parent_ids: Sequence[uuid.UUID],
    now: datetime,
) -> list[tuple[RunRef, dict[str, object]]]:
    """끝난 부모들의 queued·waiting_input 자식을 손자까지 닫는다(3.8의 2번).

    호출자의 쓰기 트랜잭션 안에서 부른다.
    """
    runs = RunRepository(session, workspace_id)
    closed: list[tuple[RunRef, dict[str, object]]] = []
    level = list(parent_ids)
    seen: set[uuid.UUID] = set(level)
    while level:
        children = [
            run
            for run in await runs.list_children(
                level, statuses=(RunStatus.QUEUED, RunStatus.WAITING_INPUT)
            )
            if run.id not in seen
        ]
        closed.extend(await orphan_runs(session, workspace_id, children, now))
        seen.update(run.id for run in children)
        level = [run.id for run in children]
    return closed


async def propagate_auto_approve_up(
    session: AsyncSession, workspace_id: uuid.UUID, run: Run
) -> int:
    """자식의 승인 카드에서 "이 대화에서는 묻지 않기"를 켰다. 조상 런의 세션에도 켠다(3.5).

    사용자가 보는 "이 대화"는 부모 대화이기 때문이다. 바꾼 세션 수를 준다.
    """
    sessions = SessionRepository(session, workspace_id)
    changed = 0
    for ancestor in await RunRepository(session, workspace_id).parent_chain(run.id):
        chat = await sessions.get(ancestor.session_id)
        if chat is not None and chat.approval_override != SESSION_AUTO_APPROVE:
            chat.approval_override = SESSION_AUTO_APPROVE
            changed += 1
    return changed


async def propagate_auto_approve_down(
    session: AsyncSession, workspace_id: uuid.UUID, session_id: uuid.UUID, value: str | None
) -> int:
    """부모 세션 헤더에서 자동 승인을 켜거나 껐다. 열린 자식 세션(손자까지)에도 같은 값을 쓴다(3.5).

    이미 승인을 기다리며 멈춘 자식에는 다음 호출부터 적용된다(1단계와 같다).
    """
    runs = RunRepository(session, workspace_id)
    sessions = SessionRepository(session, workspace_id)
    active = await runs.active_for_session(session_id)
    if active is None:
        return 0
    changed = 0
    level = [active.id]
    seen = {active.id}
    while level:
        children = [
            run
            for run in await runs.list_children(level, statuses=ACTIVE_STATUSES)
            if run.id not in seen
        ]
        for child in children:
            chat = await sessions.get(child.session_id)
            if chat is not None and chat.approval_override != value:
                chat.approval_override = value
                changed += 1
        seen.update(run.id for run in children)
        level = [run.id for run in children]
    return changed


def child_completed_data(claimed: ClaimedParent) -> dict[str, Any]:
    """child_run.completed의 data(6.1)."""
    summary = claimed.summary
    error = summary.error
    return {
        "tool_call_id": claimed.tool_call_id,
        "child_run_id": summary.run_id,
        "status": summary.status,
        "error": {"code": error.code, "message": error.message} if error is not None else None,
        "steps": summary.steps,
        "usage_total": summary.usage.model_dump(mode="json"),
        "duration_ms": summary.duration_ms,
        "files": len(summary.files) + summary.files_omitted,
    }


class ChildRunService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        supervisor: Supervisor | None,
        storage: StorageBackend,
        backend: SandboxBackend | None = None,
        clock: Callable[[], datetime] = utcnow,
        parents: ChildRunListener | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        # 에이전트 레인. 없으면(테스트의 하우스키핑 등) 투입은 건너뛰고 queued로 둔다. 기동 복구가
        # 넣는다.
        self._supervisor = supervisor
        self._storage = storage
        self._backend = backend
        self._clock = clock
        self._parents = parents

    def _runs(self) -> RunService | None:
        if self._supervisor is None:
            return None
        return RunService(
            self._db,
            self._settings,
            bus=self._bus,
            supervisor=self._supervisor,
            storage=self._storage,
            backend=self._backend,
            clock=self._clock,
            parents=self._parents,
        )

    async def resolve_choices(
        self, workspace_id: uuid.UUID, refs: Sequence[SubAgentRef]
    ) -> list[SubAgentChoice]:
        """버전의 sub_agents를 이 런이 부를 수 있는 목록으로. 지운 에이전트는 뺀다.

        이름·설명은 버전과 무관한 에이전트 정보라 지금 값을 쓴다(2.4).
        """
        if not refs:
            return []
        async with self._db.read() as session:
            agents = await AgentRepository(session, workspace_id).get_many_active(
                [ref.agent_id for ref in refs]
            )
        by_id = {agent.id: agent for agent in agents}
        choices: list[SubAgentChoice] = []
        for ref in refs:
            agent = by_id.get(ref.agent_id)
            if agent is None:
                continue
            choices.append(
                SubAgentChoice(
                    agent_id=agent.id,
                    slug=agent.slug,
                    name=agent.name,
                    description=agent.description,
                    result=ref.result,
                    approval_mode=ref.approval_mode,
                )
            )
        return choices

    async def create_child(
        self, parent: ParentRun, call: ChildCall
    ) -> ChildStarted | ChildStartError:
        """자식 과업 런 행을 만든다(3.3). 큐에는 넣지 않는다. 검사에 걸리면 ChildStartError."""
        choice = call.agent
        ws = parent.workspace_id
        depth = parent.depth + 1
        if depth > self._settings.orchestration_max_depth:
            return _error(
                "depth_exceeded",
                f"하위 에이전트 호출 깊이 한도({self._settings.orchestration_max_depth})에 닿았다",
            )
        if choice.agent_id in parent.ancestors:
            return _error(
                "agent_ref_cycle",
                f"{choice.slug}은(는) 이 호출 사슬의 상위 에이전트라 다시 부를 수 없다",
            )
        schema = choice.result.json_schema if choice.result.format == "json" else None
        if call.output_schema is not None:
            if not choice.schema_from_call:
                return _error(
                    "output_schema_not_allowed",
                    f"{choice.slug}의 결과 형식은 빌더에서 정해져 있어 output_schema를 받지 않는다",
                )
            try:
                check_output_schema(call.output_schema)
            except TaskSpecError as exc:
                return _error(exc.code, exc.message)
            schema = call.output_schema
        elif choice.schema_from_call:
            return _error(
                "output_schema_required",
                f"{choice.slug}은(는) JSON 결과를 돌려준다. output_schema로 결과 스키마를 준다",
            )
        async with self._db.read() as session:
            agent = await AgentRepository(session, ws).get_active(choice.agent_id)
            if agent is None:
                return _error("agent_not_found", f"하위 에이전트 {choice.slug}이(가) 지워졌다")
            version = resolve_version(agent, draft_parent=parent.draft)
            if version is None:
                return _error(
                    "agent_not_published",
                    f"하위 에이전트 {agent.name}({agent.slug})에 발행한 버전이 없다",
                )
            runs = RunRepository(session, ws)
            own = (await runs.count_children([parent.run_id])).get(parent.run_id, 0)
            if own >= parent.max_child_calls:
                return _error(
                    "child_limit",
                    f"이 런의 하위 에이전트 호출 한도({parent.max_child_calls}회)를 다 썼다",
                )
            root_limit = await self._root_limit(session, ws, parent)
            used = await runs.tree_child_count(parent.root_run_id)
            if used >= root_limit:
                return _error(
                    "child_limit", f"이 대화의 하위 에이전트 호출 한도({root_limit}회)를 다 썼다"
                )
            # 부모 런 도중에 세션 헤더에서 바꿨을 수 있어 지금 값을 물려준다(3.5).
            chat = await SessionRepository(session, ws).get(parent.session_id)
            override = chat.approval_override if chat is not None else parent.approval_override
        staged = await self.stage_files(parent, call)
        if isinstance(staged, ChildStartError):
            return staged
        version_id, version_kind = version
        correlation = ChildCorrelation(
            parent_run_id=parent.run_id,
            parent_session_id=parent.session_id,
            root_run_id=parent.root_run_id,
            tool_call_id=call.tool_call_id,
            depth=depth,
            ancestors=list(parent.ancestors),
            parent_agent_name=parent.agent_name,
        )
        spec = AgentTaskSpec(
            workspace_id=ws,
            agent_id=agent.id,
            agent_version_id=version_id,
            instruction=call.task,
            output_schema=schema,
            files=staged,
            approval_mode="off" if choice.approval_mode == "off" else None,
            session_approval_override="off" if override == SESSION_AUTO_APPROVE else None,
            session_mode="ephemeral",
            external_user=parent.external_user,
            correlation=correlation.model_dump(mode="json"),
        )
        origin = ChildTaskOrigin(
            parent_run_id=parent.run_id,
            title=child_title(parent.agent_name, call.task),
            meta={
                "session_mode": "ephemeral",
                "parent_session_id": str(parent.session_id),
                "parent_run_id": str(parent.run_id),
                "parent_agent_name": parent.agent_name,
            },
            api_key_id=parent.api_key_id,
            # 이 자식을 넣은 뒤 트리에 남는 호출 수가 자식(과 그 아래)의 상한이다(3.9).
            child_calls_cap=max(root_limit - used - 1, 0),
        )
        runs_service = self._runs()
        if runs_service is None:
            return _error("child_unavailable", "자식 런을 넣을 실행 레인이 없다")
        tasks = TaskService(self._db, self._settings, runs=runs_service, bus=self._bus)
        try:
            started = await tasks.create(spec, subscribe=False, enqueue=False, origin=origin)
        except ServiceError as exc:
            return _error(exc.code, exc.message)
        log.info(
            "child.created",
            parent_run_id=str(parent.run_id),
            child_run_id=str(started.run_id),
            agent=choice.slug,
            version=version_kind,
            depth=depth,
            files=len(staged),
        )
        return ChildStarted(
            child_run_id=started.run_id,
            child_session_id=started.session_id,
            agent_id=agent.id,
            agent_slug=agent.slug,
            agent_name=agent.name,
            version=version_kind,
            depth=depth,
            result_format=choice.result.format,
            task_preview=task_preview(call.task),
        )

    async def _root_limit(self, session: AsyncSession, ws: uuid.UUID, parent: ParentRun) -> int:
        if parent.root_run_id == parent.run_id:
            return parent.max_child_calls
        root = await RunRepository(session, ws).get(parent.root_run_id)
        if root is None:
            return parent.max_child_calls
        try:
            return RunLimits.model_validate(root.limits or {}).max_child_calls
        except ValidationError:
            return parent.max_child_calls

    async def start_node_child(self, parent: NodeParent, spec: AgentTaskSpec) -> StartedRun:
        """워크플로우 agent 노드의 자식 과업을 만들어 에이전트 레인에 넣는다(2단계 3.7, 9장).

        call_agent의 자식과 달리 바로 큐에 넣는다. 워크플로우 런은 다른 레인에서 기다리므로 교착이
        없다(2단계 3.6). 지시는 노드가 이미 렌더했다. 실패는 ServiceError다.
        """
        ws = parent.workspace_id
        update: dict[str, Any] = {
            "workspace_id": ws,
            "correlation": {**spec.correlation, "workflow_run_id": str(parent.wf_run_id)},
        }
        if parent.approval_off:
            update["approval_mode"] = "off"
        if spec.agent_version_id is None:
            async with self._db.read() as session:
                agent = await AgentRepository(session, ws).get_active(spec.agent_id)
            if agent is None:
                raise not_found("에이전트")
            version = resolve_version(agent, draft_parent=parent.draft)
            if version is None:
                raise ServiceError(
                    409,
                    "agent_not_published",
                    f"에이전트 {agent.name}({agent.slug})에 발행한 버전이 없다. 먼저 발행한다",
                )
            update["agent_version_id"] = version[0]
        spec = spec.model_copy(update=update)
        runs = self._runs()
        if runs is None:
            raise ServiceError(503, "child_unavailable", "자식 런을 넣을 실행 레인이 없다")
        origin = ChildTaskOrigin(
            parent_run_id=None,
            title=child_title(parent.label, spec.instruction),
            meta={
                "session_mode": "ephemeral",
                "workflow_id": str(parent.workflow_id),
                "workflow_run_id": str(parent.wf_run_id),
                "node_id": spec.correlation.get("node_id"),
            },
            api_key_id=None,
            child_calls_cap=None,
        )
        tasks = TaskService(self._db, self._settings, runs=runs, bus=self._bus)
        started = await tasks.create(spec, subscribe=False, origin=origin)
        log.info(
            "child.node_child_created",
            wf_run_id=str(parent.wf_run_id),
            child_run_id=str(started.run_id),
            node_id=spec.correlation.get("node_id"),
            files=len(spec.files),
        )
        return started

    def queue_position(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> int | None:
        """에이전트 레인 대기열에서 몇 번째인지. 레인이 없거나 큐에 없으면 None."""
        if self._supervisor is None:
            return None
        return self._supervisor.position(run_key(workspace_id, run_id))

    async def stage_files(
        self, parent: ParentRun, call: ChildCall
    ) -> list[uuid.UUID] | ChildStartError:
        """files로 넘긴 부모 작업공간 파일을 session_file 행으로(4.1).

        부모 세션에 같은 경로·같은 내용의 행이 이미 있으면 그 행을 쓰고, 없으면 storage에 복사해
        부모 런의 출력 행(kind=output)을 만든다. 1단계 과업 흐름이 그 행을 자식 task/files/로
        복사한다.
        """
        if not call.files:
            return []
        if len(call.files) > MAX_CHILD_FILES:
            return _error("file_too_large", f"넘길 파일은 {MAX_CHILD_FILES}개까지다")
        root = parent.workspace_root
        found: list[tuple[str, Path, int]] = []
        total = 0
        for rel in call.files:
            try:
                path = await asyncio.to_thread(resolve_in_workspace, root, rel)
                size = await asyncio.to_thread(_file_size, path)
            except (SandboxError, OSError) as exc:
                message = exc.message if isinstance(exc, SandboxError) else str(exc)
                return _error("file_not_found", f"{rel}: {message}")
            if size is None:
                return _error("file_not_found", f"작업공간에 그런 파일이 없다: {rel}")
            if size > MAX_CHILD_FILE_BYTES:
                return _error(
                    "file_too_large", f"{rel}: 파일 하나는 {MAX_CHILD_FILE_BYTES // 2**20}MB까지다"
                )
            total += size
            if total > MAX_CHILD_FILES_BYTES:
                return _error(
                    "file_too_large", f"넘길 파일은 합쳐서 {MAX_CHILD_FILES_BYTES // 2**20}MB까지다"
                )
            found.append((relative_posix(root.resolve(), path), path, size))
        ids: list[uuid.UUID] = []
        call_key = safe_tool_call_id(call.tool_call_id)
        for rel, path, size in found:
            try:
                digest = await asyncio.to_thread(_hash_file, path)
            except OSError as exc:
                return _error("file_not_found", f"{rel}: {exc}")
            async with self._db.read() as session:
                existing = await SessionFileRepository(session, parent.workspace_id).find_by_hash(
                    parent.session_id, rel, digest
                )
            if existing is not None:
                ids.append(existing.id)
                continue
            key = staged_file_key(parent.run_id, call_key, rel)
            try:
                stored = await self._storage.write_file(key, path, max_bytes=MAX_CHILD_FILE_BYTES)
            except (StorageError, OSError) as exc:
                return _error("file_not_found", f"{rel}: 파일을 옮기지 못했다({exc})")
            filename = rel.rsplit("/", 1)[-1]
            async with self._db.write() as session:
                row = SessionFileRepository(session, parent.workspace_id).add(
                    SessionFile(
                        id=new_id(),
                        session_id=parent.session_id,
                        run_id=parent.run_id,
                        message_id=call.message_id,
                        kind=SessionFileKind.OUTPUT,
                        path=rel,
                        filename=filename,
                        mime=guess_mime(filename),
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_key=key,
                    )
                )
                ids.append(row.id)
            log.info("child.file_staged", run_id=str(parent.run_id), path=rel, size=size)
        return ids

    async def launch_child(self, ref: RunRef) -> None:
        """부모가 대기 상태를 저장한 뒤 자식을 큐에 넣는다(3.3). 종료 중이면 queued로 둔다."""
        runs = self._runs()
        if runs is None:
            return
        async with self._db.read() as session:
            child = await RunRepository(session, ref.workspace_id).get(ref.run_id)
        if child is None or child.status != RunStatus.QUEUED:
            # 그사이 중지됐다(부모 중지의 하위 트리 취소).
            return
        await runs.enqueue(ref, subscribe=False, leave_if_closed=True)

    async def launch_parent(self, claimed: ClaimedParent) -> None:
        """claim_parent가 돌린 부모를 트랜잭션 밖에서 알리고 넣는다(3.3, 3.8)."""
        await self._bus.publish(claimed.ref, "child_run.completed", child_completed_data(claimed))
        runs = self._runs()
        if runs is None:
            return
        await runs.enqueue(claimed.ref, subscribe=False, leave_if_closed=True)

    async def settle_if_finished(self, workspace_id: uuid.UUID, child_run_id: uuid.UUID) -> None:
        """자식이 부모의 대기 저장보다 먼저 끝났으면 여기서 부모를 넣는다(3.3의 c)."""
        async with self._db.read() as session:
            child = await RunRepository(session, workspace_id).get(child_run_id)
        if child is None or child.status not in TERMINAL_STATUSES:
            return
        async with self._db.write() as session:
            fresh = await RunRepository(session, workspace_id).get(child_run_id)
            claimed = (
                await claim_parent(session, workspace_id, fresh, self._clock())
                if fresh is not None
                else None
            )
        if claimed is not None:
            await self.launch_parent(claimed)

    async def cancel_subtree(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> int:
        """run_id의 열린 자식을 RunService.stop과 같은 경로로 멈춘다(3.6).

        손자는 자식의 stop이 같은 방식으로 잇는다.
        """
        runs = self._runs()
        async with self._db.read() as session:
            children = await RunRepository(session, workspace_id).list_children(
                [run_id], statuses=ACTIVE_STATUSES
            )
        if not children:
            return 0
        if runs is None:
            # 레인이 없으면 상태만 닫는다(대기·큐 자식). 실행 중인 자식은 이 프로세스에 없다.
            async with self._db.write() as session:
                fresh = await RunRepository(session, workspace_id).list_children(
                    [run_id], statuses=(RunStatus.QUEUED, RunStatus.WAITING_INPUT)
                )
                closed = await orphan_runs(session, workspace_id, fresh, self._clock())
            for ref, data in closed:
                await self._bus.publish(ref, "run.completed", data)
                self._bus.end(ref.run_id)
            return len(closed)
        stopped = 0
        for child in children:
            try:
                await runs.stop(workspace_id, child.id)
                stopped += 1
            except ServiceError as exc:
                if exc.code != "run_not_active":
                    log.warning("child.cancel_failed", child_run_id=str(child.id), code=exc.code)
        return stopped

    async def copy_results_to_parent(
        self,
        workspace_id: uuid.UUID,
        handle: WorkspaceHandle,
        pending: PendingInteraction,
        summary: ChildResultSummary,
    ) -> ChildResultSummary:
        """재개하는 부모 작업공간 agents/<slug>/<run_id>/로 자식 결과 파일을 복사한다(4.2).

        원본은 언제나 storage다(자식 작업공간은 ephemeral이라 이미 지워졌을 수 있다). 복사하지 못한
        파일은 목록에 "복사 실패"로 남는다. JSON 결과가 32KB를 넘으면 result.json으로 쓴다.
        """
        if self._backend is None:
            return summary
        spec = pending.result or ResultSpec()
        base = child_files_dir(summary.agent_slug or "agent", summary.run_id)
        provisioner = WorkspaceProvisioner(self._backend)
        files: list[ChildFile] = list(summary.files)
        if spec.files and files:
            wanted = [item.session_file_id for item in files if item.session_file_id is not None]
            async with self._db.read() as session:
                rows = await SessionFileRepository(session, workspace_id).get_many(wanted)
            keys = {row.id: row.storage_key for row in rows}
            copied: list[ChildFile] = []
            for item in files:
                key = keys.get(item.session_file_id) if item.session_file_id is not None else None
                if not item.downloadable:
                    copied.append(item)
                    continue
                if key is None:
                    copied.append(item.model_copy(update={"copy_failed": True}))
                    continue
                dest = f"{base}/{item.child_path}"
                try:
                    await provisioner.copy_file_in(handle, dest, self._storage.local_path(key))
                except (SandboxError, StorageError, OSError) as exc:
                    log.warning("child.result_copy_failed", path=dest, error=str(exc))
                    copied.append(item.model_copy(update={"copy_failed": True}))
                    continue
                copied.append(item.model_copy(update={"parent_path": dest}))
            files = copied
        update: dict[str, Any] = {"files": files}
        if summary.result_too_large:
            path = await self._write_result_json(workspace_id, handle, summary, base)
            if path is not None:
                update["result_path"] = path
        return summary.model_copy(update=update)

    async def _write_result_json(
        self,
        workspace_id: uuid.UUID,
        handle: WorkspaceHandle,
        summary: ChildResultSummary,
        base: str,
    ) -> str | None:
        async with self._db.read() as session:
            child = await RunRepository(session, workspace_id).get(summary.run_id)
        result = (child.output or {}).get("result") if child is not None else None
        if not isinstance(result, dict) or self._backend is None:
            return None
        path = f"{base}/{RESULT_FILE}"
        data = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            await self._backend.write_file(handle, path, data)
        except SandboxError as exc:
            log.warning("child.result_json_failed", path=path, error=exc.message)
            return None
        return path

    async def reconcile_children(self) -> int:
        """안전망(3.8). 이미 끝난 자식을 아직 기다리는 부모를 찾아 다시 넣는다.

        훅을 부르기 직전에 프로세스가 죽었거나 지난 종료 중에 놓친 것을 잡는다. 조건부 갱신이라 두
        번 돌아도 한 번만 넣는다.
        """
        now = self._clock()
        claimed: list[ClaimedParent] = []
        async with self._db.read() as session:
            workspace_ids = [row.id for row in await WorkspaceRepository(session).list_all()]
        for workspace_id in workspace_ids:
            async with self._db.write() as session:
                runs = RunRepository(session, workspace_id)
                for parent in await runs.list_by_status([RunStatus.WAITING_INPUT]):
                    pending = pending_of(parent)
                    if pending is None or pending.kind != "agent_call":
                        continue
                    child = (
                        await runs.get(pending.child_run_id)
                        if pending.child_run_id is not None
                        else None
                    )
                    if child is not None and child.status not in TERMINAL_STATUSES:
                        continue
                    if child is not None:
                        found = await claim_parent(session, workspace_id, child, now)
                    else:
                        found = _claim(parent, pending, _missing_child(pending))
                    if found is not None:
                        claimed.append(found)
        for item in claimed:
            await self.launch_parent(item)
        if claimed:
            log.warning("child.reconciled", parents=len(claimed))
        return len(claimed)


def _missing_child(pending: PendingInteraction) -> ChildResultSummary:
    return ChildResultSummary(
        run_id=pending.child_run_id or uuid.UUID(int=0),
        agent_slug=pending.agent_slug or "",
        agent_name=pending.agent_name or "",
        result_format=(pending.result or ResultSpec()).format,
        status="failed",
        error=ChildError(code="child_missing", message="기다리던 하위 에이전트 런이 없다"),
        usage=UsageTotal(),
    )
