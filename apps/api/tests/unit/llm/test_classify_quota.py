"""429 본문의 quotaId·retryDelay로 분당·일일 한도를 가르는 공용 분류(3단계 설계 Z2, 6.6)."""

from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from app.llm.errors import ErrorClass, TransportError, classify, seconds_until_daily_reset
from app.llm.gemini_adapter import OpenAICompatTransport
from app.llm.gemini_adapter import TransportError as ReexportedTransportError

MINUTE = "EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier"
DAY = "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier"


def quota_error(*quota_ids: str, retry_delay: object = None, status: int = 429) -> dict[str, Any]:
    """Gemini 429 본문의 error 객체(2026-11-02 실측 모양)."""
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.Help",
            "links": [{"description": "Learn more", "url": "https://ai.google.dev/"}],
        }
    ]
    if quota_ids:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/embed_content_requests",
                        "quotaId": quota_id,
                        "quotaDimensions": {"location": "global", "model": "gemini-embedding-2"},
                        "quotaValue": "1000" if "PerDay" in quota_id else "100",
                    }
                    for quota_id in quota_ids
                ],
            }
        )
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return {
        "code": status,
        "message": "You exceeded your current quota.",
        "status": "RESOURCE_EXHAUSTED",
        "details": details,
    }


def rate_limited(payload: dict[str, Any] | None) -> TransportError:
    return TransportError("RESOURCE_EXHAUSTED", status=429, payload=payload)


def test_transport_error_keeps_its_old_import_path_and_fields() -> None:
    error = TransportError("boom", status=503, kind="stream", payload={"code": 503})

    assert ReexportedTransportError is TransportError
    assert (error.message, error.status, error.kind, error.payload) == (
        "boom",
        503,
        "stream",
        {"code": 503},
    )
    assert TransportError("x").payload is None
    assert str(error) == "boom"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (None, "transient"),
        (500, "transient"),
        (503, "transient"),
        (401, "auth"),
        (403, "auth"),
        (400, "invalid"),
        (404, "invalid"),
    ],
)
def test_status_rules_are_unchanged(status: int | None, kind: str) -> None:
    verdict = classify(TransportError("e", status=status, kind="http" if status else "connection"))

    assert verdict == ErrorClass(kind)  # pyright: ignore[reportArgumentType]
    assert verdict.scope is None


def test_minute_quota_from_the_measured_body() -> None:
    verdict = classify(rate_limited(quota_error(MINUTE, retry_delay="45s")))

    assert verdict == ErrorClass("rate_limited", scope="minute", retry_after=45.0, quota_id=MINUTE)


def test_daily_quota_from_the_measured_body() -> None:
    verdict = classify(rate_limited(quota_error(DAY, retry_delay="17437s")))

    assert verdict == ErrorClass("rate_limited", scope="day", retry_after=17437.0, quota_id=DAY)


def test_daily_wins_when_both_limits_are_reported() -> None:
    verdict = classify(rate_limited(quota_error(MINUTE, DAY, retry_delay="30s")))

    assert (verdict.scope, verdict.quota_id) == ("day", DAY)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"},
        {"details": "not a list"},
        {"details": [{"violations": [{"quotaId": 7}]}, "junk"]},
    ],
)
def test_429_without_a_quota_id_is_unknown(payload: dict[str, Any] | None) -> None:
    verdict = classify(rate_limited(payload))

    assert verdict == ErrorClass("rate_limited", scope="unknown")


def test_unrecognised_quota_id_is_unknown_but_kept() -> None:
    other = "GenerateRequestsPerProjectPerModel-FreeTier"

    verdict = classify(rate_limited(quota_error(other)))

    assert (verdict.scope, verdict.quota_id, verdict.retry_after) == ("unknown", other, None)


@pytest.mark.parametrize(
    ("delay", "expected"),
    [
        ("45s", 45.0),
        ("0.5s", 0.5),
        (" 3s ", 3.0),
        ({"seconds": "12", "nanos": 500_000_000}, 12.5),
        ("45", None),
        ("soon", None),
        (12, None),
    ],
)
def test_retry_delay_formats(delay: object, expected: float | None) -> None:
    verdict = classify(rate_limited(quota_error(MINUTE, retry_delay=delay)))

    assert verdict.retry_after == expected


def test_retry_delay_is_read_on_server_errors_too() -> None:
    error = TransportError(
        "UNAVAILABLE", status=503, payload=quota_error(retry_delay="7s", status=503)
    )

    assert classify(error) == ErrorClass("transient", retry_after=7.0)


@pytest.mark.parametrize(
    ("now", "expected_hours"),
    [
        # 표준시(PST): 태평양 자정은 08:00 UTC.
        (datetime(2026, 1, 15, 10, 0, tzinfo=UTC), 22),
        (datetime(2026, 1, 15, 7, 0, tzinfo=UTC), 1),
        # 서머타임(PDT): 07:00 UTC.
        (datetime(2026, 7, 1, 6, 0, tzinfo=UTC), 1),
        (datetime(2026, 7, 1, 7, 0, tzinfo=UTC), 24),
        # 2026-03-08(일) 02:00에 PDT로 바뀐다. 그날 자정은 아직 PST, 다음 날 자정은 PDT.
        (datetime(2026, 3, 8, 7, 30, tzinfo=UTC), 0.5),
        (datetime(2026, 3, 8, 9, 0, tzinfo=UTC), 22),
        # 2026-11-01(일) 02:00에 PST로 돌아간다. 그날 자정은 아직 PDT, 다음 날 자정은 PST.
        (datetime(2026, 11, 1, 6, 0, tzinfo=UTC), 1),
        (datetime(2026, 11, 1, 8, 0, tzinfo=UTC), 24),
    ],
)
def test_seconds_until_pacific_midnight(now: datetime, expected_hours: float) -> None:
    assert seconds_until_daily_reset(now) == expected_hours * 3600


def sdk_transport(handler: Any) -> OpenAICompatTransport:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAICompatTransport("https://example.test/v1beta/openai", http_client=client)


@pytest.mark.parametrize("wrapped", [True, False])
async def test_sdk_errors_carry_the_error_object_as_payload(wrapped: bool) -> None:
    # 호환 레이어는 오류 본문을 리스트로 감싸 보낸다. 감싸지 않은 본문도 같은 payload가 된다.
    error_object = quota_error(DAY, retry_delay="17437s")
    body: object = [{"error": error_object}] if wrapped else {"error": error_object}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json=body)

    transport = sdk_transport(handler)

    with pytest.raises(TransportError) as chat:
        async for _ in transport.stream_chat(api_key="test-key-0000", body={"model": "m"}):
            pass
    with pytest.raises(TransportError) as embed:
        await transport.embed(api_key="test-key-0000", model="m", inputs=["a"], dimensions=None)
    await transport.aclose()

    for caught in (chat, embed):
        assert caught.value.payload == error_object
        assert classify(caught.value).scope == "day"
