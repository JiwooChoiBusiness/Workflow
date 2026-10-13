"""사용량 집계 SQL(설계 6.2 GET /stats/usage).

런 하나가 한 번만 세어지도록 run.usage(런 전체 합계)를 더한다. 스텝별 usage 이벤트는
보존 기간이 지나면 지워지므로 쓰지 않는다. JSON 값은 집계 대상에만 쓰고 조건에는 쓰지 않는다
(5장 공통 규칙).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Float, Integer, Select, String, and_, case, func

from app.db.models import Agent, ChatSession, Run
from app.db.repositories.base import WorkspaceScopedRepository

UsageGroupBy = Literal["day", "agent", "model"]


def _usage_int(field: str) -> ColumnElement[int]:
    return func.coalesce(func.json_extract(Run.usage, f"$.{field}", type_=Integer), 0)


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    key: str | None
    label: str | None
    deleted: bool
    runs: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    unpriced_runs: int
    estimated: bool


class UsageRepository(WorkspaceScopedRepository[Run]):
    model = Run

    async def aggregate(
        self,
        *,
        group_by: UsageGroupBy,
        since: datetime,
        until: datetime,
        offset_minutes: int,
    ) -> list[UsageAggregate]:
        """[since, until)에 만든(queued_at) 런을 묶어 더한다.

        day는 offset_minutes 시간대의 날짜다.
        """
        input_tokens = _usage_int("input_tokens")
        output_tokens = _usage_int("output_tokens")
        cost = func.json_extract(Run.usage, "$.cost_usd", type_=Float)
        measures: list[ColumnElement[Any]] = [
            func.count(Run.id).label("runs"),
            func.sum(input_tokens).label("input_tokens"),
            func.sum(output_tokens).label("output_tokens"),
            func.sum(_usage_int("reasoning_tokens")).label("reasoning_tokens"),
            func.sum(_usage_int("cached_input_tokens")).label("cached_input_tokens"),
            # SUM은 NULL을 건너뛰고 전부 NULL이면 NULL이다. 가격이 없는 런은 비용 합에서 빠진다.
            func.sum(cost).label("cost_usd"),
            func.sum(
                case((and_(cost.is_(None), input_tokens + output_tokens > 0), 1), else_=0)
            ).label("unpriced_runs"),
            func.max(_usage_int("estimated")).label("estimated"),
        ]
        in_range = and_(Run.queued_at >= since, Run.queued_at < until)
        tokens = func.sum(input_tokens + output_tokens)
        statement: Select[Any]
        if group_by == "day":
            # 저장 형식(…ffffffZ)을 SQLite 날짜 함수가 읽고 수정자로 로컬 날짜로 옮긴다. 소수 초는
            # 떼어 낸다. SQLite가 밀리초로 반올림해 자정 직전 값이 다음 날로 넘어가지 않게 한다.
            stamp = func.substr(Run.queued_at, 1, 19, type_=String)
            day = func.date(stamp, f"{offset_minutes:+d} minutes", type_=String)
            statement = (
                self._select(day.label("key"), *measures)
                .where(in_range)
                .group_by(day)
                .order_by(day)
            )
        elif group_by == "model":
            # 런이 마지막으로 쓴 모델. 모델을 부르기 전에 끝난 런은 요청한 모델, 그것도 없으면 NULL.
            model = func.coalesce(
                func.json_extract(Run.model, "$.used", type_=String),
                func.json_extract(Run.model, "$.model_id", type_=String),
                type_=String,
            )
            statement = (
                self._select(model.label("key"), *measures)
                .where(in_range)
                .group_by(model)
                .order_by(tokens.desc(), model)
            )
        else:
            statement = (
                self._select(
                    ChatSession.agent_id.label("key"),
                    Agent.name.label("label"),
                    Agent.deleted_at.label("deleted_at"),
                    *measures,
                )
                .select_from(Run)
                .join(ChatSession, ChatSession.id == Run.session_id)
                .join(Agent, Agent.id == ChatSession.agent_id)
                .where(in_range, ChatSession.workspace_id == self.workspace_id)
                .group_by(ChatSession.agent_id, Agent.name, Agent.deleted_at)
                .order_by(tokens.desc(), Agent.name)
            )
        result = await self._execute(statement)
        aggregates: list[UsageAggregate] = []
        for row in result.mappings():
            key = row["key"]
            aggregates.append(
                UsageAggregate(
                    key=str(key) if isinstance(key, uuid.UUID) else key,
                    label=row.get("label"),
                    deleted=row.get("deleted_at") is not None,
                    runs=int(row["runs"]),
                    input_tokens=int(row["input_tokens"] or 0),
                    output_tokens=int(row["output_tokens"] or 0),
                    reasoning_tokens=int(row["reasoning_tokens"] or 0),
                    cached_input_tokens=int(row["cached_input_tokens"] or 0),
                    cost_usd=None if row["cost_usd"] is None else float(row["cost_usd"]),
                    unpriced_runs=int(row["unpriced_runs"] or 0),
                    estimated=bool(row["estimated"]),
                )
            )
        return aggregates
