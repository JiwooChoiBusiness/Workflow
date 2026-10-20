import asyncio
import uuid
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

    summary = await housekeeping.run_hourly()

    assert await remaining_logins(db) == ["valid"]
    assert summary["purged_workspaces"] is None
    assert summary["expired_login_sessions"] == 1


class RecordingRuns:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def expire_waiting(self) -> int:
        self.calls.append("expire_waiting")
        return 1

    async def purge_events(self) -> int:
        self.calls.append("purge_events")
        raise PermissionError("locked")

    async def purge_tool_outputs(self) -> int:
        self.calls.append("purge_tool_outputs")
        return 2

    async def release_task_workspaces(self) -> int:
        self.calls.append("release_task_workspaces")
        return 3

    async def reconcile_children(self) -> int:
        self.calls.append("reconcile_children")
        return 0


async def test_hourly_job_runs_every_run_job_in_order(
    db: Database, backend: LocalWorkspaceBackend
) -> None:
    runs = RecordingRuns()
    housekeeping = Housekeeping(db=db, backend=backend, memory_budget_mb=300, runs=runs)

    summary = await housekeeping.run_hourly()

    # tool-outputs 정리는 이벤트를 지운 뒤에 남은 폴더를 본다.
    assert runs.calls == [
        "expire_waiting",
        "reconcile_children",
        "purge_events",
        "purge_tool_outputs",
        "release_task_workspaces",
    ]
    assert summary["input_timeouts"] == 1
    assert summary["purged_run_events"] is None
    assert summary["purged_tool_outputs"] == 2
    assert summary["released_task_workspaces"] == 3


async def test_hourly_job_logs_disk_sizes_and_checkpoints_the_wal(
    db: Database, backend: LocalWorkspaceBackend
) -> None:
    await add_login_sessions(db)
    session_dir = backend.workspaces_dir / str(uuid.uuid4())
    (session_dir / ".packages").mkdir(parents=True)
    (session_dir / ".packages" / "big.whl").write_bytes(b"x" * (3 * 2**20 // 2))
    pip_cache = backend.workspaces_dir.parent / "cache" / "pip"
    pip_cache.mkdir(parents=True)
    (pip_cache / "cached.whl").write_bytes(b"x" * (2**20 // 2))
    # 2단계 폴더도 같이 잰다(2단계 설계 11.3).
    data_dir = backend.workspaces_dir.parent
    for parts in (("wf-workspaces", "r1"), ("storage", "wf-files", "w1"), ("cache", "wf-extract")):
        folder = data_dir.joinpath(*parts)
        folder.mkdir(parents=True)
        (folder / "f.bin").write_bytes(b"x" * (2**20 // 2))
    housekeeping = Housekeeping(db=db, backend=backend, memory_budget_mb=300)

    summary = await housekeeping.run_hourly()

    assert (summary["workspaces_mb"], summary["pip_cache_mb"]) == (1.5, 0.5)
    assert (summary["wf_workspaces_mb"], summary["wf_files_mb"], summary["wf_extract_mb"]) == (
        0.5,
        0.5,
        0.5,
    )
    assert summary["disk_estimated"] is False
    # 지운 로그인 세션까지 DB 파일로 옮겼다. 하우스키핑이 쓰기 락을 잡지 않아도 끝난다.
    assert summary["wal_frames"] > 0
    assert summary["wal_checkpointed"] == summary["wal_frames"]
    # 재기만 한다.
    assert (session_dir / ".packages" / "big.whl").is_file()
    assert (pip_cache / "cached.whl").is_file()


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
