"""llm 노드(2단계 설계 2.3, 3.8, 3.10, M13).

모델은 가짜 어댑터(각본)이고, 429 재시도는 실제 GeminiAdapter에 가짜 전송을 붙여 본다.
"""

from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any

from app.llm.catalog import ModelCapabilities
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.llm.gemini_adapter import GeminiAdapter, TransportError
from app.llm.types import ChatRequest, ErrorEvent, ImageBlock, TextBlock, Usage
from app.runner.supervisor import CancelSignal
from app.schemas.workflow import WorkflowKind
from app.workflow.graph import validate
from app.workflow.nodes.llm import extract_json_object
from app.workflow.ports import NodeSettings

Node = Callable[..., dict[str, Any]]


def usage(inp: int, out: int) -> Usage:
    return Usage(input_tokens=inp, output_tokens=out)


def llm_graph(kit: Any, make_node: Node, config: dict[str, Any], **extra: Any) -> Any:
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("llm_1", "llm", config, **extra),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "text", "var": "llm_1.text"},
                        {"name": "json", "var": "llm_1.json"},
                        {"name": "usage", "var": "llm_1.usage"},
                    ]
                },
            ),
        ],
        ["start>llm_1", "llm_1>end_1"],
    )


def prompt(*blocks: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "text": text} for role, text in blocks]


async def test_text_streams_as_node_deltas_and_usage_is_summed(kit: Any, make_node: Node) -> None:
    adapter = FakeAdapter(
        [
            [
                *fake_text("안녕하세요, 지우님. 오늘도 좋은 하루", chunk_size=5),
                fake_end(usage=usage(12, 7)),
            ]
        ]
    )
    graph = llm_graph(
        kit,
        make_node,
        {
            "model": {"model_id": "gemini-3.8-flash", "params": {"temperature": 0.2}},
            "prompt": prompt(("system", "짧게 답한다"), ("user", "{{ start.q }}에게 인사")),
        },
    )
    prices = {"fake-model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}

    run = await kit.run(
        graph,
        inputs={"q": "지우"},
        ports=kit.ports(model=adapter, node_settings=NodeSettings(model_prices=prices)),
    )

    assert run.outcome.status == "succeeded"
    deltas = run.of("node.text_delta")
    assert len(deltas) > 1 and all(d["node_id"] == "llm_1" for d in deltas)
    assert "".join(d["text"] for d in deltas) == "안녕하세요, 지우님. 오늘도 좋은 하루"
    types = run.types()
    done = next(
        index
        for index, event in enumerate(run.events)
        if event.type == "node.completed" and event.data["node_id"] == "llm_1"
    )
    assert types.index("node.text_delta") < types.index("node.usage") < done
    [used] = run.of("node.usage")
    assert (used["model"], used["input_tokens"], used["output_tokens"]) == ("fake-model", 12, 7)
    assert used["cost_usd"] == (12 * 1.0 + 7 * 2.0) / 1_000_000
    assert run.outcome.usage.input_tokens == 12 and run.outcome.usage.output_tokens == 7
    assert run.outcome.usage.cost_usd == used["cost_usd"]
    outputs = run.outcome.outputs
    assert outputs["text"] == "안녕하세요, 지우님. 오늘도 좋은 하루" and outputs["json"] is None
    assert outputs["usage"]["input_tokens"] == 12
    request = adapter.requests[0]
    assert request.model == "gemini-3.8-flash" and request.params.temperature == 0.2
    assert request.system == "짧게 답한다"
    assert [m.role for m in request.messages] == ["user"]
    completed = [e for e in run.events if e.type == "node.completed" and e.node_run]
    record = next(e.node_run for e in completed if e.node_run and e.node_run.node_id == "llm_1")
    assert record is not None and record.usage is not None and record.usage.output_tokens == 7
    assert record.inputs["messages"] == [
        {"role": "system", "text": "짧게 답한다"},
        {"role": "user", "text": "지우에게 인사"},
    ]
    assert record.process["model"] == "fake-model" and record.process["stop_reason"] == "end_turn"


async def test_neighbouring_blocks_merge_and_empty_blocks_are_dropped(
    kit: Any, make_node: Node
) -> None:
    adapter = FakeAdapter([fake_text("ok")])
    graph = llm_graph(
        kit,
        make_node,
        {
            "prompt": prompt(
                ("system", "규칙 1"),
                ("user", "{{ start.q }}"),
                ("user", "둘째 줄"),
                ("assistant", ""),
                ("system", "규칙 2"),
            )
        },
    )

    run = await kit.run(graph, inputs={"q": "첫 줄"}, ports=kit.ports(model=adapter))

    assert run.outcome.status == "succeeded"
    request = adapter.requests[0]
    assert request.system == "규칙 1\n\n규칙 2"
    [message] = request.messages
    assert [b.text for b in message.blocks if isinstance(b, TextBlock)] == ["첫 줄", "둘째 줄"]


async def test_json_mode_asks_again_once_and_checks_the_schema(kit: Any, make_node: Node) -> None:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    adapter = FakeAdapter(
        [
            [*fake_text("도시는 서울입니다"), fake_end(usage=usage(10, 3))],
            [*fake_text('```json\n{"city": "서울"}\n```'), fake_end(usage=usage(20, 4))],
        ]
    )
    graph = llm_graph(
        kit,
        make_node,
        {"prompt": prompt(("user", "도시?")), "json_mode": True, "output_schema": schema},
    )

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["json"] == {"city": "서울"}
    assert run.outcome.usage.input_tokens == 30
    assert len(run.of("node.usage")) == 2
    first, second = adapter.requests
    assert "JSON 객체 하나만" in first.system and '"city"' in first.system
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]


