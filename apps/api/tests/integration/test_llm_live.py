import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.agent.child_runs import SubAgentChoice
from app.agent.tools.call_agent import CallAgentTool
from app.config import REPO_ROOT, Settings, get_settings
from app.llm.catalog import get_capabilities
from app.llm.gemini_adapter import (
    GeminiAdapter,
    OpenAICompatTransport,
    TransportError,
    thought_signature,
)
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    ForcedTool,
    MessageEnd,
    ModelEvent,
    ModelParams,
    TextBlock,
    TextDelta,
    ToolCallEnd,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from app.schemas.agent_version import ResultSpec

# 실제 Gemini API를 부른다. 키는 앱 설정(.env의 GEMINI_API_KEYS)으로만 받고, 없으면 건너뛴다.
# 실행마다 생성 호출이 여러 번 나가므로 기본 실행에서 빠지고 `uv run pytest -m live`로만 돈다.
# 호출 수를 줄이려고 프롬프트를 짧게, 사고 수준을 low로 둔다.
pytestmark = pytest.mark.live

SHELL = ToolSpec(
    name="shell",
    description="Run a PowerShell command in the session workspace and return its output.",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)
LOW = ModelParams(max_output_tokens=1024, reasoning_effort="low")
LISTING = (
    "    Directory: C:\\wf-data\\workspaces\\demo\n\n"
    "Mode                 LastWriteTime         Length Name\n"
    "----                 -------------         ------ ----\n"
    "-a----        2026-10-13     09:01            120 notes.txt\n"
)


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # 상위 conftest가 테스트마다 .env 읽기를 끊어 두므로 이 모듈에서만 되돌린다.
    monkeypatch.setitem(Settings.model_config, "env_file", REPO_ROOT / ".env")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    if not settings.gemini_api_keys:
        pytest.skip("GEMINI_API_KEYS가 설정에 없어 라이브 테스트를 건너뛴다")
    return settings


@pytest.fixture
async def adapter(live_settings: Settings) -> AsyncIterator[GeminiAdapter]:
    gemini = GeminiAdapter.from_settings(live_settings)
    yield gemini
    await gemini.aclose()


def user(text: str) -> CanonicalMessage:
    return CanonicalMessage(role="user", blocks=[TextBlock(text=text)])


async def collect(adapter: GeminiAdapter, req: ChatRequest) -> list[ModelEvent]:
    return [event async for event in adapter.stream(req)]


def finished(events: list[ModelEvent]) -> MessageEnd:
    end = events[-1]
    if isinstance(end, ErrorEvent) and end.code == "model_unavailable":
        pytest.skip(f"Gemini가 일시적으로 응답하지 않는다: {end.message[:200]}")
    assert isinstance(end, MessageEnd), end
    return end


async def test_list_models_and_catalog_limits(
    live_settings: Settings, adapter: GeminiAdapter
) -> None:
    models = await adapter.list_models()

    assert set(live_settings.gemini_model_fallback) <= set(models)
    # 능력표의 상한이 API 값과 같은지 본다.
    # 상한은 OpenAI 호환 /models에 없어서 네이티브 모델 정보를 읽는다.
    native = live_settings.gemini_openai_base_url.rstrip("/").removesuffix("/openai")
    async with httpx.AsyncClient(timeout=30) as http:
        for model in live_settings.gemini_model_fallback:
            response = await http.get(
                f"{native}/models/{model}",
                headers={"x-goog-api-key": live_settings.gemini_api_keys[0].get_secret_value()},
            )
            assert response.status_code == 200, model
            info = response.json()
            caps = get_capabilities(model)
            assert caps.context_window == info["inputTokenLimit"], model
            assert caps.max_output_tokens == info["outputTokenLimit"], model


async def test_korean_text_reply(live_settings: Settings, adapter: GeminiAdapter) -> None:
    req = ChatRequest(
        system="간결하게 답한다.", messages=[user("한국어로 한 문장만 인사해줘.")], params=LOW
    )

    events = await collect(adapter, req)

    end = finished(events)
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert re.search("[가-힣]", text), text
    assert end.stop_reason == "end_turn"
    assert end.model in live_settings.gemini_model_fallback
    assert not end.usage.estimated
    assert end.usage.input_tokens > 0
    assert end.usage.output_tokens > 0


async def test_shell_tool_call_then_signature_resend(adapter: GeminiAdapter) -> None:
    first_user = user("Get-ChildItem을 실행해")
    req = ChatRequest(
        system="작업공간에서 도구를 써서 일한다.", messages=[first_user], tools=[SHELL], params=LOW
    )

    events = await collect(adapter, req)

    end = finished(events)
    kinds = [event.type for event in events]
    assert {"tool_call_start", "tool_call_delta", "tool_call_end"} <= set(kinds), kinds
    assert (
        kinds.index("tool_call_start")
        < kinds.index("tool_call_delta")
        < kinds.index("tool_call_end")
    )
    call = next(event for event in events if isinstance(event, ToolCallEnd))
    assert call.name == "shell"
    arguments = json.loads(call.arguments)
    assert "command" in arguments
    # 사고 서명 경로(extra_content.google.thought_signature)가 바뀌지 않았는지 확인한다.
    assert thought_signature(call.provider_extra), call.provider_extra
    assert end.stop_reason == "tool_use"
    assert not end.usage.estimated
    assert end.usage.input_tokens > 0

    # 서명을 그대로 돌려보낸 다음 턴이 400 없이 이어져야 한다.
    history = [
        first_user,
        CanonicalMessage(
            role="assistant",
            blocks=[
                ToolUseBlock(
                    id=call.id,
                    name=call.name,
                    input=arguments,
                    provider_extra=call.provider_extra,
                )
            ],
            provider_extra=end.provider_extra,
        ),
        CanonicalMessage(
            role="user", blocks=[ToolResultBlock(tool_use_id=call.id, content=LISTING)]
        ),
    ]
    follow_up = req.model_copy(update={"messages": history, "model": end.model})

    second = await collect(adapter, follow_up)

    assert not any(isinstance(event, ErrorEvent) for event in second), second[-1]
    assert finished(second).stop_reason in ("end_turn", "tool_use")


