"""로그 화면의 런 목록(설계 6.2 GET /runs, 7.2 /logs).

런마다 세션 소스·제목과 에이전트 이름을 붙여 화면이 세션을 따로 읽지 않게 한다. 지운 세션·에이전트의
런도 기록으로 남아 있으므로 목록에 나온다. 기간은 사용량 집계와 같은 기준(queued_at)으로 거른다.
"""

import uuid
from datetime import datetime

from app.db.engine import Database
from app.db.repositories.runs import RunRepository
from app.schemas.logs import RunLogItem, RunLogList
from app.services.runs import run_out


def assume_local(value: datetime | None) -> datetime | None:
    """시간대 없이 온 시각(예: since=2026-10-13)을 서버 PC 시간대의 시각으로 본다.

    DB는 UTC로 비교하므로 시간대 없는 값을 그대로 넘기면 저장 계층이 받지 않는다.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.astimezone()


class RunLogService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_runs(
        self,
        workspace_id: uuid.UUID,
        *,
        session_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        source: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        parent_run_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RunLogList:
        """parent_run_id를 주면 그 런이 call_agent로 부른 자식 런만 나온다(오케스트레이션 6.2)."""
        async with self._db.read() as session:
            runs = RunRepository(session, workspace_id)
            rows = await runs.list_for_logs(
                session_id=session_id,
                agent_id=agent_id,
                source=source,
                status=status,
                since=since,
                until=until,
                parent_run_id=parent_run_id,
                limit=limit + 1,
                offset=offset,
            )
            children = await runs.count_children([run.id for run, _, _ in rows[:limit]])
        more = len(rows) > limit
        items = [
            RunLogItem(
                **run_out(run).model_dump(),
                agent_id=chat.agent_id,
                agent_name=agent.name,
                agent_deleted=agent.deleted_at is not None,
                session_source=chat.source,  # pyright: ignore[reportArgumentType]
                session_title=chat.title,
                session_deleted=chat.deleted_at is not None,
                child_count=children.get(run.id, 0),
            )
            for run, chat, agent in rows[:limit]
        ]
        return RunLogList(items=items, next_offset=offset + limit if more else None)
