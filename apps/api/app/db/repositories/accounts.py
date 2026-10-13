import uuid
from datetime import datetime

from sqlalchemy import func, select, update

from app.db.models import Account
from app.db.repositories.base import GlobalRepository


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AccountRepository(GlobalRepository[Account]):
    model = Account

    async def count(self) -> int:
        return await self.session.scalar(select(func.count()).select_from(Account)) or 0

    async def get(self, account_id: uuid.UUID) -> Account | None:
        return await self.session.get(Account, account_id)

    async def get_by_email(self, email: str) -> Account | None:
        return await self.session.scalar(
            select(Account).where(Account.email == normalize_email(email))
        )

    async def first(self) -> Account | None:
        return await self.session.scalar(select(Account).order_by(Account.created_at).limit(1))

    def add(self, *, email: str, name: str, password_hash: str) -> Account:
        account = Account(email=normalize_email(email), name=name, password_hash=password_hash)
        self.session.add(account)
        return account

    async def set_password(self, account_id: uuid.UUID, password_hash: str) -> None:
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(password_hash=password_hash)
        )

    async def mark_login(self, account_id: uuid.UUID, at: datetime) -> None:
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(last_login_at=at)
        )
