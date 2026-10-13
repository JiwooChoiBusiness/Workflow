from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm import make_adapter
from app.llm.fake_adapter import FakeAdapter
from app.llm.gemini_adapter import GeminiAdapter, TransportError
from app.services.llm_status import LlmStatusService

LIVE_MODELS = ["models/gemini-3.8-flash", "models/text-embedding-004", "models/gemini-2.5-pro"]
GEMINI_3_FLASH_CAPS = {
    "context_window": 1_048_576,
    "max_output_tokens": 65_536,
    "supports_tools": True,
    "reasoning_efforts": ["low", "medium", "high"],
    "known": True,
}

Handler = Callable[[httpx.Request], httpx.Response]


def models_response(ids: list[str] = LIVE_MODELS) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


class FakeGemini:
    def __init__(self, settings: Settings, respond: Callable[[int], httpx.Response]) -> None:
        self.keys = [key.get_secret_value() for key in settings.gemini_api_keys]
        self.respond = respond
        self.calls: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1beta/openai/models")
        index = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        self.calls.append(index)
        return self.respond(index)


class FakeChat:
    """어댑터의 생성 호출을 받는 전송 계층. outcomes를 앞에서부터 쓰고, 다 쓰면 '확인'으로 답한다.

    문자열은 그 텍스트로 답하고, 정수는 그 HTTP 상태로 실패한다.
    """

    def __init__(self, keys: list[str], outcomes: list[str | int] | None = None) -> None:
        self.keys = keys
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, int]] = []
        self.bodies: list[dict[str, Any]] = []

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append((str(body["model"]), self.keys.index(api_key)))
        self.bodies.append(dict(body))
        outcome = self.outcomes.pop(0) if self.outcomes else "확인"
        if isinstance(outcome, int):
            raise TransportError(f"status {outcome}", status=outcome)
        yield {"choices": [{"index": 0, "delta": {"role": "assistant", "content": outcome}}]}
        yield {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 20},
        }

    async def list_models(self, *, api_key: str) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


def use_fake(
    app: FastAPI,
    settings: Settings,
    respond: Callable[[int], httpx.Response],
    outcomes: list[str | int] | None = None,
) -> tuple[FakeGemini, FakeChat, GeminiAdapter]:
    fake = FakeGemini(settings, respond)
    chat = FakeChat(fake.keys, outcomes)
    adapter = GeminiAdapter(
        api_keys=settings.gemini_api_keys,
        model_fallback=settings.gemini_model_fallback,
        transport=chat,
    )
    app.state.llm_status = LlmStatusService(settings, adapter, transport=httpx.MockTransport(fake))
    return fake, chat, adapter


async def test_llm_endpoints_require_login(client: AsyncClient, account: dict[str, Any]) -> None:
    for response in (
        await client.get("/api/console/llm"),
        await client.post("/api/console/llm/test", json={}),
        await client.get("/api/console/models"),
    ):
        assert response.status_code == 401


def idle_key(index: int, last4: str) -> dict[str, Any]:
    return {
        "index": index,
        "last4": last4,
        "cooldown_until": None,
        "cooldown_by_model": {},
        "disabled": False,
        "success_count": 0,
        "failure_count": 0,
        "rate_limited_count": 0,
        "last_429_at": None,
    }


async def test_status_shows_only_index_and_last4(
    logged_in: AsyncClient, settings: Settings
) -> None:
    response = await logged_in.get("/api/console/llm")

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 3
    assert body["keys"] == [idle_key(0, "k000"), idle_key(1, "k001"), idle_key(2, "k002")]
    assert body["fallback_models"] == settings.gemini_model_fallback
    assert body["base_url"] == settings.gemini_openai_base_url
    assert body["fake"] is False
    for key in settings.gemini_api_keys:
        assert key.get_secret_value() not in response.text
    assert "AIza" not in response.text


