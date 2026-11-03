"""모델 API 오류의 분류(3단계 설계 Z2, 6.6). 채팅 어댑터와 임베딩 클라이언트가 같이 쓴다.

Gemini의 429는 모두 한도(quota) 응답이다. 본문 error.details의 QuotaFailure.violations[].quotaId로
분당·일일 한도를 가르고 RetryInfo.retryDelay로 기다릴 시간을 읽는다.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast

ErrorKind = Literal["rate_limited", "transient", "auth", "invalid"]
QuotaScope = Literal["minute", "day", "unknown"]
TransportKind = Literal["http", "connection", "timeout", "stream"]

# protobuf Duration의 JSON 표기("45s", "17437s", "0.5s").
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


class TransportError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        kind: TransportKind = "http",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind
        # 오류 JSON의 error 객체 원문(code, message, status, details). 키 값이 섞일 수 있어 로그에
        # 그대로 남기지 않는다.
        self.payload = payload


@dataclass(frozen=True)
class ErrorClass:
    kind: ErrorKind
    # rate_limited일 때만 값이 있다.
    scope: QuotaScope | None = None
    # RetryInfo.retryDelay(초). 없으면 None.
    retry_after: float | None = None
    quota_id: str | None = None


def classify(error: TransportError) -> ErrorClass:
    retry_after = _retry_delay(error.payload)
    status = error.status
    if status is None:
        return ErrorClass("transient", retry_after=retry_after)
    if status == 429:
        quota_id, scope = _quota(error.payload)
        return ErrorClass("rate_limited", scope=scope, retry_after=retry_after, quota_id=quota_id)
    if status >= 500:
        return ErrorClass("transient", retry_after=retry_after)
    if status in (401, 403):
        return ErrorClass("auth")
    return ErrorClass("invalid")


def _details(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = payload.get("details") if payload is not None else None
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[Any], raw) if isinstance(item, dict)]


def _quota(payload: dict[str, Any] | None) -> tuple[str | None, QuotaScope]:
    ids: list[str] = []
    for detail in _details(payload):
        violations = detail.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in cast(list[Any], violations):
            if isinstance(violation, dict):
                quota_id = cast(dict[str, Any], violation).get("quotaId")
                if isinstance(quota_id, str) and quota_id:
                    ids.append(quota_id)
    # 분당과 일일이 한 응답에 같이 오면 일일이 앞선다. 몇 초 기다려도 풀리지 않는 쪽이다.
    for quota_id in ids:
        if "PerDay" in quota_id:
            return quota_id, "day"
    for quota_id in ids:
        if "PerMinute" in quota_id:
            return quota_id, "minute"
    return (ids[0] if ids else None), "unknown"


def _retry_delay(payload: dict[str, Any] | None) -> float | None:
    for detail in _details(payload):
        value = detail.get("retryDelay")
        if isinstance(value, str):
            match = _DURATION.match(value)
            if match:
                return float(match.group(1))
        elif isinstance(value, dict):
            parts = cast(dict[str, Any], value)
            seconds, nanos = parts.get("seconds", 0), parts.get("nanos", 0)
            if isinstance(seconds, int | str) and isinstance(nanos, int):
                try:
                    return max(float(seconds) + nanos / 1e9, 0.0)
                except ValueError:
                    return None
    return None


def _pacific_midnight_utc(day: date) -> datetime:
    # 일일 한도는 태평양 자정에 풀린다. 서버에 IANA 시간대 자료(tzdata)가 없어 미국 서머타임
    # 규칙(3월 둘째 일요일 ~ 11월 첫째 일요일)으로 직접 계산한다. 자정에는 바뀌는 날의 전 상태다.
    march = date(day.year, 3, 8)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7)
    november = date(day.year, 11, 1)
    dst_end = november + timedelta(days=(6 - november.weekday()) % 7)
    offset_hours = 7 if dst_start < day <= dst_end else 8
    return datetime.combine(day, time(), tzinfo=UTC) + timedelta(hours=offset_hours)


def seconds_until_daily_reset(now: datetime) -> float:
    """다음 태평양 자정까지 남은 초. 429 본문에 retryDelay가 없을 때 일일 한도를 막아 둘 시간."""
    current = now.astimezone(UTC)
    for offset in range(-1, 3):
        reset = _pacific_midnight_utc(current.date() + timedelta(days=offset))
        if reset > current:
            return (reset - current).total_seconds()
    raise AssertionError("태평양 자정은 하루 안에 온다")


def daily_block_seconds(retry_after: float | None, now: datetime) -> float:
    """일일 429를 막아 둘 초. retryDelay가 짧게 와도 다음 태평양 자정까지는 막는다.

    짧은 retryDelay를 믿으면 그 모델을 요청마다 다시 두드려 429와 폴백이 되풀이된다.
    """
    return max(seconds_until_daily_reset(now), retry_after or 0.0)
