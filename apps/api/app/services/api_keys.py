"""서비스 API 키(설계 5.1 api_key, 6.2, 11장 S7).

원문은 생성 응답에 한 번만 나가고 DB에는 sha256과 앞 12자만 남는다. 폐기·만료·에이전트 삭제는
authenticate가 바로 반영한다. HTTP 인증 의존성(Bearer → 401)은 서비스 API와 함께 M8에서 붙인다.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from app.db.engine import Database
from app.db.models import ApiKey
from app.db.repositories.agents import AgentRepository
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyOut
from app.security.tokens import API_KEY_PREFIX, hash_token, new_api_key
from app.services.errors import Problems, not_found

log = structlog.get_logger("app.services.api_keys")

# 요청마다 쓰기를 하지 않도록 last_used_at은 이 간격이 지났을 때만 고친다.
TOUCH_INTERVAL = timedelta(minutes=1)
# 정상 키는 sk-wf- + token_urlsafe(32) = 49자다. 터무니없이 긴 값은 해시하지 않고 거절한다.
_MAX_KEY_LENGTH = 200


@dataclass(frozen=True)
class AuthenticatedApiKey:
    key_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_id: uuid.UUID
    scopes: tuple[str, ...]


def _out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        agent_id=key.agent_id,
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

    async def list_keys(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> ApiKeyList:
        async with self._db.read() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            keys = await ApiKeyRepository(session, workspace_id).list_for_agent(agent_id)
        return ApiKeyList(items=[_out(key) for key in keys])

    async def create(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, body: ApiKeyCreate
    ) -> ApiKeyCreated:
        if body.expires_at is not None and body.expires_at <= self._clock():
            problems = Problems()
            problems.add(("expires_at",), "expires_in_past", "만료 시각은 지금보다 뒤여야 한다")
            problems.raise_if_any()
        issued = new_api_key()
        async with self._db.write() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            keys = ApiKeyRepository(session, workspace_id)
            key = keys.add(
                ApiKey(
                    id=new_id(),
                    agent_id=agent_id,
                    name=body.name,
                    key_prefix=issued.prefix,
                    key_hash=issued.hash,
                    scopes=list(dict.fromkeys(body.scopes)),
                    expires_at=body.expires_at,
                )
            )
            await keys.flush()
            created = ApiKeyCreated(**_out(key).model_dump(), key=issued.raw)
        log.info("api_key.created", key_id=str(created.id), agent_id=str(agent_id))
        return created

    async def revoke(self, workspace_id: uuid.UUID, agent_id: uuid.UUID, key_id: uuid.UUID) -> None:
        async with self._db.write() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            key = await ApiKeyRepository(session, workspace_id).get_for_agent(agent_id, key_id)
            if key is None:
                raise not_found("API 키")
            # 이미 폐기한 키를 다시 폐기해도 처음 시각을 그대로 둔다.
            if key.revoked_at is None:
                key.revoked_at = self._clock()
        log.info("api_key.revoked", key_id=str(key_id), agent_id=str(agent_id))

    async def authenticate(self, raw_key: str) -> AuthenticatedApiKey | None:
        """Bearer 값으로 키를 찾는다. 없거나 폐기·만료됐거나 에이전트가 지워졌으면 None."""
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
            if await AgentRepository(session, workspace.id).get_active(key.agent_id) is None:
                return None
        if key.last_used_at is None or now - key.last_used_at >= TOUCH_INTERVAL:
            async with self._db.write() as session:
                await ApiKeyRepository(session, workspace.id).touch(key.id, now)
        return AuthenticatedApiKey(
            key_id=key.id,
            workspace_id=workspace.id,
            agent_id=key.agent_id,
            scopes=tuple(key.scopes),
        )
