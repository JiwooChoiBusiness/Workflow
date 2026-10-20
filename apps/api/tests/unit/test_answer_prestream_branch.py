"""분기가 있는 answer 미리 흘리기(2단계 설계 3.8, A6).

llm → if_else → answer처럼 경로에 분기가 있으면 분기가 정해진 뒤 answer가 한 번에 나온다.
분류기로 갈린 가지 안의 llm → answer는 고른 가지에서만 미리 흐르고, answer의 다른 들어오는 엣지가
아직 해결되지 않았으면(병렬 합류) 기다린다. 판정 (1)은 그래프만 보는 prestream_sources로 정한다.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.llm.types import ChatRequest, ModelEvent, TextDelta
from app.workflow.answer_stream import prestream_sources
from app.workflow.graph import analyze
from app.workflow.nodes.base import ALL_STRATEGIES, NodeContext, NodeResult, NodeRunner, OutputSpec

LONG = "분기 뒤에 나오는 답은 조각으로 나뉘어 있어도 한 번에 보인다."


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None


class GateRunner(NodeRunner[GateConfig]):
    """open()할 때까지 멈춰 있다가 value를 내는 노드."""

    type = "test_gate"
    Config = GateConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def run(
        self, ctx: NodeContext, config: GateConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        await self.gate.wait()
        return NodeResult(status="succeeded", outputs={"value": config.value})


class PausingAdapter(FakeAdapter):
    """pause_after번째 델타 뒤에서 resume이 켜질 때까지 멈추는 모델."""

    def __init__(self, text: str, *, pause_after: int) -> None:
        super().__init__([[*fake_text(text, chunk_size=3), fake_end()]])
        self.pause_after = pause_after
        self.resume = asyncio.Event()

    async def stream(self, req: ChatRequest) -> AsyncIterator[ModelEvent]:
        count = 0
        async for event in super().stream(req):
            yield event
            if isinstance(event, TextDelta):
                count += 1
                if count == self.pause_after:
                    await self.resume.wait()


def llm(text: str = "{{ sys.query }}") -> dict[str, Any]:
    return {"prompt": [{"role": "user", "text": text}]}


def turn(text: str) -> list[Any]:
    return [*fake_text(text, chunk_size=6), fake_end()]


def classified(class_id: str) -> list[Any]:
    return [*fake_text(json.dumps({"class_id": class_id})), fake_end()]


def classifier() -> dict[str, Any]:
    return {
        "query_var": "sys.query",
        "classes": [
            {"id": "a", "name": "가", "description": "가 문의"},
            {"id": "b", "name": "나", "description": "나 문의"},
        ],
    }


async def test_a_branch_between_llm_and_answer_holds_the_answer_until_it_is_decided(
    chat_kit: Any,
) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node(
                "if_1",
                "if_else",
                {
                    "cases": [
                        {"id": "yes", "conditions": [{"var": "llm_1.text", "op": "is_not_empty"}]}
                    ]
                },
            ),
            chat_kit.node("answer_1", "answer", {"text": "답: {{ llm_1.text }}"}),
            chat_kit.node("answer_2", "answer", {"text": "빈 답"}),
        ],
        ["start>llm_1", "llm_1>if_1", "if_1:case_yes>answer_1", "if_1:else>answer_2"],
    )
    assert prestream_sources(analyze(graph, "chatflow")) == {"answer_1": [], "answer_2": []}

    run = await chat_kit.run(graph, model=FakeAdapter([turn(LONG)]), system={"query": "q"})

    assert run.outcome.status == "succeeded"
    assert len(run.of("node.text_delta")) > 1
    # 분기가 정해지기 전에는 answer.delta가 없고, 정해진 뒤 한 번에 나온다.
    assert [data["text"] for data in run.of("answer.delta")] == [f"답: {LONG}"]
    assert run.index("answer.delta", "answer_1") > run.index("node.completed", "if_1")
    assert run.outcome.answer == f"답: {LONG}"
    statuses = {data["node_id"]: data["status"] for data in run.of("node.completed")}
    assert statuses["answer_2"] == "skipped"


async def test_llms_inside_classifier_branches_stream_only_on_the_chosen_branch(
    chat_kit: Any,
) -> None:
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("classify", "question_classifier", classifier()),
            chat_kit.node("llm_a", "llm", llm()),
            chat_kit.node("llm_b", "llm", llm()),
            chat_kit.node("answer_a", "answer", {"text": "가: {{ llm_a.text }}"}),
            chat_kit.node("answer_b", "answer", {"text": "나: {{ llm_b.text }}"}),
            chat_kit.node("joined", "answer", {"text": "|{{ llm_a.text }}{{ llm_b.text }}"}),
        ],
        [
            "start>classify",
            "classify:class_a>llm_a",
            "classify:class_b>llm_b",
            "llm_a>answer_a",
            "llm_b>answer_b",
            "answer_a>joined",
            "answer_b>joined",
        ],
    )
    sources = prestream_sources(analyze(graph, "chatflow"))
    assert sources == {"answer_a": ["llm_a"], "answer_b": ["llm_b"], "joined": ["llm_a", "llm_b"]}

    run = await chat_kit.run(
        graph, model=FakeAdapter([classified("a"), turn(LONG)]), system={"query": "q"}
    )

    assert run.outcome.status == "succeeded"
    assert run.index("answer.delta", "answer_a") < run.index("node.completed", "llm_a")
    assert {data["node_id"] for data in run.of("answer.delta")} == {"answer_a", "joined"}
    assert run.outcome.answer == f"가: {LONG}|{LONG}"
    assert run.answer() == run.outcome.answer


async def test_an_unresolved_join_keeps_the_answer_back(chat_kit: Any) -> None:
    gate = GateRunner()
    chat_kit.registry.register(gate)
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("gate_1", "test_gate", {"value": "뒤"}),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}{{ gate_1.value }}"}),
        ],
        ["start>llm_1", "start>gate_1", "llm_1>answer_1", "gate_1>answer_1"],
    )
    # 경로 조건은 맞지만 gate_1에서 오는 엣지가 해결되기 전에는 열지 않는다.
    assert prestream_sources(analyze(graph, "chatflow")) == {"answer_1": ["llm_1"]}

    def release(event: Any) -> None:
        if event.type == "node.completed" and event.data["node_id"] == "llm_1":
            gate.gate.set()

    run = await chat_kit.run(
        graph, model=FakeAdapter([turn(LONG)]), system={"query": "q"}, on_event=release
    )

    assert run.outcome.status == "succeeded"
    assert run.answer() == run.outcome.answer == f"{LONG}뒤"
    assert run.index("answer.delta", "answer_1") > run.index("node.completed", "gate_1")


async def test_another_unfinished_answer_keeps_the_stream_closed(chat_kit: Any) -> None:
    gate = GateRunner()
    chat_kit.registry.register(gate)
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}"}),
            chat_kit.node("gate_1", "test_gate", {"value": "x"}),
            chat_kit.node("answer_2", "answer", {"text": "[다른 가지]"}),
        ],
        ["start>llm_1", "llm_1>answer_1", "start>gate_1", "gate_1>answer_2"],
    )

    def release(event: Any) -> None:
        if event.type == "node.completed" and event.data["node_id"] == "answer_1":
            gate.gate.set()

    run = await chat_kit.run(
        graph, model=FakeAdapter([turn(LONG)]), system={"query": "q"}, on_event=release
    )

    # 병렬 가지의 answer_2가 언제 끝날지 몰라 answer_1은 llm이 끝난 뒤 한 번에 나온다.
    assert run.index("answer.delta", "answer_1") > run.index("node.completed", "llm_1")
    assert [data["text"] for data in run.of("answer.delta")] == [LONG, "[다른 가지]"]
    assert run.outcome.answer == f"{LONG}[다른 가지]"


def test_error_strategies_and_templates_decide_the_sources(chat_kit: Any) -> None:
    def sources(llm_extra: dict[str, Any], text: str) -> list[str]:
        graph = chat_kit.graph(
            [
                chat_kit.node("start", "start"),
                chat_kit.node("llm_1", "llm", llm(), **llm_extra),
                chat_kit.node("tpl", "template", {"template": "x"}),
                chat_kit.node("answer_1", "answer", {"text": text}),
            ],
            ["start>llm_1", "llm_1>tpl", "tpl>answer_1"],
        )
        return prestream_sources(analyze(graph, "chatflow"))["answer_1"]

    assert sources({}, "{{ llm_1.text }}") == ["llm_1"]
    assert sources({"error": {"strategy": "branch"}}, "{{ llm_1.text }}") == []
    assert sources({"error": {"strategy": "default"}}, "{{ llm_1.text }}") == []
    assert sources({}, "{{ llm_1.text | upper }}") == []
    assert sources({}, "{% if true %}{{ llm_1.text }}{% endif %}") == []
    assert sources({}, "{{ llm_1.usage }}") == []


async def test_a_join_resolved_mid_stream_catches_up_then_streams_live(chat_kit: Any) -> None:
    gate = GateRunner()
    chat_kit.registry.register(gate)
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm()),
            chat_kit.node("gate_1", "test_gate", {"value": "뒤"}),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}{{ gate_1.value }}"}),
        ],
        ["start>llm_1", "start>gate_1", "llm_1>answer_1", "gate_1>answer_1"],
    )
    model = PausingAdapter(LONG, pause_after=2)
    seen: list[str] = []

    def drive(event: Any) -> None:
        if event.type == "node.text_delta":
            seen.append(event.data["text"])
            if len(seen) == 2:
                gate.gate.set()
        elif event.type == "node.completed" and event.data["node_id"] == "gate_1":
            model.resume.set()

    run = await chat_kit.run(graph, model=model, system={"query": "q"}, on_event=drive)

    assert run.outcome.status == "succeeded"
    # gate_1이 끝나 합류가 풀리자 밀린 델타부터 차례로 흘리고, 나머지는 llm과 함께 흐른다.
    assert run.answer() == run.outcome.answer == f"{LONG}뒤"
    assert run.index("answer.delta", "answer_1") > run.index("node.completed", "gate_1")
    assert run.index("answer.delta", "answer_1") < run.index("node.completed", "llm_1")
    deltas = [data["text"] for data in run.of("answer.delta")]
    assert deltas[:2] == seen[:2]
