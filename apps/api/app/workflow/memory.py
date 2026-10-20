# pyright: strict
"""챗플로우 메모리(2단계 설계 2.3의 llm, 11.2).

대화(wf_message)의 최근 N턴, 즉 사용자 글과 그 턴의 답을 프롬프트 메시지로 바꾼다. 1단계의 요약
압축은 쓰지 않는다(턴 수 창으로 충분하다). 엔진은 DB를 모르므로 실행기가 그래프에서 가장 큰 창만큼
읽어 RunContext.history로 넘기고, llm 노드가 자기 창만큼 잘라 쓴다.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.types import CanonicalMessage, TextBlock
from app.schemas.workflow import Graph

if TYPE_CHECKING:
    from app.workflow.nodes.base import NodeRegistry

MAX_WINDOW_TURNS = 50


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    window_turns: int = Field(default=10, ge=1, le=MAX_WINDOW_TURNS)


@dataclass(frozen=True, slots=True)
class Turn:
    query: str
    answer: str


def recent(turns: Sequence[Turn], window: int) -> list[Turn]:
    """오래된 것부터 쌓인 턴 중 마지막 window개."""
    if window <= 0:
        return []
    return list(turns[-window:])


def turn_messages(turns: Iterable[Turn]) -> list[CanonicalMessage]:
    messages: list[CanonicalMessage] = []
    for turn in turns:
        messages.append(CanonicalMessage(role="user", blocks=[TextBlock(text=turn.query)]))
        # 답이 빈 턴도 자리를 지켜야 user·assistant가 번갈아 온다.
        answer = turn.answer or "(빈 응답)"
        messages.append(CanonicalMessage(role="assistant", blocks=[TextBlock(text=answer)]))
    return messages


def memory_window(graph: Graph, registry: "NodeRegistry") -> int:
    """메모리를 켠 노드의 가장 큰 창(턴 수). 켠 노드가 없으면 0이다.

    config에 memory(MemoryConfig)가 있는 노드면 타입을 가리지 않는다. 읽지 못하는 config는 실행할 때
    검증이 걸러 내므로 여기서는 건너뛴다.
    """
    widest = 0
    for node in graph.nodes:
        runner = registry.get(node.type, node.version)
        if runner is None:
            continue
        try:
            config = runner.parse_config(node.config)
        except ValidationError:
            continue
        memory = getattr(config, "memory", None)
        if isinstance(memory, MemoryConfig) and memory.enabled:
            widest = max(widest, memory.window_turns)
    return widest
