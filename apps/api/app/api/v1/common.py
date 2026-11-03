"""서비스 API 공통: 키 대상, 키가 다룰 수 있는 범위, 서비스 조립, 커서(설계 6.4, 11장 S11,
2단계 설계 6.3).

에이전트 키는 그 에이전트의 api 세션(대화)과 task 세션(과업)만 다룬다. chat 권한은 api 세션, tasks
권한은 task 세션을 본다. 워크플로우 키(workflow·chatflow)는 그 키로 만든 워크플로우 런과 챗플로우
대화만 다룬다. user를 주면 그 외부 사용자의 것만 보인다. 지식 베이스 키는 datasets 경로에서 그 KB
하나만 다룬다(3단계 8.5). 경로가 받지 않는 대상의 키는 400 wrong_target이다.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Request

from app.api.console.runs import get_run_service
from app.api.errors import ApiError
from app.auth.dependencies import DbDep, SettingsDep
from app.db.models.enums import SessionSource, WfConversationSource
from app.services.access import DatasetScope, FlowScope, SessionScope, dataset_in_scope
from app.services.agents import AgentService
from app.services.api_key_target import ApiTarget, wrong_target
from app.services.api_keys import AuthenticatedApiKey
from app.services.runs import RunService
from app.services.tasks import TaskService

SCOPE_SOURCES = {"chat": SessionSource.API, "tasks": SessionSource.TASK}
# 파일은 대화에 올려 과업 입력으로 쓰기도 하므로 두 소스를 함께 본다.
FILE_SOURCES = frozenset({SessionSource.API, SessionSource.TASK})


def agent_of(key: AuthenticatedApiKey) -> uuid.UUID:
    """에이전트 키의 에이전트. 워크플로우 키로 에이전트 경로를 부르면 400 wrong_target이다."""
    if key.agent_id is None:
        raise wrong_target(key.api_target, "agent")
    return key.agent_id


def flow_of(key: AuthenticatedApiKey, *targets: ApiTarget) -> uuid.UUID:
    """워크플로우 키의 워크플로우. 키 대상이 targets에 없으면 400 wrong_target이다."""
    if key.workflow_id is None or key.api_target not in targets:
        raise wrong_target(key.api_target, *targets)
    return key.workflow_id


def dataset_of(key: AuthenticatedApiKey, dataset_id: uuid.UUID | None = None) -> uuid.UUID:
    """지식 베이스 키가 다룰 수 있는 KB. 다른 대상의 키는 400 wrong_target, 다른 KB id는 404다.

    dataset_id를 비우면 키에 묶인 KB다.
    """
    if key.knowledge_base_id is None:
        raise wrong_target(key.api_target, "knowledge")
    if "knowledge" not in key.scopes:
        raise ApiError(403, "scope_not_allowed", "이 API 키에는 knowledge 권한이 없다")
    scope = DatasetScope(key.knowledge_base_id)
    return dataset_in_scope(scope, dataset_id if dataset_id is not None else key.knowledge_base_id)


def flow_scope(key: AuthenticatedApiKey, *, user: str | None = None) -> FlowScope:
    """워크플로우 키가 다룰 수 있는 런·대화. 그 키로 만든 것만이다."""
    return FlowScope(
        sources=frozenset({WfConversationSource.API}), api_key_id=key.key_id, external_user=user
    )


def key_scope(
    key: AuthenticatedApiKey, *, user: str | None = None, only: str | None = None
) -> SessionScope:
    """키의 권한(scopes)이 허용하는 세션. only를 주면 그 소스(api 또는 task)로 좁힌다."""
    agent_id = agent_of(key)
    sources = frozenset(SCOPE_SOURCES[name] for name in key.scopes if name in SCOPE_SOURCES)
    if only is not None:
        sources &= {only}
    return SessionScope(sources=sources, agent_id=agent_id, external_user=user)


def file_scope(key: AuthenticatedApiKey, *, user: str | None = None) -> SessionScope:
    return SessionScope(sources=FILE_SOURCES, agent_id=agent_of(key), external_user=user)


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
