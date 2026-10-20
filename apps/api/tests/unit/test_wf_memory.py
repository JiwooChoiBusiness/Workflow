"""챗플로우 메모리(2단계 설계 2.3의 llm, 11.2, A6).

llm 노드의 memory를 켜면 대화의 앞 턴 중 window_turns개가 시스템 글 다음, 프롬프트 블록 앞에
user·assistant 메시지로 들어간다. 요약 압축은 없고, 컨텍스트를 넘으면 창을 반으로 줄여 한 번
더 본다.
워크플로우에서는 켜 두어도 쓰지 않는다.
"""

from typing import Any

from app.llm.catalog import ModelCapabilities
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.llm.types import ChatRequest, TextBlock
from app.schemas.workflow import Graph
from app.workflow.memory import MemoryConfig, Turn, memory_window, recent, turn_messages
from app.workflow.nodes import default_registry

HISTORY = [Turn(f"질문 {index}", f"답 {index}") for index in range(1, 5)]


def llm(memory: dict[str, Any] | None = None, **config: Any) -> dict[str, Any]:
    return {
        "prompt": [
            {"role": "system", "text": "짧게 답한다"},
            {"role": "user", "text": "{{ sys.query }}"},
        ],
        "memory": memory or {"enabled": True, "window_turns": 2},
        **config,
    }


def flow(chat_kit: Any, config: dict[str, Any]) -> Graph:
    return chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", config),
            chat_kit.node("answer_1", "answer", {"text": "{{ llm_1.text }}"}),
        ],
        ["start>llm_1", "llm_1>answer_1"],
    )


def conversation(request: ChatRequest) -> list[tuple[str, str]]:
    return [
        (message.role, "".join(b.text for b in message.blocks if isinstance(b, TextBlock)))
        for message in request.messages
    ]


def test_recent_turns_become_alternating_messages() -> None:
    assert recent(HISTORY, 2) == HISTORY[2:]
    assert recent(HISTORY, 10) == HISTORY
    assert recent(HISTORY, 0) == []
    messages = turn_messages([Turn("물음", ""), Turn("다음", "응")])
    assert [(m.role, m.blocks[0]) for m in messages] == [
        ("user", TextBlock(text="물음")),
        ("assistant", TextBlock(text="(빈 응답)")),
        ("user", TextBlock(text="다음")),
        ("assistant", TextBlock(text="응")),
    ]


def test_the_executor_reads_the_widest_window_in_the_graph(chat_kit: Any) -> None:
    registry = default_registry()
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start"),
            chat_kit.node("llm_1", "llm", llm({"enabled": True, "window_turns": 3})),
            chat_kit.node("llm_2", "llm", llm({"enabled": True, "window_turns": 7})),
            chat_kit.node("llm_3", "llm", llm({"enabled": False, "window_turns": 50})),
            chat_kit.node("answer_1", "answer", {"text": "x"}),
        ],
        ["start>llm_1", "llm_1>llm_2", "llm_2>llm_3", "llm_3>answer_1"],
    )

    assert memory_window(graph, registry) == 7
    assert memory_window(flow(chat_kit, llm({"enabled": False})), registry) == 0
    assert MemoryConfig().window_turns == 10


async def test_memory_puts_the_last_turns_between_system_and_prompt(chat_kit: Any) -> None:
    model = FakeAdapter([[*fake_text("세 번째 답"), fake_end()]])

    run = await chat_kit.run(
        flow(chat_kit, llm()), model=model, system={"query": "지금 질문"}, history=HISTORY
    )

    assert run.outcome.status == "succeeded"
    [request] = model.requests
    assert request.system == "짧게 답한다"
    assert conversation(request) == [
        ("user", "질문 3"),
        ("assistant", "답 3"),
        ("user", "질문 4"),
        ("assistant", "답 4"),
        ("user", "지금 질문"),
    ]
    llm_record = [
        e.node_run for e in run.events if e.node_run is not None and e.node_run.node_id == "llm_1"
    ][-1]
    assert llm_record is not None
    assert llm_record.inputs["memory"] == [
        {"role": "user", "text": "질문 3"},
        {"role": "assistant", "text": "답 3"},
        {"role": "user", "text": "질문 4"},
        {"role": "assistant", "text": "답 4"},
    ]


async def test_memory_off_or_in_a_workflow_sends_only_the_prompt(chat_kit: Any) -> None:
    off = FakeAdapter([[*fake_text("a"), fake_end()]])
    await chat_kit.run(
        flow(chat_kit, llm({"enabled": False, "window_turns": 5})),
        model=off,
        system={"query": "q"},
        history=HISTORY,
    )
    workflow_model = FakeAdapter([[*fake_text("b"), fake_end()]])
    graph = chat_kit.graph(
        [
            chat_kit.node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            chat_kit.node(
                "llm_1",
                "llm",
                {
                    "prompt": [{"role": "user", "text": "{{ start.q }}"}],
                    "memory": {"enabled": True},
                },
            ),
            chat_kit.node("end_1", "end", {"outputs": [{"name": "t", "var": "llm_1.text"}]}),
        ],
        ["start>llm_1", "llm_1>end_1"],
    )

    run = await chat_kit.run(
        graph, model=workflow_model, kind="workflow", inputs={"q": "질문"}, history=HISTORY
    )

    assert conversation(off.requests[0]) == [("user", "q")]
    assert run.outcome.status == "succeeded"
    assert conversation(workflow_model.requests[0]) == [("user", "질문")]


class SmallWindow(FakeAdapter):
    """메시지 하나를 100토큰으로 세는 작은 컨텍스트의 모델."""

    def __init__(self, turns: list[Any], *, window: int) -> None:
        super().__init__(turns)
        self.window = window

    def capabilities(self, model_id: str) -> ModelCapabilities:
        return super().capabilities(model_id).model_copy(update={"context_window": self.window})

    def estimate_tokens(self, req: ChatRequest) -> int:
        return 100 * len(req.messages)


async def test_too_long_context_halves_the_memory_window_once(chat_kit: Any) -> None:
    model = SmallWindow([[*fake_text("줄인 답"), fake_end()]], window=700)

    run = await chat_kit.run(
        flow(chat_kit, llm({"enabled": True, "window_turns": 4})),
        model=model,
        system={"query": "지금"},
        history=HISTORY,
    )

    assert run.outcome.status == "succeeded"
    assert conversation(model.requests[0]) == [
        ("user", "질문 3"),
        ("assistant", "답 3"),
        ("user", "질문 4"),
        ("assistant", "답 4"),
        ("user", "지금"),
    ]
    completed = next(d for d in run.of("node.completed") if d["node_id"] == "llm_1")
    assert any(w.startswith("memory_trimmed") for w in completed["warnings"])

    still_long = SmallWindow([], window=300)
    failed = await chat_kit.run(
        flow(chat_kit, llm({"enabled": True, "window_turns": 4})),
        model=still_long,
        system={"query": "지금"},
        history=HISTORY,
    )
    # 한 번 줄여도 넘치면 context_too_long으로 끝나고 모델은 부르지 않는다.
    assert failed.outcome.status == "failed"
    assert failed.outcome.error is not None and failed.outcome.error.code == "context_too_long"
    assert still_long.requests == []
