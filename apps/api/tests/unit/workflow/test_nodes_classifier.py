"""question_classifier 노드(2단계 설계 2.3, 11.2, M13). 모델은 가짜 어댑터 각본이다."""

from collections.abc import Callable
from typing import Any

from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.llm.types import TextBlock, Usage
from app.workflow.graph import validate

Node = Callable[..., dict[str, Any]]

CLASSES = [
    {"id": "weather", "name": "날씨", "description": "날씨·기온·비 소식을 묻는 글"},
    {"id": "order", "name": "주문", "description": ""},
]


def classifier_graph(kit: Any, make_node: Node, **config: Any) -> Any:
    body = {
        "query_var": "start.q",
        "classes": CLASSES,
        "instruction": "{{ start.hint }}",
        **config,
    }
    return kit.graph(
        [
            make_node(
                "start",
                "start",
                {"fields": [{"name": "q", "type": "string"}, {"name": "hint", "type": "string"}]},
            ),
            make_node("question_classifier_1", "question_classifier", body),
            make_node(
                "end_weather",
                "end",
                {
                    "outputs": [
                        {"name": "name", "var": "question_classifier_1.class_name"},
                        {"name": "label", "var": "question_classifier_1.class_label"},
                        {"name": "id", "var": "question_classifier_1.class_id"},
                    ]
                },
            ),
            make_node(
                "end_order",
                "end",
                {
                    "outputs": [
                        {"name": "order_name", "var": "question_classifier_1.class_name"},
                    ]
                },
            ),
        ],
        [
            "start>question_classifier_1",
            "question_classifier_1:class_weather>end_weather",
            "question_classifier_1:class_order>end_order",
        ],
    )


def reply(text: str, inp: int = 10, out: int = 2) -> list[Any]:
    return [*fake_text(text), fake_end(usage=Usage(input_tokens=inp, output_tokens=out))]


async def run_with(kit: Any, make_node: Node, turns: list[Any], **config: Any) -> Any:
    adapter = FakeAdapter(turns)
    run = await kit.run(
        classifier_graph(kit, make_node, **config),
        inputs={"q": "내일 비 와?", "hint": "애매하면 날씨"},
        ports=kit.ports(model=adapter),
    )
    return run, adapter


def completed(run: Any) -> dict[str, Any]:
    return next(d for d in run.of("node.completed") if d["node_id"] == "question_classifier_1")


async def test_json_answer_opens_that_class_handle(kit: Any, make_node: Node) -> None:
    run, adapter = await run_with(kit, make_node, [reply('{"class_id": "weather"}')])

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {
        "name": "날씨·기온·비 소식을 묻는 글",
        "label": "날씨",
        "id": "weather",
    }
    assert completed(run)["branch"] == "class_weather"
    assert run.statuses()["end_order"] == "skipped"
    assert run.of("node.text_delta") == []
    assert len(run.of("node.usage")) == 1
    request = adapter.requests[0]
    assert "id: weather / 이름: 날씨 / 설명: 날씨·기온·비 소식을 묻는 글" in request.system
    assert "id: order / 이름: 주문" in request.system
    assert "애매하면 날씨" in request.system
    [message] = request.messages
    assert [b.text for b in message.blocks if isinstance(b, TextBlock)] == ["내일 비 와?"]


async def test_class_name_falls_back_to_the_title(kit: Any, make_node: Node) -> None:
    run, _ = await run_with(kit, make_node, [reply("order")])

    assert run.outcome.outputs == {"order_name": "주문"}
    assert completed(run)["branch"] == "class_order"


async def test_unreadable_answer_is_asked_once_more(kit: Any, make_node: Node) -> None:
    run, adapter = await run_with(
        kit, make_node, [reply("음, 아마 날씨 같아요"), reply('{"class_id": "order"}', 20, 3)]
    )

    assert run.outcome.status == "succeeded"
    assert completed(run)["branch"] == "class_order"
    assert completed(run)["warnings"] == []
    assert run.outcome.usage.input_tokens == 30
    assert len(run.of("node.usage")) == 2
    retry = adapter.requests[1]
    assert [m.role for m in retry.messages] == ["user", "assistant", "user"]
    assert "weather, order" in str(retry.messages[-1].blocks[0])


async def test_parse_failure_falls_back_to_the_first_class_with_a_warning(
    kit: Any, make_node: Node
) -> None:
    run, _ = await run_with(kit, make_node, [reply("모르겠다"), reply('{"class_id": "refund"}')])

    assert run.outcome.status == "succeeded"
    done = completed(run)
    assert done["branch"] == "class_weather"
    [warning] = done["warnings"]
    assert warning.startswith("classifier_fallback:") and "refund" in warning
    record = next(
        e.node_run
        for e in run.events
        if e.type == "node.completed" and e.data["node_id"] == "question_classifier_1"
    )
    assert record is not None and record.process["fallback"] is True


async def test_parse_failure_can_fail_the_node_instead(kit: Any, make_node: Node) -> None:
    run, _ = await run_with(
        kit,
        make_node,
        [reply("모르겠다"), reply("여전히 모르겠다")],
        on_parse_failure="fail",
    )

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "classifier_parse_failed"
    assert run.outcome.usage.input_tokens == 20


def test_validation_rules(kit: Any, make_node: Node) -> None:
    def issues(**config: Any) -> list[tuple[str, str | None]]:
        graph = classifier_graph(kit, make_node, **config)
        return [
            (i.code, i.field)
            for i in validate(graph, "workflow")
            if i.node_id == "question_classifier_1"
        ]

    assert issues() == []
    duplicated = issues(classes=[CLASSES[0], {"id": "weather", "name": "또 날씨"}])
    assert ("duplicate_name", "classes[1].id") in duplicated
    # class_order 핸들이 없어져 그 엣지도 걸린다.
    assert ("invalid_handle", "source_handle") in duplicated
    assert issues(classes=[])[0] == ("invalid_config", "classes")
    assert ("unknown_variable", "query_var") in issues(query_var="start.nope")
