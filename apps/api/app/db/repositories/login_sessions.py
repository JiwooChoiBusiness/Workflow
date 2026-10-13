import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, delete, select, update

from app.db.models import Account, LoginSession
from app.db.repositories.base import GlobalRepository


class LoginSessionRepository(GlobalRepository[LoginSession]):
    model = LoginSession

    async def get(self, token_hash: str) -> LoginSession | None:
        return await self.session.get(LoginSession, token_hash)

    async def get_with_account(self, token_hash: str) -> tuple[LoginSession, Account] | None:
        row = (
            await self.session.execute(
                select(LoginSession, Account)
                .join(Account, Account.id == LoginSession.account_id)
                .where(LoginSession.token_hash == token_hash)
            )
        ).first()
        return None if row is None else (row[0], row[1])

    def add(
        self,
        *,
        token_hash: str,
        account_id: uuid.UUID,
        now: datetime,
        expires_at: datetime,
        user_agent: str | None,
    ) -> LoginSession:
        row = LoginSession(
            token_hash=token_hash,
            account_id=account_id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            user_agent=user_agent,
        )
        self.session.add(row)
        return row

    async def touch(self, token_hash: str, *, now: datetime, expires_at: datetime) -> None:
        await self.session.execute(
            update(LoginSession)
            .where(LoginSession.token_hash == token_hash)
            .values(last_seen_at=now, expires_at=expires_at)
        )

    async def delete(self, token_hash: str) -> int:
        return await self._delete_where(LoginSession.token_hash == token_hash)

    async def delete_for_account(self, account_id: uuid.UUID, *, keep: str | None = None) -> int:
        conditions = [LoginSession.account_id == account_id]
        if keep is not None:
            conditions.append(LoginSession.token_hash != keep)
        return await self._delete_where(*conditions)

    async def delete_expired(self, now: datetime) -> int:
        return await self._delete_where(LoginSession.expires_at <= now)

    async def list_for_account(self, account_id: uuid.UUID) -> Sequence[LoginSession]:
        result = await self.session.scalars(
            select(LoginSession).where(LoginSession.account_id == account_id)
        )
        return result.all()

    async def _delete_where(self, *conditions: ColumnElement[bool]) -> int:
        result = await self.session.execute(delete(LoginSession).where(*conditions))
        return cast(CursorResult[Any], result).rowcount
