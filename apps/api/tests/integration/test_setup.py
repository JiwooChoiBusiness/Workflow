import asyncio
from collections.abc import Callable

from httpx import AsyncClient
from sqlalchemy import func, select

from app.config import Settings
from app.db.engine import Database
from app.db.models import Account, Workspace

EMAIL = "Owner@Example.com "
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정


async def test_setup_status_before_and_after(client: AsyncClient) -> None:
    before = await client.get("/api/console/setup")

    assert before.status_code == 200
    assert before.json() == {"needs_setup": True, "password_min_length": 12}

    created = await client.post("/api/console/setup", json={"email": EMAIL, "password": PASSWORD})
    assert created.status_code == 201

    after = await client.get("/api/console/setup")
    again = await client.post(
        "/api/console/setup", json={"email": "other@example.com", "password": PASSWORD}
    )
    assert after.status_code == 404
    assert again.status_code == 404
    assert again.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_setup_creates_account_and_workspace(
    client: AsyncClient, db: Database, settings: Settings
) -> None:
    response = await client.post("/api/console/setup", json={"email": EMAIL, "password": PASSWORD})

    body = response.json()
    assert response.status_code == 201
    assert body["account"]["email"] == "owner@example.com"
    assert body["account"]["name"] == "owner"
    assert body["account"]["last_login_at"] is None
    assert body["workspace"]["slug"] == "default"
    async with db.read() as session:
        account = (await session.scalars(select(Account))).one()
        workspace = (await session.scalars(select(Workspace))).one()
    assert account.password_hash.startswith("$argon2id$")
    assert PASSWORD not in account.password_hash
    assert str(workspace.id) == body["workspace"]["id"]
    assert workspace.settings["approval_mode_default"] == settings.approval_mode_default
    assert workspace.settings["run_event_retention_days"] == settings.run_event_retention_days
    assert workspace.settings["default_limits"]["max_steps"] == 30


async def test_setup_does_not_log_in(client: AsyncClient) -> None:
    await client.post("/api/console/setup", json={"email": EMAIL, "password": PASSWORD})

    response = await client.get("/api/console/auth/me")

    assert response.status_code == 401


async def test_concurrent_setup_creates_one_account(
    make_client: Callable[[], AsyncClient], db: Database
) -> None:
    clients = [make_client() for _ in range(2)]

    responses = await asyncio.gather(
        *(
            c.post(
                "/api/console/setup",
                json={"email": f"user{i}@example.com", "password": PASSWORD},
            )
            for i, c in enumerate(clients)
        )
    )

    assert sorted(r.status_code for r in responses) == [201, 404]
    async with db.read() as session:
        assert await session.scalar(select(func.count()).select_from(Account)) == 1
        assert await session.scalar(select(func.count()).select_from(Workspace)) == 1


async def test_setup_validation_does_not_echo_password(client: AsyncClient) -> None:
    short = "tiny-secret"

    response = await client.post("/api/console/setup", json={"email": EMAIL, "password": short})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [detail["loc"] for detail in error["details"]] == [["body", "password"]]
    assert short not in response.text


async def test_setup_rejects_bad_email_and_unknown_fields(client: AsyncClient) -> None:
    bad_email = await client.post(
        "/api/console/setup", json={"email": "not-an-email", "password": PASSWORD}
    )
    extra = await client.post(
        "/api/console/setup", json={"email": EMAIL, "password": PASSWORD, "role": "admin"}
    )

    assert bad_email.status_code == 422
    assert extra.status_code == 422
    assert (await client.get("/api/console/setup")).status_code == 200
