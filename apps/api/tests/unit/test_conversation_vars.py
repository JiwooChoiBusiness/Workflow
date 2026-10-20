"""대화 변수(2단계 설계 2.3의 variable_assigner, 3.2의 conv, A6).

variable_assigner가 바꾼 값은 같은 런의 뒤 노드가 바로 보고, 런이 끝나면 실행기가 성공한 런에서만
wf_conversation.conversation_vars에 쓴다. 다음 턴은 저장된 값(없으면 선언한 기본값)으로 시작한다.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import WfConversation, WfMessage, WfRun
from app.db.repositories.workspaces import WorkspaceRepository
from app.runner import recovery
from app.runner.supervisor import CancelSignal, Supervisor
from app.schemas.wf_conversation import WfConversationCreate, WfMessageCreate
from app.schemas.workflow import (
    ConversationVar,
    Graph,
    WorkflowCreate,
    WorkflowDraftUpdate,
    WorkflowSettings,
)
from app.services.wf_conversations import WfConversationService
from app.services.workflow_executor import WorkflowRunExecutor
from app.services.workflows import WorkflowService
from app.storage import make_storage
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores

TYPES = {"count": "number", "items": "array[string]", "topic": "string"}


def assigner(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items)}


async def test_assigned_values_are_seen_downstream_and_returned(chat_kit: Any) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node(
                "assign",
                "variable_assigner",
                assigner(
                    {"target": "conv.count", "op": "+=", "value": 2},
                    {"target": "conv.count", "op": "-=", "value": 1},
                    {"target": "conv.items", "op": "append", "var": "sys.query"},
                    {"target": "conv.topic", "op": "clear"},
                ),
            ),
            chat_kit.node(
                "answer_1",
                "answer",
                {"text": "{{ conv.count }} {{ conv.items }} [{{ conv.topic }}]"},
            ),
        ],
        ["start>assign", "assign>answer_1"],
    )

    run = await chat_kit.run(
        graph,
        system={"query": "둘째"},
        conversation={"count": 4, "items": ["첫째"], "topic": "날씨"},
        conversation_types=TYPES,
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.conversation == {"count": 5, "items": ["첫째", "둘째"], "topic": ""}
    assert run.outcome.answer == '5 ["첫째","둘째"] []'


async def test_a_failing_run_still_reports_its_changes_for_the_service_to_drop(
    chat_kit: Any,
) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node(
                "assign",
                "variable_assigner",
                assigner({"target": "conv.count", "op": "+=", "value": 1}),
            ),
            chat_kit.node("boom", "template", {"template": "{{ '{' | fromjson }}"}),
            chat_kit.node("answer_1", "answer", {"text": "{{ boom.output }}"}),
        ],
        ["start>assign", "assign>boom", "boom>answer_1"],
    )

    run = await chat_kit.run(
        graph, system={"query": "q"}, conversation={"count": 1}, conversation_types=TYPES
    )

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.node_id == "boom"
    # 엔진은 바뀐 값을 그대로 돌려주고, 저장할지는 실행기가 런 상태로 정한다.
    assert run.outcome.conversation["count"] == 2


class QueueStub:
    """워크플로우 레인 대신 넣은 런 키만 모은다. 런은 시험이 실행기로 직접 돌린다."""

    shutting_down = False

    def __init__(self) -> None:
        self.keys: list[str] = []

    def stats(self) -> dict[str, int]:
        return {"active": 0, "queued": len(self.keys)}

    def enqueue(self, key: str) -> None:
        self.keys.append(key)


class Chat:
    def __init__(self, db: Database, bus: EventBus, settings: Settings) -> None:
        self.db = db
        self.bus = bus
        self.settings = settings
        self.queue = QueueStub()
        self.service = WfConversationService(
            db,
            settings,
            bus=bus,
            supervisor=cast(Supervisor, self.queue),
            storage=make_storage(settings),
        )
        self.workspace_id = uuid.UUID(int=0)

    async def setup(self, graph: Graph) -> uuid.UUID:
        async with self.db.write() as session:
            workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
            await session.flush()
            self.workspace_id = workspace.id
        workflows = WorkflowService(self.db, self.settings)
        created = await workflows.create_workflow(
            self.workspace_id, WorkflowCreate(name="상담", kind="chatflow")
        )
        draft = await workflows.get_draft(self.workspace_id, created.id)
        saved = await workflows.save_draft(
            self.workspace_id,
            created.id,
            WorkflowDraftUpdate(
                graph=graph,
                conversation_vars=[
                    ConversationVar(name="count", type="number", default=0),
                    ConversationVar(name="items", type="array[string]", default=[]),
                ],
                settings=WorkflowSettings(),
                updated_at=draft.updated_at,
            ),
        )
        assert [issue for issue in saved.issues if issue.level == "error"] == []
        conversation = await self.service.create_preview(
            self.workspace_id, created.id, WfConversationCreate()
        )
        return conversation.id

    async def turn(self, conversation_id: uuid.UUID, text: str) -> WfMessage:
        started = await self.service.send_message(
            self.workspace_id, conversation_id, WfMessageCreate(text=text), subscribe=False
        )
        executor = WorkflowRunExecutor(db=self.db, settings=self.settings, bus=self.bus)
        await executor(self.queue.keys[-1], CancelSignal())
        async with self.db.read() as session:
            message = await session.get(WfMessage, started.message_id)
            assert message is not None
            return message

    async def stored(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        async with self.db.read() as session:
            row = await session.get(WfConversation, conversation_id)
            assert row is not None
            return dict(row.conversation_vars)


@pytest.fixture
async def chat(tmp_path: Path) -> AsyncIterator[Chat]:
    db = Database(tmp_path / "wf.db")
    await upgrade_head(db)
    bus = EventBus(sqlite_stores(db), batch_window_sec=0.01, drop_delay_sec=0)
    bus.start()
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    try:
        yield Chat(db, bus, settings)
    finally:
        await bus.close()
        await db.close()


async def test_only_succeeded_turns_store_conversation_variables(chat: Chat, chat_kit: Any) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node(
                "assign",
                "variable_assigner",
                assigner(
                    {"target": "conv.count", "op": "+=", "value": 1},
                    {"target": "conv.items", "op": "append", "var": "sys.query"},
                ),
            ),
            chat_kit.node(
                "check",
                "if_else",
                {
                    "cases": [
                        {
                            "id": "boom",
                            "conditions": [{"var": "sys.query", "op": "=", "value": "실패"}],
                        }
                    ]
                },
            ),
            chat_kit.node("boom", "template", {"template": "{{ '{' | fromjson }}"}),
            chat_kit.node("answer_1", "answer", {"text": "{{ conv.count }}번째 {{ sys.turn }}턴"}),
            chat_kit.node("answer_2", "answer", {"text": "{{ boom.output }}"}),
        ],
        [
            "start>assign",
            "assign>check",
            "check:else>answer_1",
            "check:case_boom>boom",
            "boom>answer_2",
        ],
    )
    conversation_id = await chat.setup(graph)

    first = await chat.turn(conversation_id, "첫째")
    assert (first.status, first.text) == ("succeeded", "1번째 1턴")
    assert await chat.stored(conversation_id) == {"count": 1, "items": ["첫째"]}

    failed = await chat.turn(conversation_id, "실패")
    assert (failed.status, failed.text) == ("failed", "")
    assert await chat.stored(conversation_id) == {"count": 1, "items": ["첫째"]}

    second = await chat.turn(conversation_id, "둘째")
    assert (second.status, second.text) == ("succeeded", "2번째 3턴")
    assert await chat.stored(conversation_id) == {"count": 2, "items": ["첫째", "둘째"]}
    variables = await chat.service.variables(chat.workspace_id, conversation_id)
    assert {item.name: item.value for item in variables.items} == {
        "count": 2,
        "items": ["첫째", "둘째"],
    }


async def test_a_turn_cut_by_a_restart_leaves_a_failed_answer(chat: Chat, chat_kit: Any) -> None:
    graph = chat_kit.graph(
        [chat_kit.node("start", "start"), chat_kit.node("answer_1", "answer", {"text": "답"})],
        ["start>answer_1"],
    )
    conversation_id = await chat.setup(graph)
    started = await chat.service.send_message(
        chat.workspace_id, conversation_id, WfMessageCreate(text="질문"), subscribe=False
    )
    async with chat.db.write() as session:
        run = await session.get(WfRun, started.run_id)
        assert run is not None
        # 실행 중에 프로세스가 죽은 상태.
        run.status = "running"

    await recovery.recover_workflows(chat.db, cast(Supervisor, chat.queue), chat.bus)

    async with chat.db.read() as session:
        run = await session.get(WfRun, started.run_id)
        message = await session.get(WfMessage, started.message_id)
    assert run is not None and message is not None
    assert (run.status, (run.error or {}).get("code")) == ("failed", "shutdown")
    assert (message.status, message.text) == ("failed", "")
    assert await chat.stored(conversation_id) == {}
