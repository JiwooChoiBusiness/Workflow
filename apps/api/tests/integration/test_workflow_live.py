"""워크플로우 모델 노드의 실제 Gemini 호출(2단계 10장 M13 검증).

llm → question_classifier → parameter_extractor를 한 런으로 돌린다. 키는 앱 설정(.env의
GEMINI_API_KEYS)으로만 받고 없으면 건너뛴다. 기본 실행에서 빠지고 `uv run pytest -m live`로만 돈다.
"""

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.config import REPO_ROOT, Settings, get_settings
from app.llm.gemini_adapter import GeminiAdapter
from app.runner.supervisor import CancelSignal
from app.schemas.workflow import Graph
from app.workflow.engine import Engine, RunContext, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.ports import NodeSettings

pytestmark = pytest.mark.live

LOW = {"params": {"max_output_tokens": 2048, "reasoning_effort": "low"}}


class Ports:
    sandbox = None
    http = None
    files = None
    tasks = None

    def __init__(self, model: GeminiAdapter) -> None:
        self.model = model
        self.node_settings = NodeSettings()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        return None


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


def node(node_id: str, node_type: str, config: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "title": node_id, "config": config}


GRAPH = Graph.model_validate(
    {
        "nodes": [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            node(
                "llm_1",
                "llm",
                {
                    "model": LOW,
                    "prompt": [
                        {"role": "system", "text": "한국어 한 문장으로만 답한다."},
                        {"role": "user", "text": "{{ start.q }}"},
                    ],
                },
            ),
            node(
                "question_classifier_1",
                "question_classifier",
                {
                    "model": LOW,
                    "query_var": "start.q",
                    "classes": [
                        {"id": "weather", "name": "날씨", "description": "날씨·기온·비를 묻는 글"},
                        {"id": "other", "name": "기타", "description": "그 밖의 글"},
                    ],
                },
            ),
            node(
                "parameter_extractor_1",
                "parameter_extractor",
                {
                    "model": LOW,
                    "query_var": "start.q",
                    "parameters": [
                        {
                            "name": "city",
                            "type": "string",
                            "description": "도시 이름(한국어)",
                            "required": True,
                        },
                        {"name": "days", "type": "number", "description": "오늘부터 며칠 뒤인지"},
                    ],
                },
            ),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "answer", "var": "llm_1.text"},
                        {"name": "class_id", "var": "question_classifier_1.class_id"},
                        {"name": "city", "var": "parameter_extractor_1.city"},
                        {"name": "days", "var": "parameter_extractor_1.days"},
                        {"name": "ok", "var": "parameter_extractor_1.__is_success"},
                    ]
                },
            ),
            node("end_other", "end", {"outputs": [{"name": "class_id", "var": "llm_1.text"}]}),
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "llm_1", "target": "question_classifier_1"},
            {
                "id": "e3",
                "source": "question_classifier_1",
                "source_handle": "class_weather",
                "target": "parameter_extractor_1",
            },
            {"id": "e4", "source": "parameter_extractor_1", "target": "end_1"},
            {
                "id": "e5",
                "source": "question_classifier_1",
                "source_handle": "class_other",
                "target": "end_other",
            },
        ],
    }
)


async def test_llm_classifier_and_extractor_on_gemini(adapter: GeminiAdapter) -> None:
    context = RunContext(
        run_id=uuid.uuid4(),
        graph=GRAPH,
        kind="workflow",
        ports=Ports(adapter),
        cancel=CancelSignal(),
        inputs={"q": "사흘 뒤 부산에 비가 올까?"},
    )
    events: list[WfEvent] = []
    run = Engine().run(context)
    async with run:
        async for event in run:
            events.append(event)
    outcome = run.outcome

    if outcome.error is not None and outcome.error.code == "model_unavailable":
        pytest.skip(f"Gemini가 일시적으로 응답하지 않는다: {outcome.error.message[:200]}")
    assert outcome.status == "succeeded", outcome.error
    deltas = [e.data["text"] for e in events if e.type == "node.text_delta"]
    assert deltas and "".join(deltas) == outcome.outputs["answer"]
    usages = [e.data for e in events if e.type == "node.usage"]
    assert {u["node_id"] for u in usages} == {
        "llm_1",
        "question_classifier_1",
        "parameter_extractor_1",
    }
    assert all(u["input_tokens"] > 0 and not u["estimated"] for u in usages)
    assert outcome.usage.input_tokens == sum(u["input_tokens"] for u in usages)
    assert outcome.outputs["class_id"] == "weather"
    assert outcome.outputs["ok"] is True and "부산" in outcome.outputs["city"]
    assert outcome.outputs["days"] == 3