async def test_status_reports_key_pool_cooldown_and_counts(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _, _, adapter = use_fake(app, settings, lambda index: models_response())
    adapter.key_pool.mark_success(0)
    adapter.key_pool.mark_rate_limited(1, "gemini-3.8-flash")
    adapter.key_pool.mark_rate_limited(1, "gemini-3.7-flash")
    adapter.key_pool.mark_auth_failed(2)

    keys = (await logged_in.get("/api/console/llm")).json()["keys"]

    assert keys[0]["success_count"] == 1 and keys[0]["cooldown_until"] is None
    assert set(keys[1]["cooldown_by_model"]) == {"gemini-3.8-flash", "gemini-3.7-flash"}
    assert keys[1]["cooldown_until"] == max(keys[1]["cooldown_by_model"].values())
    assert (keys[1]["rate_limited_count"], keys[1]["failure_count"]) == (2, 2)
    assert keys[1]["last_429_at"] is not None
    assert (keys[2]["disabled"], keys[2]["failure_count"]) == (True, 1)


async def test_connection_test_lists_models_and_generates(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    fake, chat, _ = use_fake(app, settings, lambda index: models_response())

    response = await logged_in.post("/api/console/llm/test", json={})

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["key_index"] == 0
    assert body["key_last4"] == "k000"
    assert body["models"] == ["gemini-2.5-pro", "gemini-3.8-flash", "text-embedding-004"]
    assert body["model_count"] == 3
    assert [a["status_code"] for a in body["attempts"]] == [200]
    assert fake.calls == [0]
    generation = body["generation"]
    assert generation["ok"] is True
    assert generation["text"] == "확인"
    assert (generation["model"], generation["key_index"]) == ("gemini-3.8-flash", 0)
    assert generation["key_last4"] == "k000"
    assert generation["stop_reason"] == "end_turn"
    assert generation["latency_ms"] >= 0
    assert generation["usage"]["input_tokens"] == 12
    # 사고 토큰(total - prompt - completion)을 출력에 더한다.
    assert generation["usage"]["output_tokens"] == 8
    # 짧은 확인 호출이다: 사고 수준 low, 출력 상한 256(설계 12.2).
    assert [(b["reasoning_effort"], b["max_completion_tokens"]) for b in chat.bodies] == [
        ("low", 256)
    ]


async def test_connection_test_moves_to_next_key_on_429(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    def respond(index: int) -> httpx.Response:
        if index == 0:
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})
        return models_response()

    fake, _, _ = use_fake(app, settings, respond)

    body = (await logged_in.post("/api/console/llm/test", json={})).json()

    assert body["ok"] is True
    assert body["key_index"] == 1
    assert [(a["key_index"], a["status_code"]) for a in body["attempts"]] == [(0, 429), (1, 200)]
    assert body["attempts"][0]["error"]["code"] == "rate_limited"
    assert fake.calls == [0, 1]


async def test_models_check_moves_to_next_key_on_auth_error(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    def respond(index: int) -> httpx.Response:
        if index == 0:
            return httpx.Response(403, json={"error": {"message": "permission denied"}})
        return models_response()

    fake, _, _ = use_fake(app, settings, respond)

    body = (await logged_in.post("/api/console/llm/test", json={})).json()

    assert body["ok"] is True
    assert [(a["key_index"], a["status_code"]) for a in body["attempts"]] == [(0, 403), (1, 200)]
    assert body["attempts"][0]["error"]["code"] == "model_auth"
    assert fake.calls == [0, 1]


async def test_connection_test_with_explicit_key(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    fake, _, _ = use_fake(app, settings, lambda index: models_response())

    body = (await logged_in.post("/api/console/llm/test", json={"key_index": 2})).json()
    out_of_range = await logged_in.post("/api/console/llm/test", json={"key_index": 3})

    assert body["key_index"] == 2
    assert fake.calls == [2]
    # 생성 호출은 key_index와 관계없이 어댑터의 키 순환을 따른다.
    assert body["generation"]["key_index"] == 0
    assert out_of_range.status_code == 422
    assert out_of_range.json()["error"]["code"] == "invalid_key_index"


async def test_generation_failure_fails_the_connection_test(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    keys = [key.get_secret_value() for key in settings.gemini_api_keys]
    use_fake(app, settings, lambda index: models_response(), outcomes=[400])

    response = await logged_in.post("/api/console/llm/test", json={})

    body = response.json()
    assert body["ok"] is False
    assert body["model_count"] == 3
    generation = body["generation"]
    assert generation["ok"] is False
    assert generation["error"]["code"] == "model_request_invalid"
    assert generation["error"]["status_code"] == 400
    assert body["error"] == generation["error"]
    assert all(key not in response.text for key in keys)


async def test_auth_error_stops_and_masks_key(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    keys = [key.get_secret_value() for key in settings.gemini_api_keys]

    def respond(index: int) -> httpx.Response:
        # Google이 오류 메시지에 키를 되돌려 주는 경우를 가정한다.
        return httpx.Response(
            400, json=[{"error": {"message": f"API key not valid: {keys[index]}"}}]
        )

    fake, _, _ = use_fake(app, settings, respond)

    response = await logged_in.post("/api/console/llm/test", json={})

    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "model_request_invalid"
    assert body["error"]["message"] == "API key not valid: ***"
    assert fake.calls == [0]
    assert all(key not in response.text for key in keys)


async def test_connection_errors_try_every_key(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    def respond(index: int) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake, _, _ = use_fake(app, settings, respond)

    body = (await logged_in.post("/api/console/llm/test", json={})).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "connection_error"
    assert fake.calls == [0, 1, 2]
    assert body["models"] == []


async def test_models_puts_fallback_first_and_caches(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    fake, _, _ = use_fake(app, settings, lambda index: models_response())

    first = (await logged_in.get("/api/console/models")).json()
    second = (await logged_in.get("/api/console/models")).json()

    assert first == second
    assert fake.calls == [0]
    assert first["live_ok"] is True
    assert first["fetched_at"] is not None
    minimal = {**GEMINI_3_FLASH_CAPS, "reasoning_efforts": ["minimal", "low", "medium", "high"]}
    assert first["models"] == [
        {
            "id": "gemini-3.8-flash",
            "fallback_index": 0,
            "live": True,
            "capabilities": GEMINI_3_FLASH_CAPS,
        },
        {
            "id": "gemini-3.7-flash",
            "fallback_index": 1,
            "live": False,
            "capabilities": GEMINI_3_FLASH_CAPS,
        },
        {"id": "gemini-3.6-flash", "fallback_index": 2, "live": False, "capabilities": minimal},
        {"id": "gemini-2.5-pro", "fallback_index": None, "live": True, "capabilities": None},
        {"id": "text-embedding-004", "fallback_index": None, "live": True, "capabilities": None},
    ]


async def test_models_without_live_list(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    use_fake(app, settings, lambda index: httpx.Response(503, text="unavailable"))

    body = (await logged_in.get("/api/console/models")).json()

    assert body["live_ok"] is False
    assert body["live_error"]["code"] == "upstream_error"
    assert [m["id"] for m in body["models"]] == settings.gemini_model_fallback
    assert {m["live"] for m in body["models"]} == {None}
    assert all(m["capabilities"]["known"] for m in body["models"])


async def test_unknown_fallback_model_gets_default_capabilities(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    custom = settings.model_copy(update={"gemini_model_fallback": ["gemini-custom"]})
    app.state.llm_status = LlmStatusService(
        custom,
        make_adapter(custom),
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )

    body = (await logged_in.get("/api/console/models")).json()

    caps = body["models"][0]["capabilities"]
    assert caps["known"] is False
    assert (caps["context_window"], caps["max_output_tokens"]) == (1_000_000, 65_536)


async def test_without_keys(logged_in: AsyncClient, app: FastAPI, settings: Settings) -> None:
    no_keys = settings.model_copy(update={"gemini_api_keys": []})
    app.state.llm_status = LlmStatusService(no_keys, make_adapter(no_keys))

    status = (await logged_in.get("/api/console/llm")).json()
    test = (await logged_in.post("/api/console/llm/test", json={})).json()

    assert status["key_count"] == 0
    assert test["ok"] is False
    assert test["error"]["code"] == "no_api_keys"
    assert test["generation"]["error"]["code"] == "model_auth"


@pytest.mark.parametrize("path", ["/api/console/llm/test", "/api/console/models"])
async def test_fake_mode_skips_network(
    logged_in: AsyncClient, app: FastAPI, settings: Settings, path: str
) -> None:
    def respond(index: int) -> httpx.Response:
        raise AssertionError("가짜 모드에서는 Gemini를 부르지 않는다")

    fake_settings = settings.model_copy(update={"llm_fake": True})
    adapter = FakeAdapter(turns=[])
    app.state.llm_status = LlmStatusService(
        fake_settings, adapter, transport=httpx.MockTransport(FakeGemini(settings, respond))
    )

    if path.endswith("/test"):
        response = await logged_in.post(path, json={})
    else:
        response = await logged_in.get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["fake"] is True
    if path.endswith("/test"):
        # 각본의 턴을 쓰지 않도록 가짜 어댑터도 부르지 않는다.
        assert body["generation"] is None
        assert adapter.requests == []
    else:
        assert all(m["capabilities"] is not None for m in body["models"])
