"""대기 기록(wf_run.pending_interaction)과 workflow.waiting_input의 비밀값 가림(2단계 설계 9.4).

agent 노드의 승인·질문 카드는 자식 런이 자기 버전의 비밀값으로만 가려 주므로 엔진이 워크플로우
비밀 env로 다시 가린다. 카드를 잇는 키와 enum·시각은 두고 값만 가리는 규칙(mask_card)은 에이전트
런의 저장 카드도 같이 쓴다. 폼은 안내문만 가리고 fields는 원문으로 둔다. SQL 승인 폼의 기본 SQL은
고치지 않고 승인하면 서비스가 저장된 fields의 default로 채워 그대로 실행하기 때문이다.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent.child_runs import ChildStarted
from app.agent.interactions import PendingInteraction
from app.schemas.events import ChildRunStartedData, RunWaitingInputData
from app.schemas.run import PendingInteractionOut
from app.schemas.wf_run import WfPendingOut
from app.services.run_executor import RunMasker
from app.streaming.event_bus import jsonable
from app.workflow.nodes.start import StartField, coerce_inputs

Node = Any

AGENT_ID = "0190c1b2-0000-7000-8000-0000000000aa"
NOW = datetime(2026, 11, 5, tzinfo=UTC)
# 비밀 탐지 도구에 걸리지 않게 실행 시 조립한다.
TOKEN = "tok-" + "7d2e" * 5
QUESTION = PendingInteractionOut(
    interaction_id="int_q",
    kind="question",
    tool_call_id="call_1",
    tool_name="ask_user",
    question=f"{TOKEN}로 배포할까?",
    options=[TOKEN, "아니오"],
    allow_free_text=False,
    created_at=NOW,
)
APPROVAL = PendingInteractionOut(
    interaction_id="int_a",
    kind="approval",
    tool_call_id="call_2",
    tool_name="shell",
    input={"command": f"echo {TOKEN}", "env": {"DEPLOY": TOKEN}},
    cwd="C:/work",
    timeout_sec=60,
    created_at=NOW,
)
# 가림 최소 길이(8자)라 실제로 가려지는, 카드의 enum·id와 겹치는 비밀값.
KEY_LIKE = {"TOKEN": "approval", "OTHER": "question", "THIRD": "published"}


def agent_graph(kit: Any, make_node: Node) -> Any:
    return kit.graph(
        [
            make_node("start", "start", {"fields": []}),
            make_node(
                "agent_1",
                "agent",
                {"agent_id": AGENT_ID, "instruction": "{{ env.TOKEN }}를 확인한다"},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "agent_1.text"}]}),
        ],
        ["start>agent_1", "agent_1>end_1"],
    )


async def wait_on(
    kit: Any,
    make_node: Node,
    card: PendingInteractionOut,
    *,
    mask_outputs: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[Any, Any]:
    secrets = env or {"TOKEN": TOKEN}
    tasks = kit.tasks()
    tasks.plan.append(
        lambda fake, child, spec: fake.result(child, "waiting_input", pending_interaction=card)
    )
    run = await kit.run(
        agent_graph(kit, make_node),
        env=secrets,
        secret_env=list(secrets),
        mask_outputs=mask_outputs,
        ports=kit.ports(tasks=tasks),
    )
    assert run.outcome.status == "waiting_input", run.outcome.error
    return run, tasks


@pytest.mark.parametrize("mask_outputs", [True, False])
async def test_the_agent_wait_card_is_masked_with_the_workflow_secrets(
    kit: Any, make_node: Node, mask_outputs: bool
) -> None:
    question, tasks = await wait_on(kit, make_node, QUESTION, mask_outputs=mask_outputs)
    approval, _ = await wait_on(kit, make_node, APPROVAL, mask_outputs=mask_outputs)

    # 자식에게는 원문 지시가 간다. 가리는 것은 워크플로우에 남기는 카드다.
    assert tasks.specs[0].instruction == f"{TOKEN}를 확인한다"
    asked = question.outcome.pending.interaction
    assert asked.options == ["***", "아니오"] and asked.question == "***로 배포할까?"
    assert (asked.interaction_id, asked.kind, asked.allow_free_text) == ("int_q", "question", False)
    assert asked.created_at == QUESTION.created_at
    command = approval.outcome.pending.interaction
    assert command.input == {"command": "echo ***", "env": {"DEPLOY": "***"}}
    assert (command.tool_name, command.cwd, command.timeout_sec) == ("shell", "C:/work", 60)
    for run in (question, approval):
        pending = run.outcome.pending
        [waiting] = run.of("workflow.waiting_input")
        # 이벤트는 대기 기록과 같은 가린 카드를 싣는다.
        assert waiting["interaction"] == pending.interaction.model_dump(
            mode="json", exclude={"created_at"}
        )
        assert TOKEN not in json.dumps(jsonable(waiting), ensure_ascii=False)
        stored = WfPendingOut.model_validate(pending.model_dump(mode="json"))
        assert TOKEN not in stored.model_dump_json()


async def test_a_secret_equal_to_a_card_key_masks_only_the_values(
    kit: Any, make_node: Node
) -> None:
    # kind를 가리면 카드 검증에서 런이 깨지고, id를 가리면 답할 때 자식 카드와 맞춰 보지 못한다.
    asked = QUESTION.model_copy(
        update={
            "interaction_id": "int_question",
            "question": "question 하나만 고른다",
            "options": ["question", "아니오"],
        }
    )
    command = APPROVAL.model_copy(
        update={
            "interaction_id": "int_approval",
            "tool_call_id": "call_approval",
            "input": {"command": "echo approval"},
        }
    )

    question, _ = await wait_on(kit, make_node, asked, env=KEY_LIKE)
    approval, _ = await wait_on(kit, make_node, command, env=KEY_LIKE)

    shown = question.outcome.pending.interaction
    assert (shown.kind, shown.interaction_id) == ("question", "int_question")
    assert shown.question == "*** 하나만 고른다" and shown.options == ["***", "아니오"]
    shown = approval.outcome.pending.interaction
    assert (shown.kind, shown.interaction_id, shown.tool_call_id) == (
        "approval",
        "int_approval",
        "call_approval",
    )
    assert shown.input == {"command": "echo ***"}
    [waiting] = approval.of("workflow.waiting_input")
    assert waiting["interaction"]["kind"] == "approval"


def test_the_stored_agent_card_keeps_the_keys_that_resuming_uses() -> None:
    # 에이전트 런의 저장 카드도 mask_card를 쓴다. 재개는 tool_call_id·message_id로 멈춘 호출을 찾고,
    # 하위 에이전트 카드의 version은 RunChildOut의 enum이다.
    masker = RunMasker(KEY_LIKE.values())
    shared: dict[str, Any] = {
        "tool_call_id": "call_approval",
        "message_id": uuid.uuid4(),
        "step": 2,
        "completed_tool_call_ids": ["call_question"],
        "changes_since": NOW,
        "created_at": NOW,
    }
    cards = [
        PendingInteraction(
            interaction_id="int_approval",
            kind="approval",
            tool_name="shell",
            input={"command": "echo approval"},
            cwd="C:/approval",
            **shared,
        ),
        PendingInteraction(
            interaction_id="int_question",
            kind="question",
            tool_name="ask_user",
            question="question 하나만 고른다",
            options=["question", "아니오"],
            **shared,
        ),
        PendingInteraction(
            interaction_id="int_published",
            kind="agent_call",
            tool_name="call_agent",
            child_run_id=uuid.uuid4(),
            child_session_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            agent_name="published 담당",
            version="published",
            task_preview="published 버전으로 정리",
            **shared,
        ),
    ]
    values = {"input", "cwd", "question", "options", "agent_name", "task_preview"}

    restored = [
        PendingInteraction.model_validate(masker.card(pending.model_dump(mode="json")))
        for pending in cards
    ]

    for before, after in zip(cards, restored, strict=True):
        assert after.model_dump(exclude=values) == before.model_dump(exclude=values)
        shown = after.model_dump(mode="json", include=values, exclude_none=True)
        masked = json.dumps(list(shown.values()), ensure_ascii=False)
        assert "***" in masked and not any(secret in masked for secret in KEY_LIKE.values())
    assert restored[0].input == {"command": "echo ***"} and restored[0].cwd == "C:/***"


def test_card_events_carry_the_same_card_as_the_stored_one() -> None:
    # 실시간 이벤트(버스가 같은 값을 저장한다)가 새로 고친 화면이 읽는 저장 카드와 달라지지 않는다.
    masker = RunMasker(KEY_LIKE.values())
    pending = PendingInteraction(
        interaction_id="int_approval",
        kind="approval",
        tool_call_id="call_approval",
        tool_name="shell",
        message_id=uuid.uuid4(),
        step=1,
        created_at=NOW,
        input={"command": "echo approval"},
        cwd="C:/approval",
    )
    started = ChildStarted(
        child_run_id=uuid.uuid4(),
        child_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        agent_slug="reviewer",
        agent_name="published 담당",
        version="published",
        depth=1,
        result_format="text",
        task_preview="published 버전으로 정리",
    )

    [(_, waiting)] = masker.event("run.waiting_input", pending.event_data())
    [(_, child)] = masker.event("child_run.started", started.event_data("call_published"))

    stored = masker.card(pending.model_dump(mode="json"))
    assert waiting == {key: stored[key] for key in waiting}
    card = RunWaitingInputData.model_validate(waiting)
    assert (card.kind, card.interaction_id, card.tool_call_id) == (
        "approval",
        "int_approval",
        "call_approval",
    )
    assert card.input == {"command": "echo ***"} and card.cwd == "C:/***"
    shown = ChildRunStartedData.model_validate(child)
    assert (shown.version, shown.tool_call_id, shown.child_run_id) == (
        "published",
        "call_published",
        started.child_run_id,
    )
    assert shown.agent_name == "*** 담당" and shown.task_preview == "*** 버전으로 정리"


async def test_form_wait_masks_only_the_description_and_keeps_field_defaults(
    kit: Any, make_node: Node, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    fields = [{"name": "ok", "type": "select", "options": ["예", "아니오"], "default": "예"}]
    form_graph = kit.graph(
        [
            make_node("start", "start", {"fields": []}),
            make_node(
                "human_input_1",
                "human_input",
                {"description": "배포 키 {{ env.TOKEN }}를 쓸까?", "fields": fields},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "ok", "var": "human_input_1.ok"}]}),
        ],
        ["start>human_input_1", "human_input_1>end_1"],
    )
    sql_graph = kit.graph(
        [
            make_node("start", "start", {"fields": []}),
            make_node(
                "run_sql",
                "sql_query",
                {
                    "source_id": str(sql_source_id),
                    "sql": "SELECT region FROM sales WHERE region = '{{ env.TOKEN }}'",
                    "approval": "always",
                },
            ),
            make_node("end_1", "end", {"outputs": [{"name": "rows", "var": "run_sql.rows"}]}),
        ],
        ["start>run_sql", "run_sql>end_1"],
    )
    secrets: dict[str, Any] = {"env": {"TOKEN": TOKEN}, "secret_env": ["TOKEN"]}

    human = await kit.run(form_graph, **secrets)
    sql = await kit.run(sql_graph, ports=kit.ports(sql=fake_sql()), **secrets)

    form = human.outcome.pending.form
    assert form.description == "배포 키 ***를 쓸까?"
    assert [field.model_dump(include={"name", "options", "default"}) for field in form.fields] == [
        {"name": "ok", "options": ["예", "아니오"], "default": "예"}
    ]
    [waiting] = human.of("workflow.waiting_input")
    assert waiting["form"]["description"] == "배포 키 ***를 쓸까?"
    approval = sql.outcome.pending.form
    raw_sql = f"SELECT region FROM sales WHERE region = '{TOKEN}' LIMIT 201"  # noqa: S608
    assert approval.fields[0].name == "sql" and approval.fields[0].default == raw_sql
    [shown] = sql.of("workflow.waiting_input")
    assert shown["form"]["fields"][0]["default"] == raw_sql
    # 서비스(provide_form)는 저장된 fields로 응답을 채운다. 고치지 않고 승인하면 원문 SQL이 간다.
    stored = WfPendingOut.model_validate(sql.outcome.pending.model_dump(mode="json")).form
    assert stored is not None
    stored_fields = [StartField.model_validate(field.model_dump()) for field in stored.fields]
    assert coerce_inputs(stored_fields, {"decision": "approve"})["sql"] == raw_sql