async def test_unsigned_tool_calls_are_accepted_with_skip_signature(
    adapter: GeminiAdapter,
) -> None:
    # 가짜 어댑터나 다른 모델이 만든 히스토리처럼 서명 없는 병렬 호출 두 개를 돌려보낸다.
    # 어댑터가 검증 생략용 더미 서명을 넣으므로 400 없이 이어져야 한다.
    history = [
        user("작업공간 위치와 파일 목록을 확인해"),
        CanonicalMessage(
            role="assistant",
            blocks=[
                ToolUseBlock(id="call_pwd", name="shell", input={"command": "Get-Location"}),
                ToolUseBlock(id="call_ls", name="shell", input={"command": "Get-ChildItem"}),
            ],
        ),
        CanonicalMessage(
            role="user",
            blocks=[
                ToolResultBlock(tool_use_id="call_pwd", content="C:\\wf-data\\workspaces\\demo"),
                ToolResultBlock(tool_use_id="call_ls", content=LISTING),
            ],
        ),
    ]
    req = ChatRequest(
        system="작업공간에서 도구를 써서 일한다.", messages=history, tools=[SHELL], params=LOW
    )

    events = await collect(adapter, req)

    # 서명 검증에 걸리면 model_request_invalid(400) 오류로 끝나 여기서 실패한다.
    assert finished(events).stop_reason in ("end_turn", "tool_use")


class UnavailableModels:
    """지정한 모델에는 503을 돌려주고 나머지는 실제 API로 보내는 전송 계층."""

    def __init__(self, inner: OpenAICompatTransport, unavailable: set[str]) -> None:
        self.inner = inner
        self.unavailable = unavailable
        self.models: list[str] = []

    def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        model = str(body["model"])
        self.models.append(model)
        if model in self.unavailable:
            return self._fail()
        return self.inner.stream_chat(api_key=api_key, body=body)

    async def _fail(self) -> AsyncGenerator[dict[str, Any], None]:
        raise TransportError("UNAVAILABLE: high demand (테스트에서 넣은 503)", status=503)
        yield {}  # 이 줄이 있어야 async generator가 된다

    async def list_models(self, *, api_key: str) -> list[str]:
        return await self.inner.list_models(api_key=api_key)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def test_fallback_reaches_last_model_when_others_return_503(
    live_settings: Settings,
) -> None:
    # 실제 3.8·3.7이 503을 낼 때를 기다릴 수 없어서 앞의 모델들에만 503을 넣는다.
    # 마지막 모델 호출은 실제 API로 간다.
    fallback = list(live_settings.gemini_model_fallback)
    transport = UnavailableModels(
        OpenAICompatTransport(live_settings.gemini_openai_base_url), set(fallback[:-1])
    )
    gemini = GeminiAdapter(
        api_keys=live_settings.gemini_api_keys,
        model_fallback=fallback,
        base_url=live_settings.gemini_openai_base_url,
        key_cooldown_sec=live_settings.gemini_key_cooldown_sec,
        transport=transport,
        retry_delay_sec=0.1,
    )
    try:
        events = await collect(
            gemini, ChatRequest(system="간결하게 답한다.", messages=[user("1+1은?")], params=LOW)
        )
    finally:
        await gemini.aclose()

    end = finished(events)
    assert end.model == fallback[-1]
    # 503 모델마다 같은 키로 한 번 더 시도한 뒤 다음 모델로 넘어간다.
    expected = [model for model in fallback[:-1] for _ in range(2)]
    assert transport.models[: len(expected)] == expected
    assert transport.models[len(expected)] == fallback[-1]


async def test_call_agent_with_output_schema_object_parameter(adapter: GeminiAdapter) -> None:
    # 오케스트레이션 라이브 확인 1: properties 없는 object 파라미터(output_schema)를 함수 선언으로
    # 받는지 본다. 거부되면(400) call_agent 자체가 붙지 않으므로 output_schema를 JSON 문자열로
    # 바꿔야 한다(설계 11.3).
    tool = CallAgentTool(
        [
            SubAgentChoice(
                agent_id=uuid4(),
                slug="writer",
                name="작성",
                description="요약을 JSON으로 돌려준다",
                result=ResultSpec(format="json"),
            )
        ]
    )
    spec = tool.spec()
    assert spec.input_schema["properties"]["output_schema"]["type"] == "object"
    assert "properties" not in spec.input_schema["properties"]["output_schema"]
    req = ChatRequest(
        system="하위 에이전트에게 일을 맡긴다.",
        messages=[
            user(
                "call_agent로 writer에게 '오늘 날씨를 한 줄로 요약'을 맡겨라. output_schema는 "
                '{"type":"object","properties":{"summary":{"type":"string"}},'
                '"required":["summary"],"additionalProperties":false}로 준다.'
            )
        ],
        tools=[spec],
        params=LOW.model_copy(update={"tool_choice": ForcedTool(name="call_agent")}),
    )

    events = await collect(adapter, req)

    end = finished(events)
    assert end.stop_reason == "tool_use"
    call = next(event for event in events if isinstance(event, ToolCallEnd))
    arguments = json.loads(call.arguments)
    assert call.name == "call_agent" and arguments["agent"] == "writer"
    assert isinstance(arguments.get("output_schema", {}), dict)
