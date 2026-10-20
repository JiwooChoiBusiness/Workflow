"""answer 미리 흘리기(2단계 설계 3.8, A6).

llm → answer처럼 사이에 분기가 없으면 llm 델타가 answer.delta로 동시에 흐르고, answer 노드는 남은
뒷부분만 낸다. 여러 answer는 완료 순서로 이어 붙고, 흘린 글을 모두 이으면 런의 답 글과 같다.
재시도·json_mode llm은 미리 흘리지 않고, 입력 대기로 멈췄다가 재개해도 앞부분을 다시 흘리지 않는다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.schemas.run import PendingInteractionOut
from app.workflow.nodes.base import ALL_STRATEGIES, NodeContext, NodeResult, NodeRunner, OutputSpec

LONG = "미리 흘리는 답은 여러 조각으로 나뉘어 차례로 도착한다. 화면은 그대로 이어 그린다."


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None


class WaitRunner(NodeRunner[ProbeConfig]):
    """처음에는 승인을 기다리며 멈추고, 재개하면 저장한 state의 값으로 끝난다."""

    type = "test_wait"
    Config = ProbeConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        if state is None:
            pending = PendingInteractionOut(
                interaction_id="int_1",
                kind="approval",
                tool_call_id="call_1",
                tool_name="shell",
                input={"command": "echo hi"},
                created_at=datetime.now(UTC),
            )
            return NodeResult(
                status="waiting_input",
                pending=pending,
                state={"ticket": config.value},
                child_run_id=uuid.uuid4(),
            )
        return NodeResult(status="succeeded", outputs={"value": state["ticket"]})


def llm(text: str = "{{ sys.query }}", **config: Any) -> dict[str, Any]:
    return {"prompt": [{"role": "user", "text": text}], **config}


def turn(text: str, chunk: int = 7) -> list[Any]:
    return [*fake_text(text, chunk_size=chunk), fake_end()]


def interleaved(run: Any, source: str, answer: str) -> None:
    """source의 델타마다 바로 뒤에 같은 글의 answer.delta가 온다."""
    events = run.events
    deltas = [i for i, event in enumerate(events) if event.type == "node.text_delta"]
    assert deltas
    for position in deltas:
        assert events[position].data["node_id"] == source
        follow = events[position + 1]
        assert follow.type == "answer.delta"
        assert follow.data == {"text": events[position].data["text"], "node_id": answer}


async def test_llm_deltas_flow_into_the_answer_while_the_llm_runs(chat_kit: Any) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("answer_1", "answer", {"text": "답: {{ llm_1.text }}!\n"}),
        ],
        ["start>llm_1", "llm_1>answer_1"],
    )

    run = await chat_kit.run(
        graph, model=FakeAdapter([turn(LONG)]), system={"query": "질문", "turn": 1}
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.answer == f"답: {LONG}!"
    assert run.answer() == run.outcome.answer
    # 앞의 글 조각은 llm이 시작하자마자, 델타는 llm이 끝나기 전에 흐른다.
    first = run.index("answer.delta", "answer_1")
    assert run.events[first].data == {"text": "답: ", "node_id": "answer_1"}
    assert first < run.index("node.text_delta", "llm_1")
    assert run.index("answer.delta", "answer_1", last=True) < run.index("node.started", "answer_1")
    interleaved(run, "llm_1", "answer_1")
    completed = [data for data in run.of("node.completed") if data["node_id"] == "answer_1"]
    assert completed[0]["outputs_preview"] == {"text": f"답: {LONG}!"}
    # 노드는 이미 흘린 글을 다시 내지 않는다.
    assert all(data.get("node_run_id") is None for data in run.of("answer.delta"))


async def test_answers_join_in_completion_order_and_each_streams_its_llm(chat_kit: Any) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}\n"}),
            chat_kit.node("llm_2", "llm", llm("이어서 {{ llm_1.text }}")),
            chat_kit.node("answer_2", "answer", {"text": "\n추가: {{ llm_2.text }}"}),
        ],
        ["start>llm_1", "llm_1>answer_1", "answer_1>llm_2", "llm_2>answer_2"],
    )
    model = FakeAdapter([turn("첫 번째 답이다."), turn("두 번째 답은 조금 더 길다.")])

    run = await chat_kit.run(graph, model=model, system={"query": "질문"})

    assert run.outcome.answer == "첫 번째 답이다.\n추가: 두 번째 답은 조금 더 길다."
    assert run.answer() == run.outcome.answer
    assert run.index("answer.delta", "answer_2") < run.index("node.completed", "llm_2")
    assert run.index("answer.delta", "answer_1", last=True) < run.index("answer.delta", "answer_2")


async def test_json_mode_and_retrying_llms_answer_only_after_they_finish(chat_kit: Any) -> None:
    for name, config, extra in (
        ("json", llm(json_mode=True), {}),
        ("retry", llm(), {"retry": {"max_attempts": 1, "interval_ms": 0}}),
    ):
        graph = chat_kit.graph(
            [
                chat_kit.node("start", "start"),
                chat_kit.node("llm_1", "llm", config, **extra),
                chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}"}),
            ],
            ["start>llm_1", "llm_1>answer_1"],
        )
        reply = '{"ok": true, "note": "조각이 여럿이다"}' if name == "json" else LONG

        run = await chat_kit.run(graph, model=FakeAdapter([turn(reply)]), system={"query": "q"})

        assert run.outcome.status == "succeeded", name
        assert len(run.of("answer.delta")) == 1, name
        assert run.index("answer.delta", "answer_1") > run.index("node.completed", "llm_1")
        assert run.answer() == run.outcome.answer == reply


async def test_a_paused_answer_resumes_after_what_it_already_sent(chat_kit: Any) -> None:
    chat_kit.registry.register(WaitRunner())
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("wait_1", "test_wait", {"value": "승인됨"}),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }} / {{ wait_1.value }}"}),
        ],
        ["start>llm_1", "llm_1>wait_1", "llm_1>answer_1", "wait_1>answer_1"],
    )
    run_id = uuid.uuid4()

    first = await chat_kit.run(
        graph, model=FakeAdapter([turn(LONG)]), system={"query": "q"}, run_id=run_id
    )

    assert first.outcome.status == "waiting_input"
    # 멈춘 데까지 흘린 글이 답 글이고, 체크포인트에 조각 위치가 남는다.
    assert first.answer() == first.outcome.answer == f"{LONG} / "
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None and checkpoint.answer_cursors == {"answer_1": 2}

    second = await chat_kit.run(
        graph, model=FakeAdapter([]), system={"query": "q"}, resume=checkpoint, run_id=run_id
    )

    assert second.outcome.status == "succeeded"
    assert [data["text"] for data in second.of("answer.delta")] == ["승인됨"]
    assert second.outcome.answer == f"{LONG} / 승인됨"


async def test_an_answer_finished_before_the_pause_is_not_sent_again(chat_kit: Any) -> None:
    chat_kit.registry.register(WaitRunner())
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("answer_1", "answer", {"text": "첫째: {{ llm_1.text }}"}),
            chat_kit.node("wait_1", "test_wait", {"value": "승인됨"}),
            chat_kit.node("answer_2", "answer", {"text": " 둘째: {{ wait_1.value }}"}),
        ],
        ["start>llm_1", "llm_1>answer_1", "answer_1>wait_1", "wait_1>answer_2"],
    )
    run_id = uuid.uuid4()

    first = await chat_kit.run(
        graph, model=FakeAdapter([turn(LONG)]), system={"query": "q"}, run_id=run_id
    )

    assert first.outcome.status == "waiting_input"
    assert first.answer() == first.outcome.answer == f"첫째: {LONG}"
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None and checkpoint.answer_cursors == {}

    second = await chat_kit.run(
        graph, model=FakeAdapter([]), system={"query": "q"}, resume=checkpoint, run_id=run_id
    )

    assert second.outcome.status == "succeeded"
    # 재개 스트림에는 answer_2의 글만 오고, 답 글은 앞부분과 이어진 한 벌이다.
    assert [data["node_id"] for data in second.of("answer.delta")] == ["answer_2"]
    assert second.outcome.answer == f"첫째: {LONG} 둘째: 승인됨"
