import os
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

from app.config import Settings
from app.knowledge import index_db
from app.llm.errors import TransportError
from app.llm.fake_adapter import FakeEmbedder
from app.llm.gemini_adapter import GeminiAdapter, OpenAICompatTransport

# 1이면 index.db가 SQLite 3.43 미만 경로(본문을 저장하는 FTS5, 3.38 미만의 rowid 문서 필터)로 돈다.
# 이 PC와 CI 러너의 SQLite는 새 버전이라 CI ubuntu 잡이 지식 통합 시험을 이 값으로 한 번 더 돌린다
# (3단계 5.3). 두 모드를 직접 고르는 단위 시험(tests/unit/knowledge)에는 쓰지 않는다.
OLD_SQLITE_ENV = "KB_TEST_OLD_SQLITE"


@pytest.fixture(autouse=True)
def _old_sqlite_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(OLD_SQLITE_ENV) != "1":
        return
    monkeypatch.setattr(index_db, "CONTENTLESS_MIN_SQLITE", (999, 0, 0))
    monkeypatch.setattr(index_db, "DOC_FILTER_IN_MIN_SQLITE", (999, 0, 0))


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings는 model_validate로 만들어도 저장소의 .env와 환경변수를 읽는다.
    # 개발 PC의 값(실제 키, DATA_DIR)이 테스트에 섞이지 않게 둘 다 끊는다.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture(autouse=True)
def _no_real_gemini_calls(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """live 표시가 없는 시험은 어떤 경로로도 실제 Gemini를 부르지 않는다.

    시험 앱의 기본 어댑터는 가짜 키를 든 GeminiAdapter라, 지식 실행 객체가 그 전송을 그대로 받으면
    문서 색인·청크 편집·검색이 실제 임베딩 엔드포인트로 나간다(가짜 키라 400이 돌아온다). 그
    자리에는 가짜 임베더를 준다. 그래도 http_client 없이(실제 네트워크로) 나가는 호출은 보내지 않고
    기록해 두었다가 시험을 실패시킨다.
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    attempts: list[str] = []
    real = GeminiAdapter.embedding_transport

    def offline_transport(self: GeminiAdapter) -> Any:
        found = real.fget(self) if real.fget is not None else None
        return FakeEmbedder() if isinstance(found, OpenAICompatTransport) else found

    monkeypatch.setattr(GeminiAdapter, "embedding_transport", property(offline_transport))

    def refuse(transport: OpenAICompatTransport, what: str) -> TransportError | None:
        # http_client를 넣은 전송은 MockTransport로 응답을 흉내 내는 시험이다.
        if transport._http_client is not None:  # pyright: ignore[reportPrivateUsage]
            return None
        attempts.append(what)
        return TransportError("시험에서는 실제 Gemini를 부르지 않는다", kind="connection")

    embed = OpenAICompatTransport.embed
    stream_chat = OpenAICompatTransport.stream_chat
    list_models = OpenAICompatTransport.list_models

    async def guarded_embed(
        self: OpenAICompatTransport,
        *,
        api_key: str,
        model: str,
        inputs: Sequence[str],
        dimensions: int | None,
    ) -> list[list[float]]:
        error = refuse(self, "embed")
        if error is not None:
            raise error
        return await embed(self, api_key=api_key, model=model, inputs=inputs, dimensions=dimensions)

    async def guarded_stream(
        self: OpenAICompatTransport, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        error = refuse(self, "stream_chat")
        if error is not None:
            raise error
        async for chunk in stream_chat(self, api_key=api_key, body=body):
            yield chunk

    async def guarded_models(self: OpenAICompatTransport, *, api_key: str) -> list[str]:
        error = refuse(self, "list_models")
        if error is not None:
            raise error
        return await list_models(self, api_key=api_key)

    monkeypatch.setattr(OpenAICompatTransport, "embed", guarded_embed)
    monkeypatch.setattr(OpenAICompatTransport, "stream_chat", guarded_stream)
    monkeypatch.setattr(OpenAICompatTransport, "list_models", guarded_models)
    yield
    assert not attempts, f"live가 아닌 시험이 실제 Gemini를 부르려 했다: {attempts}"


def build_skill_zip(
    name: str = "csv-tools",
    description: str = "CSV 파일을 읽고 요약하는 도구",
    *,
    top: str | None = "csv-tools",
    files: dict[str, bytes | str] | None = None,
    skill_md: str | None = None,
) -> bytes:
    """SKILL.md(프런트매터 + 본문)와 files를 담은 스킬 zip. top이 None이면 루트에 둔다."""
    import io
    import zipfile

    prefix = f"{top}/" if top else ""
    if skill_md is None:
        skill_md = f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n사용법\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "SKILL.md", skill_md)
        for path, data in (files or {}).items():
            archive.writestr(prefix + path, data)
    return buffer.getvalue()


@pytest.fixture
def skill_zip() -> Callable[..., bytes]:
    # 테스트 모듈은 conftest를 import할 수 없어(--import-mode=importlib) 픽스처로 넘긴다.
    return build_skill_zip
