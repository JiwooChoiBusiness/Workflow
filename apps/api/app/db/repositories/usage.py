"""사용량 집계 SQL(설계 6.2 GET /stats/usage, 2단계 3.10).

런 하나가 한 번만 세어지도록 런 전체 합계를 더한다. 에이전트 런은 run.usage, 워크플로우 런은
wf_run.usage다. agent 노드의 자식 런은 에이전트 런이라 run.usage로 세고 wf_run.usage에는 들어
있지 않으므로 이중 계산이 없다. 스텝·노드별 usage 이벤트는 보존 기간이 지나면 지워지므로 쓰지
않는다. JSON 값은 집계 대상에만 쓰고 조건에는 쓰지 않는다(5장 공통 규칙). 예외는 워크플로우 런의
모델을 고르는 상관 서브쿼리로, 런 하나의 노드 런 몇 개만 본다.

두 종류의 런은 같은 묶음 키(날짜, 모델)로 따로 더하고 서비스가 합친다. 에이전트로 묶을 때 워크플로우
런은, 워크플로우로 묶을 때 에이전트 런은 키가 없는(null) 한 행으로 모은다. 그래야 어떻게 묶든 합계가
같다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    Select,
    String,
    and_,
    case,
    func,
    null,
    select,
)
from sqlalchemy.orm import InstrumentedAttribute

from app.db.models import Agent, ChatSession, Run, WfNodeRun, WfRun, Workflow
from app.db.repositories.base import WorkspaceScopedRepository

UsageGroupBy = Literal["day", "agent", "model", "workflow"]
# 모델을 부르는 노드. 워크플로우 런은 이 노드들 가운데 마지막으로 모델을 부른 노드의 모델로 센다.
MODEL_NODE_TYPES = ("llm", "question_classifier", "parameter_extractor")


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


def _usage_int(usage: InstrumentedAttribute[Any], field: str) -> ColumnElement[int]:
    return func.coalesce(func.json_extract(usage, f"$.{field}", type_=Integer), 0)


def _measures(
    id_column: InstrumentedAttribute[Any], usage: InstrumentedAttribute[Any]
) -> list[ColumnElement[Any]]:
    input_tokens = _usage_int(usage, "input_tokens")
    output_tokens = _usage_int(usage, "output_tokens")
    cost = func.json_extract(usage, "$.cost_usd", type_=Float)
    return [
        func.count(id_column).label("runs"),
        func.sum(input_tokens).label("input_tokens"),
        func.sum(output_tokens).label("output_tokens"),
        func.sum(_usage_int(usage, "reasoning_tokens")).label("reasoning_tokens"),
        func.sum(_usage_int(usage, "cached_input_tokens")).label("cached_input_tokens"),
        # SUM은 NULL을 건너뛰고 전부 NULL이면 NULL이다. 가격이 없는 런은 비용 합에서 빠진다.
        func.sum(cost).label("cost_usd"),
        func.sum(case((and_(cost.is_(None), input_tokens + output_tokens > 0), 1), else_=0)).label(
            "unpriced_runs"
        ),
        func.max(_usage_int(usage, "estimated")).label("estimated"),
    ]


def _tokens(usage: InstrumentedAttribute[Any]) -> ColumnElement[Any]:
    return func.sum(_usage_int(usage, "input_tokens") + _usage_int(usage, "output_tokens"))


def _local_day(queued_at: InstrumentedAttribute[Any], offset_minutes: int) -> ColumnElement[str]:
    # 저장 형식(…ffffffZ)을 SQLite 날짜 함수가 읽고 수정자로 로컬 날짜로 옮긴다. 소수 초는
    # 떼어 낸다. SQLite가 밀리초로 반올림해 자정 직전 값이 다음 날로 넘어가지 않게 한다.
    stamp = func.substr(queued_at, 1, 19, type_=String)
    return func.date(stamp, f"{offset_minutes:+d} minutes", type_=String)


def _aggregates(rows: Any) -> list[UsageAggregate]:
    aggregates: list[UsageAggregate] = []
    for row in rows:
        if not row["runs"]:
            # 묶음 키 없이 모은 행(null 키)은 런이 없어도 한 행이 나온다.
            continue
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


class UsageRepository(WorkspaceScopedRepository[Run]):
    """에이전트 런(run.usage)."""

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
        measures = _measures(Run.id, Run.usage)
        in_range = and_(Run.queued_at >= since, Run.queued_at < until)
        tokens = _tokens(Run.usage)
        statement: Select[Any]
        if group_by == "day":
            day = _local_day(Run.queued_at, offset_minutes)
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
        elif group_by == "workflow":
            statement = self._select(null().label("key"), *measures).where(in_range)
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
        return _aggregates(result.mappings())


class WfUsageRepository(WorkspaceScopedRepository[WfRun]):
    """워크플로우 런(wf_run.usage). agent 노드의 사용량은 들어 있지 않다(3.10)."""

    model = WfRun

    async def aggregate(
        self,
        *,
        group_by: UsageGroupBy,
        since: datetime,
        until: datetime,
        offset_minutes: int,
    ) -> list[UsageAggregate]:
        measures = _measures(WfRun.id, WfRun.usage)
        in_range = and_(WfRun.queued_at >= since, WfRun.queued_at < until)
        tokens = _tokens(WfRun.usage)
        statement: Select[Any]
        if group_by == "day":
            day = _local_day(WfRun.queued_at, offset_minutes)
            statement = (
                self._select(day.label("key"), *measures)
                .where(in_range)
                .group_by(day)
                .order_by(day)
            )
        elif group_by == "model":
            # 에이전트 런처럼 런 전체를 마지막으로 쓴 모델에 넣는다. 모델을 부른 노드가 없으면 NULL.
            used = func.json_extract(WfNodeRun.process, "$.model", type_=String)
            last_model = (
                select(used)
                .where(
                    WfNodeRun.wf_run_id == WfRun.id,
                    WfNodeRun.node_type.in_(MODEL_NODE_TYPES),
                    used.is_not(None),
                )
                .order_by(WfNodeRun.index.desc())
                .limit(1)
                .correlate(WfRun)
                .scalar_subquery()
            )
            statement = (
                self._select(last_model.label("key"), *measures)
                .where(in_range)
                .group_by(last_model)
                .order_by(tokens.desc(), last_model)
            )
        elif group_by == "agent":
            statement = self._select(null().label("key"), *measures).where(in_range)
        else:
            statement = (
                self._select(
                    WfRun.workflow_id.label("key"),
                    Workflow.name.label("label"),
                    Workflow.deleted_at.label("deleted_at"),
                    *measures,
                )
                .select_from(WfRun)
                .join(Workflow, Workflow.id == WfRun.workflow_id)
                .where(in_range, Workflow.workspace_id == self.workspace_id)
                .group_by(WfRun.workflow_id, Workflow.name, Workflow.deleted_at)
                .order_by(tokens.desc(), Workflow.name)
            )
        result = await self._execute(statement)
        return _aggregates(result.mappings())
