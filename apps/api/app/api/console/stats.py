from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.auth.dependencies import CurrentAuth, DbDep
from app.schemas.usage import UsageGroupBy, UsageReport
from app.services.usage import UsageService

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/usage", response_model=UsageReport)
async def usage_stats(
    auth: CurrentAuth,
    db: DbDep,
    since: Annotated[
        datetime | date | None,
        Query(alias="from", description="이 시각부터(포함). 비우면 to의 30일 전"),
    ] = None,
    until: Annotated[
        datetime | date | None, Query(alias="to", description="이 시각 전까지. 비우면 지금")
    ] = None,
    group_by: UsageGroupBy = "day",
    tz_offset_minutes: Annotated[
        int | None,
        Query(
            ge=-14 * 60,
            le=14 * 60,
            description="day로 묶을 날짜의 시간대(UTC와의 차이, 분). 비우면 서버 PC의 시간대",
        ),
    ] = None,
) -> UsageReport:
    """토큰·비용·런 수 집계. 런을 만든 시각(queued_at)이 [from, to)에 드는 런의 usage를 더한다.

    시간대 없이 온 from·to는 tz_offset_minutes 시간대의 시각으로, 날짜만 오면 그날 0시로 본다.
    """
    return await UsageService(db).report(
        auth.workspace_id,
        group_by=group_by,
        since=since,
        until=until,
        tz_offset_minutes=tz_offset_minutes,
    )
