import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import LoginSession
from app.db.repositories.accounts import AccountRepository
from app.db.repositories.login_sessions import LoginSessionRepository
from app.db.types import utcnow
from app.housekeeping import Housekeeping
from app.sandbox.local_workspace import DELETED_PREFIX, LocalWorkspaceBackend


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "app.db")
    await upgrade_head(database)
    yield database
    await database.close()


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[LocalWorkspaceBackend]:
    instance = LocalWorkspaceBackend(Settings.model_validate({"data_dir": tmp_path / "data"}))
    yield instance
    await instance.aclose()


async def add_login_sessions(db: Database) -> None:
    now = utcnow()
    async with db.write() as session:
        account = AccountRepository(session).add(
            email="owner@example.com",
            name="owner",
            password_hash="not-a-real-hash",  # noqa: S106  테스트 계정
        )
        await session.flush()
        logins = LoginSessionRepository(session)
        for token_hash, expires_at in (
            ("expired", now - timedelta(minutes=1)),
            ("valid", now + timedelta(days=1)),
        ):
            logins.add(
                token_hash=token_hash,
                account_id=account.id,
                now=now - timedelta(days=7),
                expires_at=expires_at,
                user_agent=None,
            )


async def remaining_logins(db: Database) -> list[str]:
    async with db.read() as session:
        return list(await session.scalars(select(LoginSession.token_hash)))


def make_leftover(backend: LocalWorkspaceBackend) -> Path:
    leftover = backend.workspaces_dir / f"{DELETED_PREFIX}1-old"
    (leftover / "locked").mkdir(parents=True)
    return leftover


async def test_hourly_job_purges_expired_logins_and_deleted_workspaces(
    db: Database, backend: LocalWorkspaceBackend
) -> None:
    await add_login_sessions(db)
    leftover = make_leftover(backend)
    housekeeping = Housekeeping(db=db, backend=backend, memory_budget_mb=300)

    await housekeeping.run_hourly()

    assert await remaining_logins(db) == ["valid"]
    assert not leftover.exists()


async def test_hourly_job_keeps_going_when_one_task_fails(
    db: Database, backend: LocalWorkspaceBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_login_sessions(db)

    async def locked() -> int:
        raise PermissionError("locked")

    monkeypatch.setattr(backend, "purge_deleted", locked)
    housekeeping = Housekeeping(db=db, backend=backend, memory_budget_mb=300)

    await housekeeping.run_hourly()

    assert await remaining_logins(db) == ["valid"]


async def test_start_runs_the_hourly_job_right_away_and_stop_cancels(
    db: Database, backend: LocalWorkspaceBackend
) -> None:
    await add_login_sessions(db)
    leftover = make_leftover(backend)
    housekeeping = Housekeeping(
        db=db, backend=backend, memory_budget_mb=300, interval_sec=3600, hourly_interval_sec=3600
    )

    housekeeping.start()
    try:
        for _ in range(100):
            if not leftover.exists():
                break
            await asyncio.sleep(0.02)
    finally:
        await housekeeping.stop()

    assert not leftover.exists()
    assert await remaining_logins(db) == ["valid"]
    # 멈춘 뒤 다시 멈춰도 문제없다(종료 훅이 여러 경로에서 부를 수 있다).
    await housekeeping.stop()
