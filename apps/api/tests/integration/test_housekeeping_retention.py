"""하우스키핑 보존 정리(설계 5.4): 참조 없는 첨부·스킬 버전, 지운 세션의 첨부, 고아 작업공간."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.config import Settings
from app.db.engine import Database
from app.db.models import AgentFile, ChatSession, SessionFile, SkillVersion, WorkspaceInstance
from app.db.types import utcnow
from app.housekeeping import Housekeeping, Retention
from app.llm.fake_adapter import fake_text

FETCH = {"X-Requested-With": "fetch"}
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "updated_at",
)


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def retention(app: FastAPI, *, days_later: float) -> Retention:
    later = utcnow() + timedelta(days=days_later)
    return Retention(
        app.state.db,
        storage=app.state.storage,
        backend=app.state.sandbox,
        clock=lambda: later,
    )


async def save_draft(client: AsyncClient, agent_id: str, **changes: Any) -> None:
    draft = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["env_vars"] = []
    body.update(changes)
    response = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
    assert response.status_code == 200, response.text


async def upload_agent_file(client: AsyncClient, agent_id: str, name: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/console/agents/{agent_id}/files",
        files={"file": (name, name.encode() * 10, "text/plain")},
        headers=FETCH,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def upload_skill(
    client: AsyncClient, data: bytes, *, skill_id: str | None = None
) -> dict[str, Any]:
    path = f"/api/console/skills/{skill_id}/versions" if skill_id else "/api/console/skills"
    response = await client.post(
        path, files={"file": ("skill.zip", data, "application/zip")}, headers=FETCH
    )
    assert response.status_code == 201, response.text
    return response.json()


async def ids_of(db: Database, column: Any) -> set[str]:
    async with db.read() as session:
        return {str(value) for value in await session.scalars(select(column))}


async def test_unreferenced_agent_files_go_after_the_grace_period(
    app: FastAPI, logged_in: AsyncClient, make_session: Any, db: Database, settings: Settings
) -> None:
    agent_id, _ = await make_session(logged_in)
    kept = await upload_agent_file(logged_in, agent_id, "in-draft.txt")
    loose = await upload_agent_file(logged_in, agent_id, "loose.txt")
    await save_draft(logged_in, agent_id, file_ids=[kept["id"]])
    storage = settings.data_dir / "storage"
    loose_files = list(storage.rglob(loose["id"]))
    assert len(loose_files) == 1

    # 7일이 지나지 않았으면 참조가 없어도 남는다.
    assert await retention(app, days_later=6).purge_agent_files() == 0
    assert await retention(app, days_later=8).purge_agent_files() == 1

    assert await ids_of(db, AgentFile.id) == {kept["id"]}
    assert not loose_files[0].exists()
    assert list(storage.rglob(kept["id"]))
    listed = (await logged_in.get(f"/api/console/agents/{agent_id}/files")).json()["items"]
    assert [item["id"] for item in listed] == [kept["id"]]


async def test_skill_versions_in_use_or_current_are_kept(
    app: FastAPI,
    logged_in: AsyncClient,
    make_session: Any,
    db: Database,
    skill_zip: Callable[..., bytes],
) -> None:
    used = await upload_skill(logged_in, skill_zip(name="used-skill", top="used-skill"))
    used_v2 = await upload_skill(
        logged_in, skill_zip(name="used-skill", top="used-skill"), skill_id=used["id"]
    )
    idle = await upload_skill(logged_in, skill_zip(name="idle-skill", top="idle-skill"))
    idle_v2 = await upload_skill(
        logged_in, skill_zip(name="idle-skill", top="idle-skill"), skill_id=idle["id"]
    )
    gone = await upload_skill(logged_in, skill_zip(name="gone-skill", top="gone-skill"))
    deleted = await logged_in.delete(f"/api/console/skills/{gone['id']}", headers=FETCH)
    assert deleted.status_code == 204
    agent_id, _ = await make_session(logged_in)
    # 이 에이전트의 초안은 used-skill의 옛 버전(v1)을 쓴다.
    await save_draft(logged_in, agent_id, skill_version_ids=[used["current_version_id"]])

    removed = await retention(app, days_later=8).purge_skill_versions()

    # idle-skill v1(현재 버전 아님, 참조 없음)과 지운 gone-skill의 버전이 빠진다.
    assert removed == 2
    assert await ids_of(db, SkillVersion.id) == {
        used["current_version_id"],
        used_v2["id"],
        idle_v2["id"],
    }
    detail = (await logged_in.get(f"/api/console/skills/{idle['id']}")).json()
    assert [version["version_number"] for version in detail["versions"]] == [2]


async def test_uploads_of_deleted_sessions_and_orphan_upload_folders(
    app: FastAPI, logged_in: AsyncClient, make_session: Any, db: Database, settings: Settings
) -> None:
    _, live_session = await make_session(logged_in)
    _, dead_session = await make_session(logged_in)
    for session_id in (live_session, dead_session):
        response = await logged_in.post(
            f"/api/console/sessions/{session_id}/uploads",
            files={"file": ("note.txt", b"memo", "text/plain")},
            headers=FETCH,
        )
        assert response.status_code == 201, response.text
    # 세션 삭제가 중간에 멈춰 행과 파일이 남은 상태를 만든다.
    async with db.write() as session:
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == uuid.UUID(dead_session))
            .values(deleted_at=utcnow())
        )
    uploads = settings.data_dir / "storage" / "uploads"
    stray = uploads / str(uuid.uuid4())
    stray.mkdir(parents=True)
    (stray / "left.bin").write_bytes(b"x")

    removed = await retention(app, days_later=0).purge_session_uploads()

    assert removed == 2
    async with db.read() as session:
        rows = list(await session.scalars(select(SessionFile.session_id)))
    assert [str(value) for value in rows] == [live_session]
    assert list((uploads / live_session).iterdir())
    assert not (uploads / dead_session).exists() or not list((uploads / dead_session).iterdir())
    assert not stray.exists()


async def test_orphan_workspaces_are_removed(
    app: FastAPI,
    logged_in: AsyncClient,
    make_session: Any,
    script: Any,
    db: Database,
    settings: Settings,
) -> None:
    _, live_session = await make_session(logged_in)
    _, dead_session = await make_session(logged_in)
    script.add(fake_text("살아 있다"), fake_text("곧 지운다"))
    for session_id in (live_session, dead_session):
        response = await logged_in.post(
            f"/api/console/sessions/{session_id}/messages", json={"text": "만들어"}
        )
        assert response.status_code == 200
    workspaces = settings.data_dir / "workspaces"
    async with db.write() as session:
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == uuid.UUID(dead_session))
            .values(deleted_at=utcnow())
        )
    orphan = workspaces / str(uuid.uuid4())
    (orphan / "outputs").mkdir(parents=True)
    (orphan / "outputs" / "x.txt").write_text("x", encoding="utf-8")
    unknown = workspaces / "not-a-session"
    unknown.mkdir()

    removed = await retention(app, days_later=0).purge_orphan_workspaces()

    assert removed == 2
    assert (workspaces / live_session).is_dir()
    assert not (workspaces / dead_session).exists()
    assert not orphan.exists()
    assert unknown.is_dir()
    assert await ids_of(db, WorkspaceInstance.session_id) == {live_session}
    listed = (await logged_in.get("/api/console/workspaces")).json()["items"]
    assert [item["session_id"] for item in listed] == [live_session]


class FlakyRetention(Retention):
    def jobs(self) -> list[tuple[str, Callable[[], Awaitable[int]]]]:
        async def broken() -> int:
            raise PermissionError("잠김")

        return [("broken", broken), *super().jobs()]


async def test_hourly_job_runs_retention_and_survives_a_failing_job(
    app: FastAPI, logged_in: AsyncClient, make_session: Any, db: Database
) -> None:
    agent_id, _ = await make_session(logged_in)
    loose = await upload_agent_file(logged_in, agent_id, "loose.txt")
    later = utcnow() + timedelta(days=8)
    jobs = FlakyRetention(
        db, storage=app.state.storage, backend=app.state.sandbox, clock=lambda: later
    )
    housekeeping = Housekeeping(
        db=db, backend=app.state.sandbox, memory_budget_mb=300, retention=jobs
    )

    summary = await housekeeping.run_hourly()

    assert loose["id"] not in await ids_of(db, AgentFile.id)
    # 실패한 작업은 로그 값이 None이고 나머지는 지운 개수가 남는다.
    assert summary["purged_broken"] is None
    assert summary["purged_agent_files"] == 1