async def test_json_that_never_fits_fails_but_keeps_the_usage(kit: Any, make_node: Node) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
    adapter = FakeAdapter(
        [
            [*fake_text('{"n": "많이"}'), fake_end(usage=usage(5, 1))],
            [*fake_text("모르겠다"), fake_end(usage=usage(6, 1))],
        ]
    )
    graph = llm_graph(
        kit,
        make_node,
        {"prompt": prompt(("user", "수?")), "json_mode": True, "output_schema": schema},
    )

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "invalid_json"
    assert run.outcome.usage.input_tokens == 11


async def test_model_errors_fail_with_the_adapter_code(kit: Any, make_node: Node) -> None:
    adapter = FakeAdapter(
        [[ErrorEvent(code="model_request_invalid", message="400 잘못된 요청", retryable=False)]]
    )
    graph = llm_graph(
        kit,
        make_node,
        {"prompt": prompt(("user", "hi"))},
        retry={"max_attempts": 3, "interval_ms": 0},
    )

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "model_request_invalid"
    assert run.started().count("llm_1") == 1


class _Transport:
    """GeminiAdapter의 가짜 전송. 단계마다 429 오류나 응답 조각을 낸다."""

    def __init__(self, steps: list[TransportError | list[dict[str, Any]]]) -> None:
        self.steps = steps
        self.calls = 0

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, TransportError):
            raise step
        for chunk in step:
            yield chunk

    async def list_models(self, *, api_key: str) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


def _reply(text: str) -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        },
    ]


async def test_retry_policy_runs_again_after_every_key_hits_429(kit: Any, make_node: Node) -> None:
    rate_limited = TransportError("RESOURCE_EXHAUSTED", status=429)
    transport = _Transport([rate_limited, rate_limited, _reply("다시 해서 됐다")])
    adapter = GeminiAdapter(
        api_keys=["test-key-a", "test-key-b"],
        model_fallback=["gemini-3.8-flash"],
        key_cooldown_sec=0,
        transport=transport,
    )
    graph = llm_graph(
        kit,
        make_node,
        {"prompt": prompt(("user", "hi"))},
        retry={"max_attempts": 2, "interval_ms": 0},
    )

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["text"] == "다시 해서 됐다"
    [retrying] = run.of("node.retrying")
    assert (retrying["reason"], retrying["attempt"]) == ("model_unavailable", 2)
    statuses = [(d["node_id"], d["status"]) for d in run.of("node.completed")]
    assert statuses.count(("llm_1", "failed")) == 1 and ("llm_1", "succeeded") in statuses
    assert transport.calls == 3
    assert run.outcome.usage.input_tokens == 9


async def test_429_without_a_retry_policy_fails_as_retryable(kit: Any, make_node: Node) -> None:
    rate_limited = TransportError("RESOURCE_EXHAUSTED", status=429)
    adapter = GeminiAdapter(
        api_keys=["test-key-a"],
        model_fallback=["gemini-3.8-flash"],
        key_cooldown_sec=0,
        transport=_Transport([rate_limited]),
    )
    graph = llm_graph(kit, make_node, {"prompt": prompt(("user", "hi"))})

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.error is not None
    assert (run.outcome.error.code, run.outcome.error.retryable) == ("model_unavailable", True)


class _SmallWindow(FakeAdapter):
    def capabilities(self, model_id: str) -> ModelCapabilities:
        return ModelCapabilities(model_id=model_id, context_window=20)


async def test_prompt_over_the_context_window_fails_before_calling(
    kit: Any, make_node: Node
) -> None:
    adapter = _SmallWindow([fake_text("안 불린다")])
    graph = llm_graph(kit, make_node, {"prompt": prompt(("user", "가" * 400))})

    run = await kit.run(graph, ports=kit.ports(model=adapter))

    assert run.outcome.error is not None and run.outcome.error.code == "context_too_long"
    assert adapter.requests == []


