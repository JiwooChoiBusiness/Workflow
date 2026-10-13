"""토큰·비용·런 수 집계(설계 6.2 GET /stats/usage, 7.3 설정의 사용량 표).

런 단위로 run.usage를 더하므로 어떻게 묶든 합계는 같은 기간 런들의 usage 합과 같다. 모델별은 런이
마지막으로 쓴 모델(run.model.used)로 센다. 한 런 안에서 폴백으로 모델이 바뀌어도 런 전체가 마지막
모델에 들어간다. 지운 세션·에이전트의 런도 사용량이므로 센다.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone

from app.db.engine import Database
from app.db.repositories.usage import UsageAggregate, UsageRepository
from app.db.types import utcnow
from app.schemas.usage import UsageGroupBy, UsageReport, UsageRow, UsageTotals
from app.services.errors import ServiceError

DEFAULT_RANGE = timedelta(days=30)
MAX_RANGE = timedelta(days=3660)


def local_offset_minutes() -> int:
    """서버 PC의 지금 시간대(UTC와의 차이, 분).

    화면과 서버가 같은 PC라 날짜 경계가 화면과 맞는다.
    """
    offset = datetime.now().astimezone().utcoffset() or timedelta()
    return int(offset.total_seconds() // 60)


def _aware(value: datetime | date | None, tz: timezone) -> datetime | None:
    # 날짜만 오면 그날 0시, 시간대 없이 온 시각은 집계 시간대의 시각으로 본다.
    if value is None:
        return None
    if not isinstance(value, datetime):
        return datetime.combine(value, time(), tz)
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=tz)


def sum_totals(rows: Sequence[UsageTotals]) -> UsageTotals:
    costs = [row.cost_usd for row in rows if row.cost_usd is not None]
    return UsageTotals(
        runs=sum(row.runs for row in rows),
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
        reasoning_tokens=sum(row.reasoning_tokens for row in rows),
        cached_input_tokens=sum(row.cached_input_tokens for row in rows),
        cost_usd=sum(costs) if costs else None,
        unpriced_runs=sum(row.unpriced_runs for row in rows),
        estimated=any(row.estimated for row in rows),
    )


def _row(item: UsageAggregate) -> UsageRow:
    return UsageRow(
        key=item.key,
        label=item.label,
        deleted=item.deleted,
        runs=item.runs,
        input_tokens=item.input_tokens,
        output_tokens=item.output_tokens,
        reasoning_tokens=item.reasoning_tokens,
        cached_input_tokens=item.cached_input_tokens,
        cost_usd=item.cost_usd,
        unpriced_runs=item.unpriced_runs,
        estimated=item.estimated,
    )


class UsageService:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utcnow,
        local_offset: Callable[[], int] = local_offset_minutes,
    ) -> None:
        self._db = db
        self._clock = clock
        self._local_offset = local_offset

    async def report(
        self,
        workspace_id: uuid.UUID,
        *,
        group_by: UsageGroupBy = "day",
        since: datetime | date | None = None,
        until: datetime | date | None = None,
        tz_offset_minutes: int | None = None,
    ) -> UsageReport:
        offset = self._local_offset() if tz_offset_minutes is None else tz_offset_minutes
        tz = timezone(timedelta(minutes=offset))
        end = _aware(until, tz) or self._clock()
        start = _aware(since, tz) or end - DEFAULT_RANGE
        if start >= end:
            raise ServiceError(422, "invalid_range", "from이 to보다 앞이어야 한다")
        if end - start > MAX_RANGE:
            raise ServiceError(422, "invalid_range", "기간은 10년을 넘을 수 없다")
        async with self._db.read() as session:
            aggregates = await UsageRepository(session, workspace_id).aggregate(
                group_by=group_by, since=start, until=end, offset_minutes=offset
            )
        items = [_row(item) for item in aggregates]
        return UsageReport(
            group_by=group_by,
            since=start,
            until=end,
            tz_offset_minutes=offset,
            items=items,
            total=sum_totals(items),
        )
