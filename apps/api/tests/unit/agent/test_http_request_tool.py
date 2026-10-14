"""모델이 http_request에 넣은 자격 증명 헤더가 기록과 승인 카드에서 가려지는지(S8, 설계 3.9)."""

import json
import secrets
from collections.abc import Callable
from typing import Any

import pytest

from app.agent.interactions import ResumePayload
from app.agent.tools.http_request import HttpRequestTool
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.schemas.events import TRANSIENT_EVENT_TYPES

Harness = Callable[..., Any]
CREDENTIAL_HEADERS = ("Authorization", "Cookie", "X-Api-Key", "Proxy-Authorization")


def model_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Cookie": f"sid={token}",
        "X-Api-Key": token,
        "Proxy-Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


async def test_credentials_are_masked_in_events_and_the_approval_card(harness: Harness) -> None:
    # 모델이 파일이나 웹에서 읽은 토큰을 헤더에 넣는 경우다. 값은 실행할 때 만든 가짜다.
    token = "tok" + secrets.token_hex(12)
    headers = model_headers(token)
    run = harness(
        [
            [
                *fake_tool_call(
                    "http_request",
                    {"method": "POST", "url": "https://pypi.org/x", "headers": headers},
                    call_id="call_post",
                ),
                fake_end("tool_use"),
            ],
            [*fake_text("보냈다"), fake_end()],
        ],
        approval_mode="http_writes",
    )

    paused = await run.run()

    assert paused.status == "waiting_input" and paused.pending is not None
    started = run.ports.of("tool_call.started")[0]
    assert started == {"tool_call_id": "call_post", "name": "http_request"}
    expected = {name: "***" for name in CREDENTIAL_HEADERS} | {"Accept": "application/json"}
    assert run.ports.of("tool_call.ready")[0]["input"]["headers"] == expected
    card = run.ports.of("run.waiting_input")[0]
    assert card["kind"] == "approval"
    assert card["input"]["headers"] == expected
    # run.pending_interaction 열에 저장되는 값도 같다.
    assert paused.pending.input is not None
    assert paused.pending.input["headers"] == expected

    done = await run.resume(
        paused,
        ResumePayload(
            interaction_id=paused.pending.interaction_id, kind="approval", decision="approve"
        ),
    )

    assert done.status == "succeeded"
    # 가리는 것은 기록뿐이고 요청에는 모델이 준 값이 그대로 나간다.
    assert run.backend.http[0].headers == headers
    persisted = [
        data for event_type, data in run.ports.events if event_type not in TRANSIENT_EVENT_TYPES
    ]
    assert token not in json.dumps(persisted, ensure_ascii=False)


@pytest.mark.parametrize(
    "name", ["authorization", "AUTHORIZATION", "cookie", "x-api-key", "PROXY-AUTHORIZATION"]
)
def test_header_names_are_matched_without_case(name: str) -> None:
    shown = HttpRequestTool().display_input(
        {"method": "GET", "url": "https://pypi.org/", "headers": {name: "value", "Accept": "*/*"}}
    )

    assert shown["headers"] == {name: "***", "Accept": "*/*"}