class _Files:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def save_output(self, source: Any, *, filename: str, mime: str | None) -> dict[str, Any]:
        raise AssertionError("쓰지 않는다")

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes:
        self.reads.append(str(ref["filename"]))
        return b"\x89PNG-bytes"


def file_ref(name: str, mime: str) -> dict[str, Any]:
    return {
        "file_id": "0190c1b2-0000-7000-8000-000000000001",
        "filename": name,
        "mime": mime,
        "size_bytes": 10,
        "storage_key": "wf-files/w/f",
        "source": "wf_file",
    }


async def test_vision_attaches_images_to_the_last_user_block(kit: Any, make_node: Node) -> None:
    adapter = FakeAdapter([fake_text("고양이 사진이다")])
    files = _Files()
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "docs", "type": "file_list"}]}),
            make_node(
                "llm_1",
                "llm",
                {
                    "prompt": prompt(("user", "첫 질문"), ("assistant", "네"), ("user", "이건?")),
                    "vision": {"enabled": True, "var": "start.docs"},
                },
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "llm_1.text"}]}),
        ],
        ["start>llm_1", "llm_1>end_1"],
    )

    run = await kit.run(
        graph,
        inputs={
            "docs": [file_ref("cat.png", "image/png"), file_ref("memo.pdf", "application/pdf")]
        },
        ports=kit.ports(model=adapter, files=files),
    )

    assert run.outcome.status == "succeeded"
    assert files.reads == ["cat.png"]
    last = adapter.requests[0].messages[-1]
    images = [block for block in last.blocks if isinstance(block, ImageBlock)]
    assert [(i.media_type, i.filename) for i in images] == [("image/png", "cat.png")]
    assert images[0].data_b64 is not None
    [completed] = [d for d in run.of("node.completed") if d["node_id"] == "llm_1"]
    assert any(w.startswith("vision_skipped") for w in completed["warnings"])


def test_validation_rules(kit: Any, make_node: Node) -> None:
    def issues(
        config: dict[str, Any], kind: WorkflowKind = "workflow"
    ) -> list[tuple[str, str | None]]:
        graph = llm_graph(kit, make_node, config)
        return [(i.code, i.field) for i in validate(graph, kind) if i.node_id == "llm_1"]

    assert issues({"prompt": prompt(("system", "규칙"))}) == [("prompt_missing_user", "prompt")]
    assert issues(
        {"prompt": prompt(("user", "x")), "json_mode": True, "output_schema": {"type": "array"}}
    ) == [("invalid_output_schema", "output_schema")]
    assert issues({"prompt": prompt(("user", "x")), "output_schema": {"type": "object"}}) == [
        ("output_schema_ignored", "output_schema")
    ]
    assert issues({"prompt": prompt(("user", "x")), "memory": {"enabled": True}}) == [
        ("memory_ignored", "memory.enabled")
    ]
    assert issues({"prompt": prompt(("user", "x")), "vision": {"enabled": True}}) == [
        ("invalid_config", "vision.var")
    ]
    assert issues({"prompt": prompt(("user", "{{ nope.x }}"))}) == [
        ("unknown_variable", "prompt[0].text")
    ]
    assert issues({"prompt": prompt(("user", "x")), "model": {"params": {"top_p": 2}}})[0][0] == (
        "invalid_config"
    )


def test_json_extraction_looks_inside_code_blocks_and_prose() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('답: ```json\n{"a": [1, 2]}\n``` 끝') == {"a": [1, 2]}
    assert extract_json_object('결과는 {"a": {"b": 2}} 입니다') == {"a": {"b": 2}}
    assert extract_json_object("[1, 2]") is None
    assert extract_json_object("없음") is None


async def test_cancelling_the_run_closes_the_model_stream(kit: Any, make_node: Node) -> None:
    closed: list[bool] = []

    class _Slow(FakeAdapter):
        async def stream(self, req: ChatRequest) -> AsyncGenerator[Any]:
            try:
                async for event in super().stream(req):
                    yield event
            finally:
                closed.append(True)

    adapter = _Slow([fake_text("천천히 오는 아주 긴 답이다" * 10, chunk_size=2)], delay_sec=0.02)
    cancel = CancelSignal()
    graph = llm_graph(kit, make_node, {"prompt": prompt(("user", "hi"))})

    async def stop_on_delta(event: Any) -> None:
        if event.type == "node.text_delta":
            cancel.set("cancel")

    run = await kit.run(
        graph, ports=kit.ports(model=adapter), cancel=cancel, on_event=stop_on_delta
    )

    assert run.outcome.status == "cancelled"
    assert run.statuses()["llm_1"] == "cancelled"
    assert closed == [True]
