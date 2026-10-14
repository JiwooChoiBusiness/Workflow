import asyncio
import hashlib
import io
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app import cli
from app.config import Settings, get_settings
from app.db.engine import Database
from app.db.models import LoginSession
from app.db.types import utcnow

PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
NEW_PASSWORD = "staple-battery-horse-2"  # noqa: S105
WRONG_PASSWORD = "wrong-password-123"  # noqa: S105
ME = "/api/console/auth/me"


async def _sessions(db: Database) -> list[LoginSession]:
    async with db.read() as session:
        return list((await session.scalars(select(LoginSession))).all())


async def test_login_sets_http_only_cookie_and_stores_only_hash(
    client: AsyncClient, account: dict[str, Any], login: Callable[..., Any], db: Database
) -> None:
    response = await login(client)

    assert response.status_code == 200
    assert response.json()["account"]["email"] == "owner@example.com"
    assert response.json()["account"]["last_login_at"] is not None
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("sid=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=604800" in cookie
    assert "Path=/" in cookie
    token = client.cookies["sid"]
    rows = await _sessions(db)
    assert [row.token_hash for row in rows] == [hashlib.sha256(token.encode()).hexdigest()]
    assert rows[0].expires_at - rows[0].created_at == timedelta(days=7)


async def test_me_requires_login(client: AsyncClient, account: dict[str, Any]) -> None:
    response = await client.get(ME)

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "로그인이 필요하다"}}


async def test_me_returns_account_and_workspace(logged_in: AsyncClient) -> None:
    response = await logged_in.get(ME)

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["email"] == "owner@example.com"
    assert body["workspace"]["slug"] == "default"


async def test_wrong_password_and_unknown_email_get_same_error(
    client: AsyncClient, account: dict[str, Any], login: Callable[..., Any]
) -> None:
    wrong = await login(client, password=WRONG_PASSWORD)
    unknown = await login(client, email="nobody@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    assert "sid" not in client.cookies


async def test_logout_deletes_server_session(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient], db: Database
) -> None:
    token = logged_in.cookies["sid"]

    response = await logged_in.post(
        "/api/console/auth/logout", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 204
    assert 'sid=""' in response.headers["set-cookie"]
    assert await _sessions(db) == []
    replay = make_client()
    replay.cookies.set("sid", token)
    assert (await replay.get(ME)).status_code == 401


async def test_password_change_revokes_other_sessions(
    logged_in: AsyncClient,
    make_client: Callable[[], AsyncClient],
    login: Callable[..., Any],
    db: Database,
) -> None:
    other = make_client()
    assert (await login(other)).status_code == 200
    assert len(await _sessions(db)) == 2

    response = await logged_in.post(
        "/api/console/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 204
    assert (await logged_in.get(ME)).status_code == 200
    assert (await other.get(ME)).status_code == 401
    assert len(await _sessions(db)) == 1
    fresh = make_client()
    assert (await login(fresh)).status_code == 401
    assert (await login(fresh, password=NEW_PASSWORD)).status_code == 200


async def test_password_change_checks_current_password(logged_in: AsyncClient) -> None:
    wrong = await logged_in.post(
        "/api/console/auth/password",
        json={"current_password": "not-my-password", "new_password": NEW_PASSWORD},
    )
    short = await logged_in.post(
        "/api/console/auth/password",
        json={"current_password": PASSWORD, "new_password": "short"},
    )

    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "wrong_password"
    assert short.status_code == 422


async def test_login_rate_limit(
    client: AsyncClient, account: dict[str, Any], login: Callable[..., Any]
) -> None:
    for _ in range(5):
        assert (await login(client, password=WRONG_PASSWORD)).status_code == 401

    blocked = await login(client)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60


async def test_expired_session_is_rejected(logged_in: AsyncClient, db: Database) -> None:
    async with db.write() as session:
        await session.execute(
            update(LoginSession).values(expires_at=utcnow() - timedelta(seconds=1))
        )

    assert (await logged_in.get(ME)).status_code == 401


async def test_session_expiry_slides_when_used(logged_in: AsyncClient, db: Database) -> None:
    an_hour_left = utcnow() + timedelta(hours=1)
    async with db.write() as session:
        await session.execute(
            update(LoginSession).values(
                expires_at=an_hour_left, last_seen_at=utcnow() - timedelta(minutes=5)
            )
        )

    response = await logged_in.get(ME)

    assert response.status_code == 200
    assert "Max-Age=604800" in response.headers["set-cookie"]
    (row,) = await _sessions(db)
    assert row.expires_at > utcnow() + timedelta(days=6, hours=23)


async def test_recent_session_is_not_rewritten_every_request(
    logged_in: AsyncClient, db: Database
) -> None:
    (before,) = await _sessions(db)

    response = await logged_in.get(ME)

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    (after,) = await _sessions(db)
    assert after.last_seen_at == before.last_seen_at


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Content-Type": "text/plain"}, 415, "json_required"),
        ({}, 415, "json_required"),
        (
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
            403,
            "origin_not_allowed",
        ),
        ({"Content-Type": "application/json", "Origin": "null"}, 403, "origin_not_allowed"),
    ],
)
async def test_csrf_rejects(
    client: AsyncClient,
    account: dict[str, Any],
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    body = b'{"email": "owner@example.com", "password": "correct-horse-battery"}'

    response = await client.post("/api/console/auth/login", content=body, headers=headers)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "sid" not in client.cookies


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json; charset=utf-8", "Origin": "http://127.0.0.1:8500"},
        {"Content-Type": "application/json", "Origin": "http://localhost:8500"},
        {"Content-Type": "application/json", "Origin": "http://localhost:5173"},
        {"X-Requested-With": "fetch"},
    ],
)
async def test_csrf_allows_same_origin_and_dev_server(
    client: AsyncClient, account: dict[str, Any], headers: dict[str, str]
) -> None:
    response = await client.post("/api/console/auth/logout", headers=headers)

    assert response.status_code == 204


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example:8500"])
async def test_foreign_host_is_rejected(client: AsyncClient, host: str) -> None:
    # DNS 리바인딩으로 들어온 요청은 Host가 공격자 도메인이다(S6). 로그인 전 경로도 막는다.
    for path in ("/healthz", "/api/console/setup", "/api/console/auth/me"):
        response = await client.get(path, headers={"Host": host})

        assert response.status_code == 400, path

    assert (await client.get("/healthz", headers={"Host": "localhost:8500"})).status_code == 200


async def test_reset_password_cli(
    logged_in: AsyncClient,
    make_client: Callable[[], AsyncClient],
    login: Callable[..., Any],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(NEW_PASSWORD + "\n"))
    get_settings.cache_clear()
    try:
        # CLI는 asyncio.run을 쓰므로 테스트의 이벤트 루프 밖(다른 스레드)에서 돌린다.
        code = await asyncio.to_thread(cli.main, ["reset-password", "--password-stdin"])
    finally:
        get_settings.cache_clear()

    assert code == 0
    assert "로그인 세션 1개" in capsys.readouterr().out
    assert (await logged_in.get(ME)).status_code == 401
    fresh = make_client()
    assert (await login(fresh)).status_code == 401
    assert (await login(fresh, password=NEW_PASSWORD)).status_code == 200


async def test_reset_password_cli_rejects_short_password(
    account: dict[str, Any],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    get_settings.cache_clear()
    try:
        code = await asyncio.to_thread(cli.main, ["reset-password", "--password-stdin"])
    finally:
        get_settings.cache_clear()

    assert code == 1
