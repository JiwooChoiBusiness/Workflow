import pytest

from app.config import Settings
from app.llm.catalog import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ModelCapabilities,
    get_capabilities,
    list_capabilities,
    resolve_reasoning_effort,
)
from app.llm.types import ReasoningEffort


@pytest.mark.parametrize("model_id", ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"])
def test_live_checked_limits(model_id: str) -> None:
    caps = get_capabilities(model_id)

    # 2026-10-13 GET /v1beta/models/<id>: inputTokenLimit 1048576, outputTokenLimit 65536
    assert caps.known
    assert caps.model_id == model_id
    assert caps.context_window == 1_048_576
    assert caps.max_output_tokens == 65_536
    assert caps.supports_tools
    assert caps.supports_reasoning_effort


def test_reasoning_effort_values_per_model() -> None:
    # 3.8·3.7은 minimal을 400으로 거부했고 3.6은 받았다(2026-10-13).
    assert get_capabilities("gemini-3.8-flash").reasoning_efforts == ("low", "medium", "high")
    assert get_capabilities("gemini-3.7-flash").reasoning_efforts == ("low", "medium", "high")
    assert get_capabilities("gemini-3.6-flash").reasoning_efforts == (
        "minimal",
        "low",
        "medium",
        "high",
    )


def test_unknown_model_gets_conservative_defaults() -> None:
    caps = get_capabilities("gemini-9-ultra")

    assert not caps.known
    assert caps.model_id == "gemini-9-ultra"
    assert caps.context_window == DEFAULT_CONTEXT_WINDOW == 1_000_000
    assert caps.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS == 65_536


def test_default_fallback_models_are_all_in_the_catalog() -> None:
    fallback = Settings().gemini_model_fallback

    assert [caps.model_id for caps in list_capabilities()] == fallback


@pytest.mark.parametrize(
    ("model_id", "requested", "expected"),
    [
        ("gemini-3.8-flash", "minimal", "low"),
        ("gemini-3.7-flash", "minimal", "low"),
        ("gemini-3.6-flash", "minimal", "minimal"),
        ("gemini-3.8-flash", "high", "high"),
        ("gemini-3.8-flash", None, None),
    ],
)
def test_resolve_reasoning_effort(
    model_id: str, requested: ReasoningEffort | None, expected: ReasoningEffort | None
) -> None:
    assert resolve_reasoning_effort(get_capabilities(model_id), requested) == expected


def test_resolve_reasoning_effort_edge_cases() -> None:
    no_effort = ModelCapabilities(model_id="x", supports_reasoning_effort=False)
    only_minimal = ModelCapabilities(model_id="y", reasoning_efforts=("minimal",))

    assert resolve_reasoning_effort(no_effort, "high") is None
    # 위쪽에 허용값이 없으면 가장 가까운 아래 단계로 내린다.
    assert resolve_reasoning_effort(only_minimal, "high") == "minimal"
