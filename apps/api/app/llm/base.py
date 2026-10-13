from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.llm.catalog import ModelCapabilities
from app.llm.types import ChatRequest, ModelEvent


@runtime_checkable
class ModelAdapter(Protocol):
    name: str  # "gemini" | "fake"

    # 구현은 async generator다. 호출하면 바로 이터레이터가 나오므로 await 없이 `async for`로 쓴다.
    # 프로바이더 오류는 예외가 아니라 ErrorEvent 한 개로 끝난다. 취소는 소비 태스크를 취소하면 된다.
    def stream(self, req: ChatRequest) -> AsyncIterator[ModelEvent]: ...

    async def list_models(self) -> list[str]: ...

    def capabilities(self, model_id: str) -> ModelCapabilities: ...

    def estimate_tokens(self, req: ChatRequest) -> int: ...

    # 서버 종료 때 HTTP 연결을 닫는다. 닫은 뒤 다시 부르면 연결을 새로 연다.
    async def aclose(self) -> None: ...
