from typing import TYPE_CHECKING

from app.llm.base import ModelAdapter
from app.llm.catalog import ModelCapabilities, get_capabilities, list_capabilities
from app.llm.fake_adapter import FakeAdapter
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.key_pool import KeyStatus
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    LLMError,
    MessageEnd,
    ModelEvent,
    ModelParams,
    ToolSpec,
    Usage,
)

if TYPE_CHECKING:
    from app.config import Settings

__all__ = [
    "CanonicalMessage",
    "ChatRequest",
    "ErrorEvent",
    "FakeAdapter",
    "GeminiAdapter",
    "KeyStatus",
    "LLMError",
    "MessageEnd",
    "ModelAdapter",
    "ModelCapabilities",
    "ModelEvent",
    "ModelParams",
    "ToolSpec",
    "Usage",
    "get_capabilities",
    "list_capabilities",
    "make_adapter",
]


def make_adapter(settings: "Settings") -> ModelAdapter:
    if settings.llm_fake:
        delay = settings.llm_fake_delay_sec
        if settings.llm_fake_script is not None:
            return FakeAdapter.from_file(settings.llm_fake_script, step_delay_sec=delay)
        return FakeAdapter(step_delay_sec=delay)
    return GeminiAdapter.from_settings(settings)
