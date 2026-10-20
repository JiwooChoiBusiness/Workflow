"""서비스 API 키(설계 5.1 api_key, 6.2, 11장 S7, 2단계 설계 4.1).

원문은 생성 응답에 한 번만 나가고 DB에는 sha256과 앞 12자만 남는다. 폐기·만료·대상 삭제는
authenticate가 바로 반영한다. HTTP 인증 의존성(Bearer → 401)은 app/auth/dependencies.py에 있다.

키는 대상(KeyTarget) 하나에 묶인다. 에이전트 키와 워크플로우 키(workflow·chatflow)가 있고, 행에서
대상을 읽고 대상별 권한을 정하는 일은 app/services/api_key_target.py가 한다.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import Database
from app.db.models import ApiKey
from app.db.models.enums import WorkflowKind
from app.db.repositories.agents import AgentRepository
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.repositories.workflows import WorkflowRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyOut
from app.security.tokens import API_KEY_PREFIX, hash_token, new_api_key
from app.services.api_key_target import (
    ApiTarget,
    KeyTarget,
    allowed_scopes,
    api_target,
    default_scopes,
    key_target,
    target_columns,
)
from app.services.errors import Problems, ServiceError, not_found

log = structlog.get_logger("app.services.api_keys")

# 요청마다 쓰기를 하지 않도록 last_used_at은 이 간격이 지났을 때만 고친다.
TOUCH_INTERVAL = timedelta(minutes=1)
# 정상 키는 sk-wf- + token_urlsafe(32) = 49자다. 터무니없이 긴 값은 해시하지 않고 거절한다.
_MAX_KEY_LENGTH = 200

__all__ = [
    "ApiKeyService",
    "AuthenticatedApiKey",
    "KeyTarget",
    "default_scopes",
    "key_target",
]


@dataclass(frozen=True)
class AuthenticatedApiKey:
    key_id: uuid.UUID
    workspace_id: uuid.UUID
    # 에이전트 키의 에이전트. 워크플로우 키면 None이다.
    agent_id: uuid.UUID | None
    scopes: tuple[str, ...]
    workflow_id: uuid.UUID | None = None
    # 워크플로우 키가 가리키는 워크플로우의 kind(workflow·chatflow).
    flow_kind: str | None = None

    @property
    def target(self) -> KeyTarget:
        if self.workflow_id is not None:
            return KeyTarget.workflow(self.workflow_id)
        if self.agent_id is None:
            raise ValueError(f"대상이 없는 API 키다: {self.key_id}")
        return KeyTarget.agent(self.agent_id)

    @property
    def api_target(self) -> ApiTarget:
        return api_target(self.target, self.flow_kind)


def _out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        agent_id=key.agent_id,
        workflow_id=key.workflow_id,
        name=key.name,
        key_prefix=key.key_prefix,
        scopes=list(key.scopes),
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
        created_at=key.created_at,
    )


class ApiKeyService:
    def __init__(self, db: Database, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def _check_target(
        self, session: AsyncSession, workspace_id: uuid.UUID, target: KeyTarget
    ) -> str | None:
        """대상이 살아 있는지. 없으면 404. 워크플로우면 그 kind를 돌려준다."""
        if target.kind == "agent":
            if await AgentRepository(session, workspace_id).get_active(target.id) is None:
                raise not_found("에이전트")
            return None
        workflow = await WorkflowRepository(session, workspace_id).get_active(target.id)
        if workflow is None:
            raise not_found("워크플로우")
        return workflow.kind

    async def _keys_of(self, keys: ApiKeyRepository, target: KeyTarget) -> list[ApiKey]:
        if target.kind == "agent":
            return await keys.list_for_agent(target.id)
        return await keys.list_for_workflow(target.id)

    async def _key_of(
        self, keys: ApiKeyRepository, target: KeyTarget, key_id: uuid.UUID
    ) -> ApiKey | None:
        if target.kind == "agent":
            return await keys.get_for_agent(target.id, key_id)
        return await keys.get_for_workflow(target.id, key_id)

    async def list_keys(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> ApiKeyList:
        return await self.list_for(workspace_id, KeyTarget.agent(agent_id))

    async def list_for(self, workspace_id: uuid.UUID, target: KeyTarget) -> ApiKeyList:
        async with self._db.read() as session:
            await self._check_target(session, workspace_id, target)
            keys = await self._keys_of(ApiKeyRepository(session, workspace_id), target)
        return ApiKeyList(items=[_out(key) for key in keys])

    async def create(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, body: ApiKeyCreate
    ) -> ApiKeyCreated:
        return await self.create_for(workspace_id, KeyTarget.agent(agent_id), body)

    async def create_for(
        self, workspace_id: uuid.UUID, target: KeyTarget, body: ApiKeyCreate
    ) -> ApiKeyCreated:
        if body.expires_at is not None and body.expires_at <= self._clock():
            problems = Problems()
            problems.add(("expires_at",), "expires_in_past", "만료 시각은 지금보다 뒤여야 한다")
            problems.raise_if_any()
        issued = new_api_key()
        async with self._db.write() as session:
            kind = await self._check_target(session, workspace_id, target)
            scopes = _scopes(target, kind, body.scopes)
            keys = ApiKeyRepository(session, workspace_id)
            key = keys.add(
                ApiKey(
                    id=new_id(),
                    **target_columns(target),
                    name=body.name,
                    key_prefix=issued.prefix,
                    key_hash=issued.hash,
                    scopes=scopes,
                    expires_at=body.expires_at,
                )
            )
            await keys.flush()
            created = ApiKeyCreated(**_out(key).model_dump(), key=issued.raw)
        log.info(
            "api_key.created", key_id=str(created.id), target=target.kind, target_id=str(target.id)
        )
        return created

    async def revoke(self, workspace_id: uuid.UUID, agent_id: uuid.UUID, key_id: uuid.UUID) -> None:
        await self.revoke_for(workspace_id, KeyTarget.agent(agent_id), key_id)

    async def revoke_for(
        self, workspace_id: uuid.UUID, target: KeyTarget, key_id: uuid.UUID
    ) -> None:
        async with self._db.write() as session:
            await self._check_target(session, workspace_id, target)
            key = await self._key_of(ApiKeyRepository(session, workspace_id), target, key_id)
            if key is None:
                raise not_found("API 키")
            # 이미 폐기한 키를 다시 폐기해도 처음 시각을 그대로 둔다.
            if key.revoked_at is None:
                key.revoked_at = self._clock()
        log.info(
            "api_key.revoked", key_id=str(key_id), target=target.kind, target_id=str(target.id)
        )

    async def authenticate(self, raw_key: str) -> AuthenticatedApiKey | None:
        """Bearer 값으로 키를 찾는다. 없거나 폐기·만료됐거나 대상이 지워졌으면 None."""
        if not raw_key.startswith(API_KEY_PREFIX) or len(raw_key) > _MAX_KEY_LENGTH:
            return None
        now = self._clock()
        async with self._db.read() as session:
            # 1단계는 워크스페이스가 하나다. 키만으로 워크스페이스를 알 수 없어 기본 행을 쓴다.
            workspace = await WorkspaceRepository(session).get_default()
            if workspace is None:
                return None
            key = await ApiKeyRepository(session, workspace.id).get_by_hash(hash_token(raw_key))
            if (
                key is None
                or key.revoked_at is not None
                or (key.expires_at is not None and key.expires_at <= now)
            ):
                return None
            try:
                kind = await self._check_target(session, workspace.id, key_target(key))
            except ServiceError:
                return None
        if key.last_used_at is None or now - key.last_used_at >= TOUCH_INTERVAL:
            async with self._db.write() as session:
                await ApiKeyRepository(session, workspace.id).touch(key.id, now)
        return AuthenticatedApiKey(
            key_id=key.id,
            workspace_id=workspace.id,
            agent_id=key.agent_id,
            scopes=tuple(key.scopes),
            workflow_id=key.workflow_id,
            flow_kind=kind,
        )


def _scopes(target: KeyTarget, kind: str | None, requested: Sequence[str] | None) -> list[str]:
    """요청한 권한(없으면 대상의 기본값). 대상이 받지 않는 권한이면 422다."""
    if requested is None:
        return default_scopes(target, chatflow=kind == WorkflowKind.CHATFLOW)
    scopes = list(dict.fromkeys(requested))
    allowed = allowed_scopes(api_target(target, kind))
    problems = Problems()
    for index, scope in enumerate(scopes):
        if scope not in allowed:
            names = ", ".join(sorted(allowed))
            problems.add(
                ("scopes", index),
                "scope_not_allowed",
                f"이 대상의 키에는 {scope} 권한을 줄 수 없다({names}만 된다)",
            )
    problems.raise_if_any()
    return scopes
