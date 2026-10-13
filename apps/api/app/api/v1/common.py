"""서비스 API 공통: 키가 다룰 수 있는 세션 범위, 서비스 조립, 커서(설계 6.4, 11장 S11).

키는 에이전트 하나에 묶이고 그 에이전트의 api 세션(대화)과 task 세션(과업)만 다룬다. chat 권한은
api 세션, tasks 권한은 task 세션을 본다. user를 주면 그 외부 사용자의 세션만 보인다.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.api.console.runs import get_run_service
from app.api.errors import ApiError
from app.auth.dependencies import DbDep, SettingsDep
from app.db.models.enums import SessionSource
from app.services.access import SessionScope
from app.services.agents import AgentService
from app.services.api_keys import AuthenticatedApiKey
from app.services.runs import RunService
from app.services.tasks import TaskService

SCOPE_SOURCES = {"chat": SessionSource.API, "tasks": SessionSource.TASK}
# 파일은 대화에 올려 과업 입력으로 쓰기도 하므로 두 소스를 함께 본다.
FILE_SOURCES = frozenset({SessionSource.API, SessionSource.TASK})


def key_scope(
    key: AuthenticatedApiKey, *, user: str | None = None, only: str | None = None
) -> SessionScope:
    """키의 권한(scopes)이 허용하는 세션. only를 주면 그 소스(api 또는 task)로 좁힌다."""
    sources = frozenset(SCOPE_SOURCES[name] for name in key.scopes if name in SCOPE_SOURCES)
    if only is not None:
        sources &= {only}
    return SessionScope(sources=sources, agent_id=key.agent_id, external_user=user)


def file_scope(key: AuthenticatedApiKey, *, user: str | None = None) -> SessionScope:
    return SessionScope(sources=FILE_SOURCES, agent_id=key.agent_id, external_user=user)


def parse_cursor(cursor: str | None) -> int | None:
    if cursor is None or cursor == "":
        return None
    if not cursor.isdigit():
        raise ApiError(422, "cursor_invalid", "cursor는 앞 응답의 next_cursor 값을 그대로 쓴다")
    return int(cursor)


def get_agent_service(db: DbDep, settings: SettingsDep) -> AgentService:
    return AgentService(db, settings)


def get_task_service(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    runs: Annotated[RunService, Depends(get_run_service)],
) -> TaskService:
    return TaskService(db, settings, runs=runs, bus=request.app.state.bus)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
