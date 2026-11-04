"""에이전트 런이 일일 한도로 끝나면 RunOut.error에 retry_after가 남는다(3단계 12.4, M26 검토).

새로 고친 화면이 run.error 이벤트 없이 런 행만 읽어도 "N시 이후" 안내를 다시 그릴 수 있게 한다.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.types import ErrorEvent


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings.model_copy(update={"llm_fake": True})


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


async def test_a_daily_limit_failure_keeps_retry_after_on_the_run(
    logged_in: AsyncClient, script: Any
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "비서"})).json()
    session = (
        await logged_in.post(f"/api/console/agents/{agent['id']}/preview/sessions", json={})
    ).json()
    script.add(
        [
            ErrorEvent(
                code="model_unavailable", message="일일 한도", retryable=True, retry_after=3600.0
            )
        ]
    )

    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "안녕", "response_mode": "blocking"},
    )

    assert sent.status_code == 200, sent.text
    run = sent.json()["run"]
    assert run["status"] == "failed"
    assert run["error"]["code"] == "model_unavailable"
    assert run["error"]["retry_after"] == 3600.0
    detail = (await logged_in.get(f"/api/console/runs/{run['id']}")).json()
    assert detail["error"]["retry_after"] == 3600.0
