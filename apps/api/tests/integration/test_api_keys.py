import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.services.api_keys import ApiKeyService

FETCH = {"X-Requested-With": "fetch"}


async def create_agent(client: AsyncClient, name: str = "Helper") -> Any:
    response = await client.post("/api/console/agents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def create_key(client: AsyncClient, agent_id: str, **fields: Any) -> Any:
    response = await client.post(
        f"/api/console/agents/{agent_id}/api-keys", json={"name": "자동화", **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


def dump_database(settings: Settings) -> str:
    with closing(sqlite3.connect(settings.data_dir / "app.db")) as conn:
        return "\n".join(conn.iterdump())


async def test_raw_key_is_shown_once_and_only_hash_is_stored(
    logged_in: AsyncClient, settings: Settings
) -> None:
    agent = await create_agent(logged_in)

    response = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "자동화"}
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    created = response.json()
    raw = created["key"]
    assert raw.startswith("sk-wf-") and len(raw) == 49
    assert created["key_prefix"] == raw[:12]
    assert created["scopes"] == ["chat", "tasks"]
    assert (created["revoked_at"], created["expires_at"], created["last_used_at"]) == (
        None,
        None,
        None,
    )

    listed = (await logged_in.get(f"/api/console/agents/{agent['id']}/api-keys")).json()["items"]
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert raw not in str(listed)
    dump = dump_database(settings)
    assert raw not in dump
    assert raw[12:] not in dump
    assert hashlib.sha256(raw.encode()).hexdigest() in dump


async def test_revoked_key_stops_authenticating_immediately(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    agent = await create_agent(logged_in)
    created = await create_key(logged_in, agent["id"], scopes=["chat"])
    service = ApiKeyService(app.state.db)

    authenticated = await service.authenticate(created["key"])

    assert authenticated is not None
    assert str(authenticated.agent_id) == agent["id"]
    assert authenticated.scopes == ("chat",)
    path = f"/api/console/agents/{agent['id']}/api-keys/{created['id']}"
    assert (await logged_in.delete(path, headers=FETCH)).status_code == 204
    assert await service.authenticate(created["key"]) is None
    listed = (await logged_in.get(f"/api/console/agents/{agent['id']}/api-keys")).json()["items"]
    revoked_at = listed[0]["revoked_at"]
    assert revoked_at is not None
    # 다시 폐기해도 처음 시각을 그대로 둔다.
    assert (await logged_in.delete(path, headers=FETCH)).status_code == 204
    again = (await logged_in.get(f"/api/console/agents/{agent['id']}/api-keys")).json()["items"]
    assert again[0]["revoked_at"] == revoked_at


async def test_authenticate_rejects_unknown_and_malformed_keys(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    agent = await create_agent(logged_in)
    created = await create_key(logged_in, agent["id"])
    service = ApiKeyService(app.state.db)

    assert await service.authenticate(created["key"] + "x") is None
    assert await service.authenticate("Bearer " + created["key"]) is None
    assert await service.authenticate("sk-wf-" + "a" * 500) is None
    assert await service.authenticate("") is None


async def test_expired_key_and_last_used(logged_in: AsyncClient, app: FastAPI) -> None:
    agent = await create_agent(logged_in)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    created = await create_key(logged_in, agent["id"], expires_at=expires_at.isoformat())
    now = datetime.now(UTC)

    service = ApiKeyService(app.state.db, clock=lambda: now)
    later = ApiKeyService(app.state.db, clock=lambda: now + timedelta(hours=2))

    assert await service.authenticate(created["key"]) is not None
    assert await later.authenticate(created["key"]) is None
    listed = (await logged_in.get(f"/api/console/agents/{agent['id']}/api-keys")).json()["items"]
    assert listed[0]["last_used_at"] is not None
    assert datetime.fromisoformat(listed[0]["expires_at"]) == expires_at


async def test_expiry_must_be_in_the_future(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    past_response = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "k", "expires_at": past}
    )
    naive = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys",
        json={"name": "k", "expires_at": "2099-01-01T00:00:00"},
    )
    bad_scope = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "k", "scopes": ["admin"]}
    )

    assert past_response.status_code == 422
    assert past_response.json()["error"]["details"][0]["type"] == "expires_in_past"
    assert naive.status_code == 422
    assert bad_scope.status_code == 422


async def test_deleting_agent_revokes_its_keys(logged_in: AsyncClient, app: FastAPI) -> None:
    agent = await create_agent(logged_in)
    created = await create_key(logged_in, agent["id"])
    service = ApiKeyService(app.state.db)

    await logged_in.delete(f"/api/console/agents/{agent['id']}", headers=FETCH)

    assert await service.authenticate(created["key"]) is None
    assert (await logged_in.get(f"/api/console/agents/{agent['id']}/api-keys")).status_code == 404


async def test_keys_are_scoped_to_their_agent(logged_in: AsyncClient) -> None:
    first = await create_agent(logged_in, "First")
    second = await create_agent(logged_in, "Second")
    created = await create_key(logged_in, first["id"])

    wrong_agent = await logged_in.delete(
        f"/api/console/agents/{second['id']}/api-keys/{created['id']}", headers=FETCH
    )

    assert wrong_agent.status_code == 404
    second_keys = await logged_in.get(f"/api/console/agents/{second['id']}/api-keys")
    assert second_keys.json()["items"] == []


async def test_service_api_accepts_the_key_until_revoked(
    logged_in: AsyncClient, make_client: Any
) -> None:
    agent = await create_agent(logged_in)
    created = await create_key(logged_in, agent["id"])
    bearer = {"Authorization": f"Bearer {created['key']}"}
    # 서비스 API는 쿠키가 아니라 키로만 인증한다. 로그인하지 않은 클라이언트로 부른다.
    outsider = make_client()

    info = await outsider.get("/api/v1/info", headers=bearer)
    await logged_in.delete(
        f"/api/console/agents/{agent['id']}/api-keys/{created['id']}", headers=FETCH
    )
    revoked = await outsider.get("/api/v1/info", headers=bearer)

    assert info.status_code == 200
    assert (info.json()["agent_id"], info.json()["published"]) == (agent["id"], False)
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "invalid_api_key"
    assert revoked.headers["www-authenticate"] == "Bearer"
