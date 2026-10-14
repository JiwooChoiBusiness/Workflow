"""ChildRunService와 종료 훅(오케스트레이션 2.4, 3.3, 3.5, 3.9, 4.1).

버전 해석, 순환·깊이·호출 수·트리 예산 검사, 파일 스테이징, claim_parent의 조건부 갱신, 자동 승인의
양방향 전파를 DB에 직접 행을 넣어 본다. 실행 레인은 띄우지 않는다(큐에 넣기만 한다).
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.agent.child_runs import ChildCall, ChildStarted, ChildStartError, SubAgentChoice
from app.agent.interactions import PendingInteraction
from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import Agent, AgentVersion, ChatSession, Message, Run, SessionFile
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.runner.supervisor import CancelSignal, Supervisor
from app.schemas.agent_version import ResultSpec
from app.services import child_runs
from app.services.child_runs import (
    ChildRunService,
    ParentRun,
    claim_parent,
    propagate_auto_approve_down,
    propagate_auto_approve_up,
    resolve_version,
)
from app.storage import make_storage
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "additionalProperties": False,
}


async def never(run_id: str, cancel: CancelSignal) -> None:
    raise AssertionError("이 테스트는 런을 실행하지 않는다")


@dataclass
class Env:
    db: Database
    bus: EventBus
    supervisor: Supervisor
    service: ChildRunService
    workspace_id: uuid.UUID
    root: Path
    seq: int = 0

    async def agent(self, slug: str, *, published: bool = True) -> Agent:
        async with self.db.write() as session:
            agent = Agent(
                id=new_id(), workspace_id=self.workspace_id, slug=slug, name=f"{slug} 이름"
            )
            session.add(agent)
            await session.flush()
            draft = AgentVersion(
                id=new_id(),
                workspace_id=self.workspace_id,
                agent_id=agent.id,
                status="draft",
                approval_mode="all_shell",
            )
            session.add(draft)
            await session.flush()
            agent.draft_version_id = draft.id
            if published:
                version = AgentVersion(
                    id=new_id(),
                    workspace_id=self.workspace_id,
                    agent_id=agent.id,
                    version_number=1,
                    status="published",
                    approval_mode="all_shell",
                )
                session.add(version)
                await session.flush()
                agent.published_version_id = version.id
            return agent

    async def session(self, agent: Agent, *, override: str | None = None) -> ChatSession:
        async with self.db.write() as session:
            chat = ChatSession(
                id=new_id(),
                workspace_id=self.workspace_id,
                agent_id=agent.id,
                agent_version_id=agent.draft_version_id,
                source="preview",
                approval_override=override,
            )
            session.add(chat)
            return chat

    async def run(
        self,
        chat: ChatSession,
        *,
        status: str = "running",
        parent: uuid.UUID | None = None,
        pending: PendingInteraction | None = None,
        limits: dict[str, Any] | None = None,
    ) -> Run:
        self.seq += 1
        async with self.db.write() as session:
            message = Message(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=chat.id,
                seq=self.seq,
                role="assistant",
            )
            session.add(message)
            await session.flush()
            run = Run(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=chat.id,
                agent_version_id=chat.agent_version_id,
                trigger="chat",
                status=status,
                user_message_id=message.id,
                approval_mode="all_shell",
                limits=limits or {},
                parent_run_id=parent,
                pending_interaction=pending.model_dump(mode="json") if pending else None,
                assistant_message_id=message.id,
            )
            session.add(run)
            return run

    async def get(self, model: Any, row_id: uuid.UUID) -> Any:
        async with self.db.read() as session:
            return await session.get(model, row_id)

    def parent(self, agent: Agent, chat: ChatSession, run: Run, **fields: Any) -> ParentRun:
        values: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "run_id": run.id,
            "session_id": chat.id,
            "agent_id": agent.id,
            "agent_name": agent.name,
            "draft": True,
            "depth": 0,
            "root_run_id": run.id,
            "ancestors": (agent.id,),
            "max_child_calls": 20,
            "approval_override": chat.approval_override,
            "external_user": None,
            "api_key_id": None,
            "workspace_root": self.root,
        }
        values.update(fields)
        return ParentRun(**values)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    db = Database(tmp_path / "child.db")
    await upgrade_head(db)
    bus = EventBus(sqlite_stores(db), batch_window_sec=0.01)
    bus.start()
    supervisor = Supervisor(never)
    service = ChildRunService(
        db, settings, bus=bus, supervisor=supervisor, storage=make_storage(settings)
    )
    async with db.write() as session:
        workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
        workspace.id = new_id()
        workspace_id = workspace.id
    root = tmp_path / "ws"
    root.mkdir()
    try:
        yield Env(db, bus, supervisor, service, workspace_id, root)
    finally:
        await bus.close()
        await db.close()


def choice(agent: Agent, **fields: Any) -> SubAgentChoice:
    return SubAgentChoice(
        agent_id=agent.id, slug=agent.slug, name=agent.name, description="d", **fields
    )


def call_for(target: Agent, message_id: uuid.UUID, **fields: Any) -> ChildCall:
    values: dict[str, Any] = {
        "tool_call_id": "call_1",
        "message_id": message_id,
        "agent": choice(target),
        "task": "일을 한다 {{ 이건 템플릿이 아니다 }}",
    }
    values.update(fields)
    return ChildCall.model_validate(values)


def waiting_on(child_id: uuid.UUID, **fields: Any) -> PendingInteraction:
    values: dict[str, Any] = {
        "interaction_id": "i1",
        "kind": "agent_call",
        "tool_call_id": "call_1",
        "tool_name": "call_agent",
        "message_id": new_id(),
        "step": 1,
        "created_at": utcnow(),
        "child_run_id": child_id,
        "child_session_id": new_id(),
        "agent_id": new_id(),
        "agent_slug": "kid",
        "agent_name": "kid 이름",
        "version": "published",
        "result": ResultSpec(),
    }
    values.update(fields)
    return PendingInteraction.model_validate(values)


def test_version_rule() -> None:
    agent = Agent(slug="a", name="a")
    agent.draft_version_id, agent.published_version_id = new_id(), None
    assert resolve_version(agent, draft_parent=True) == (agent.draft_version_id, "draft")
    assert resolve_version(agent, draft_parent=False) is None
    agent.published_version_id = new_id()
    assert resolve_version(agent, draft_parent=True) == (agent.published_version_id, "published")
    assert resolve_version(agent, draft_parent=False) == (agent.published_version_id, "published")


async def test_create_child_makes_a_queued_row_without_enqueueing(env: Env) -> None:
    boss = await env.agent("boss")
    kid = await env.agent("kid", published=False)
    chat = await env.session(boss, override="off")
    run = await env.run(chat, limits={"max_child_calls": 5})
    parent = env.parent(boss, chat, run, max_child_calls=5)

    started = await env.service.create_child(parent, call_for(kid, run.user_message_id))

    assert isinstance(started, ChildStarted)
    assert started.version == "draft" and started.depth == 1
    assert env.supervisor.stats()["queued"] == 0
    child = await env.get(Run, started.child_run_id)
    assert child.status == "queued" and child.parent_run_id == run.id
    assert child.trigger == "task" and child.agent_version_id == kid.draft_version_id
    # 뿌리 한도 5에서 이 자식을 넣고 남은 수가 자식의 상한이다(3.9).
    assert child.limits["max_child_calls"] == 4
    correlation = child.input["correlation"]
    assert correlation["parent_run_id"] == str(run.id) and correlation["depth"] == 1
    assert correlation["ancestors"] == [str(boss.id)]
    session = await env.get(ChatSession, started.child_session_id)
    assert session.source == "task" and session.approval_override == "off"
    assert session.title == "boss 이름 → 일을 한다 {{ 이건 템플릿이 아니다 }}"
    assert session.meta["parent_agent_name"] == "boss 이름"
    async with env.db.read() as db_session:
        first = (
            await db_session.execute(select(Message).where(Message.run_id == child.id))
        ).scalar_one()
    # 모델이 쓴 task는 템플릿으로 채우지 않는다.
    assert first.text.startswith("일을 한다 {{ 이건 템플릿이 아니다 }}")


async def test_create_child_checks(env: Env) -> None:
    boss = await env.agent("boss")
    kid = await env.agent("kid", published=False)
    chat = await env.session(boss)
    run = await env.run(chat)
    base = env.parent(boss, chat, run)
    message_id = run.user_message_id

    async def code(parent: ParentRun, call: ChildCall) -> str:
        result = await env.service.create_child(parent, call)
        assert isinstance(result, ChildStartError)
        return result.code

    assert await code(replace(base, draft=False), call_for(kid, message_id)) == (
        "agent_not_published"
    )
    assert await code(replace(base, depth=2), call_for(kid, message_id)) == "depth_exceeded"
    assert await code(replace(base, ancestors=(kid.id, boss.id)), call_for(kid, message_id)) == (
        "agent_ref_cycle"
    )
    fixed = choice(kid, result=ResultSpec.model_validate({"format": "json", "schema": SCHEMA}))
    free = choice(kid, result=ResultSpec(format="json"))
    assert await code(base, call_for(kid, message_id, agent=fixed, output_schema=SCHEMA)) == (
        "output_schema_not_allowed"
    )
    assert await code(base, call_for(kid, message_id, agent=free)) == "output_schema_required"
    assert await code(
        base, call_for(kid, message_id, agent=free, output_schema={"type": "object"})
    ) == ("output_schema_invalid")
    assert await code(replace(base, max_child_calls=0), call_for(kid, message_id)) == "child_limit"
    assert await code(base, call_for(kid, message_id, files=["../out.txt"])) == "file_not_found"
    assert await code(base, call_for(kid, message_id, files=["missing.txt"])) == "file_not_found"
    async with env.db.write() as session:
        stored = await session.get(Agent, kid.id)
        assert stored is not None
        stored.deleted_at = utcnow()
    assert await code(base, call_for(kid, message_id)) == "agent_not_found"


async def test_tree_budget_counts_every_descendant(env: Env) -> None:
    boss = await env.agent("boss")
    kid = await env.agent("kid")
    chat = await env.session(boss)
    root = await env.run(chat, limits={"max_child_calls": 2})
    middle = await env.run(chat, status="waiting_input", parent=root.id)
    await env.run(chat, status="succeeded", parent=middle.id)
    # 중간 런(깊이 1)의 상한은 남아 있어도 뿌리 한도 2를 이미 다 썼다.
    parent = env.parent(
        kid, chat, middle, depth=1, root_run_id=root.id, ancestors=(boss.id, kid.id)
    )
    other = await env.agent("other")

    result = await env.service.create_child(parent, call_for(other, middle.user_message_id))

    assert isinstance(result, ChildStartError) and result.code == "child_limit"
    assert "이 대화" in result.message


async def test_stage_files_reuses_same_content_and_keeps_its_own_key(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    boss = await env.agent("boss")
    kid = await env.agent("kid")
    chat = await env.session(boss)
    run = await env.run(chat)
    parent = env.parent(boss, chat, run)
    (env.root / "outputs").mkdir()
    (env.root / "outputs" / "data.txt").write_text("원본", encoding="utf-8")

    first = await env.service.stage_files(
        parent, call_for(kid, run.user_message_id, files=["outputs/data.txt"])
    )
    again = await env.service.stage_files(
        parent,
        call_for(kid, run.user_message_id, tool_call_id="call/2", files=["outputs/data.txt"]),
    )
    (env.root / "outputs" / "data.txt").write_text("고쳤다", encoding="utf-8")
    changed = await env.service.stage_files(
        parent,
        call_for(kid, run.user_message_id, tool_call_id="call_3", files=["outputs/data.txt"]),
    )

    assert isinstance(first, list) and isinstance(again, list) and isinstance(changed, list)
    assert first == again and first != changed
    row = await env.get(SessionFile, first[0])
    assert row.kind == "output" and row.run_id == run.id
    assert row.message_id == run.user_message_id
    assert row.storage_key == f"session-files/{run.id}/.calls/call_1/outputs/data.txt"
    monkeypatch.setattr(child_runs, "MAX_CHILD_FILE_BYTES", 2)
    too_big = await env.service.stage_files(
        parent, call_for(kid, run.user_message_id, files=["outputs/data.txt"])
    )
    assert isinstance(too_big, ChildStartError) and too_big.code == "file_too_large"


async def test_claim_parent_moves_the_waiting_parent_only_once(env: Env) -> None:
    boss = await env.agent("boss")
    chat = await env.session(boss)
    child_chat = await env.session(boss)
    child = await env.run(child_chat, status="succeeded")
    parent = await env.run(chat, status="waiting_input", pending=waiting_on(child.id))
    async with env.db.write() as session:
        row = await session.get(Run, child.id)
        assert row is not None
        row.parent_run_id = parent.id
        row.output = {"status": "succeeded", "text": "결과", "files": [], "usage": {}}
    now = utcnow()

    async with env.db.write() as session:
        row = await session.get(Run, child.id)
        assert row is not None
        claimed = await claim_parent(session, env.workspace_id, row, now)
        again = await claim_parent(session, env.workspace_id, row, now)

    assert claimed is not None and again is None
    assert claimed.summary.text == "결과" and claimed.tool_call_id == "call_1"
    resumed = await env.get(Run, parent.id)
    assert resumed.status == "queued"
    assert resumed.resume_payload["kind"] == "agent_call"
    assert resumed.resume_payload["child"]["run_id"] == str(child.id)

    other_child = await env.run(child_chat, status="failed", parent=parent.id)
    waiting = await env.run(chat, status="waiting_input", pending=waiting_on(new_id()))
    async with env.db.write() as session:
        row = await session.get(Run, other_child.id)
        assert row is not None
        row.parent_run_id = waiting.id
        # 다른 자식을 기다리는 부모, 이미 끝난 부모는 건드리지 않는다.
        assert await claim_parent(session, env.workspace_id, row, now) is None
    assert (await env.get(Run, waiting.id)).status == "waiting_input"


async def test_auto_approve_goes_both_ways(env: Env) -> None:
    boss = await env.agent("boss")
    kid = await env.agent("kid")
    root_chat = await env.session(boss)
    child_chat = await env.session(kid)
    grand_chat = await env.session(kid)
    root = await env.run(root_chat, status="waiting_input", pending=waiting_on(new_id()))
    child = await env.run(child_chat, status="waiting_input", parent=root.id)
    grand = await env.run(grand_chat, status="waiting_input", parent=child.id)

    async with env.db.write() as session:
        changed = await propagate_auto_approve_down(session, env.workspace_id, root_chat.id, "off")
    assert changed == 2
    assert (await env.get(ChatSession, grand_chat.id)).approval_override == "off"

    async with env.db.write() as session:
        await propagate_auto_approve_down(session, env.workspace_id, root_chat.id, None)
        stored = await session.get(Run, grand.id)
        assert stored is not None
        assert await propagate_auto_approve_up(session, env.workspace_id, stored) == 2
    assert (await env.get(ChatSession, root_chat.id)).approval_override == "off"
    assert (await env.get(ChatSession, child_chat.id)).approval_override == "off"
