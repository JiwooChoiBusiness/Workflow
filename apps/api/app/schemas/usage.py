from datetime import datetime
from typing import Literal

from pydantic import BaseModel

UsageGroupBy = Literal["day", "agent", "model"]


class UsageTotals(BaseModel):
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    # 비용이 기록된 런의 합(USD). 비용은 런을 돌릴 때 가격표(workspace.settings.model_prices)에 그
    # 모델이 있었을 때만 남는다. 그런 런이 하나도 없으면 null.
    cost_usd: float | None = None
    # 토큰을 썼는데 비용이 없는 런의 수. cost_usd에 들어 있지 않다.
    unpriced_runs: int = 0
    # 토큰 수가 추정치(usage.estimated)인 런이 섞여 있다.
    estimated: bool = False


class UsageRow(UsageTotals):
    # day: 날짜(YYYY-MM-DD, tz_offset_minutes 기준), agent: 에이전트 id,
    # model: 런이 마지막으로 쓴 모델(모델을 부르기 전에 끝나 알 수 없으면 null).
    key: str | None
    # agent일 때 에이전트 이름. 나머지는 null.
    label: str | None = None
    # agent일 때 지운 에이전트인지.
    deleted: bool = False


class UsageReport(BaseModel):
    group_by: UsageGroupBy
    # 실제로 쓴 기간 [since, until). 요청의 from·to를 비우면 최근 30일이다.
    since: datetime
    until: datetime
    # day로 묶을 때 쓴 시간대(UTC와의 차이, 분).
    tz_offset_minutes: int
    # day는 날짜 순, agent·model은 토큰(입력+출력)이 많은 순이다. 런이 없는 날은 없다.
    items: list[UsageRow]
    total: UsageTotals
