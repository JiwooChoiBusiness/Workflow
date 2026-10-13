import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.engine import Database
from app.db.models import Account
from app.db.repositories.accounts import AccountRepository
from app.db.repositories.login_sessions import LoginSessionRepository
from app.db.types import utcnow
from app.security.tokens import hash_token, new_session_token

SESSION_TTL = timedelta(days=7)
# 요청마다 쓰기를 하지 않도록 만료 연장은 이 간격이 지났을 때만 한다.
TOUCH_INTERVAL = timedelta(minutes=1)
USER_AGENT_MAX_LENGTH = 512


@dataclass(frozen=True)
class IssuedSession:
    token: str
    token_hash: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ActiveSession:
    token_hash: str
    account: Account
    expires_at: datetime
    # 만료를 연장했으면 쿠키의 Max-Age도 다시 내려보낸다.
    refreshed: bool


class SessionStore:
    # 쿠키 sid 값의 원문은 DB에 두지 않고 sha256만 저장한다(설계 6.1).

    def __init__(self, db: Database, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def issue(self, account_id: uuid.UUID, user_agent: str | None) -> IssuedSession:
        token = new_session_token()
        token_hash = hash_token(token)
        now = self._clock()
        expires_at = now + SESSION_TTL
        async with self._db.write() as session:
            LoginSessionRepository(session).add(
                token_hash=token_hash,
                account_id=account_id,
                now=now,
                expires_at=expires_at,
                user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
            )
            await AccountRepository(session).mark_login(account_id, now)
        return IssuedSession(token, token_hash, now, expires_at)

    async def resolve(self, token: str) -> ActiveSession | None:
        token_hash = hash_token(token)
        now = self._clock()
        async with self._db.read() as session:
            found = await LoginSessionRepository(session).get_with_account(token_hash)
        if found is None:
            return None
        login, account = found
        if login.expires_at <= now:
            return None
        if now - login.last_seen_at < TOUCH_INTERVAL:
            return ActiveSession(token_hash, account, login.expires_at, refreshed=False)
        expires_at = now + SESSION_TTL
        async with self._db.write() as session:
            await LoginSessionRepository(session).touch(token_hash, now=now, expires_at=expires_at)
        return ActiveSession(token_hash, account, expires_at, refreshed=True)

    async def revoke(self, token: str) -> None:
        async with self._db.write() as session:
            await LoginSessionRepository(session).delete(hash_token(token))

    async def revoke_all(self, account_id: uuid.UUID, *, keep_token_hash: str | None = None) -> int:
        async with self._db.write() as session:
            return await LoginSessionRepository(session).delete_for_account(
                account_id, keep=keep_token_hash
            )

    async def purge_expired(self) -> int:
        # 하우스키핑이 매시 부른다(설계 5.4).
        async with self._db.write() as session:
            return await LoginSessionRepository(session).delete_expired(self._clock())
