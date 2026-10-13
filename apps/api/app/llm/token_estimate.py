import json
import math
from collections.abc import Iterable, Sequence

from app.llm.types import (
    Block,
    CanonicalMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

# 설계 4.2의 휴리스틱: 문자 수 / 3.5 + 블록당 가산. 한국어는 실제보다 적게 나오므로
# 어댑터가 직전 응답의 usage로 비율을 보정한다(GeminiAdapter.estimate_tokens).
CHARS_PER_TOKEN = 3.5
BLOCK_OVERHEAD = 4
MESSAGE_OVERHEAD = 4
TOOL_OVERHEAD = 8
# base64 길이로 세면 수십만이 나오므로 고정값을 쓴다. Gemini 3 기본 해상도의 이미지 1장 토큰 수.
IMAGE_TOKENS = 1120


def estimate_text(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_block(block: Block) -> int:
    match block:
        case TextBlock():
            body = estimate_text(block.text)
        case ImageBlock():
            body = IMAGE_TOKENS
        case ToolUseBlock():
            body = estimate_text(block.name) + estimate_text(
                json.dumps(block.input, ensure_ascii=False)
            )
        case ToolResultBlock():
            body = estimate_text(block.content)
    return body + BLOCK_OVERHEAD


def estimate_blocks(blocks: Iterable[Block]) -> int:
    return sum(estimate_block(block) for block in blocks)


def estimate_messages(messages: Iterable[CanonicalMessage]) -> int:
    return sum(estimate_blocks(message.blocks) + MESSAGE_OVERHEAD for message in messages)


def estimate_tools(tools: Sequence[ToolSpec]) -> int:
    return sum(
        estimate_text(tool.name)
        + estimate_text(tool.description)
        + estimate_text(json.dumps(tool.input_schema, ensure_ascii=False))
        + TOOL_OVERHEAD
        for tool in tools
    )


def estimate_request(req: ChatRequest) -> int:
    return (
        estimate_text(req.system)
        + MESSAGE_OVERHEAD
        + estimate_messages(req.messages)
        + estimate_tools(req.tools)
    )
