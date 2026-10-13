"""설정 하위 API: http_request 허용 목록 보기(설계 3.9, 6.2 GET /settings/http-allowlist)."""

import codecs
from pathlib import Path
from typing import Any

from httpx import AsyncClient

from app.config import Settings
from app.sandbox.allowlist import DEFAULT_ENTRIES


async def allowlist(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/console/settings/http-allowlist")
    assert response.status_code == 200, response.text
    return response.json()


async def test_default_file_is_shown_read_only(logged_in: AsyncClient, settings: Settings) -> None:
    body = await allowlist(logged_in)

    assert Path(body["path"]) == settings.allowlist_file
    assert body["exists"] is True and body["truncated"] is False
    assert body["size_bytes"] == settings.allowlist_file.stat().st_size
    assert body["modified_at"] is not None
    assert body["content"] == settings.allowlist_file.read_text(encoding="utf-8")
    rules = body["rules"]
    assert rules["allow_any"] is False
    expected = set(DEFAULT_ENTRIES)
    assert set(rules["hosts"]) | set(rules["suffixes"]) == expected
    assert ".pypi.org" in rules["suffixes"] and "pypi.org" in rules["hosts"]
    assert body["reload_interval_sec"] == 2.0
    # 보기 전용이다. 고치는 API는 없다.
    put = await logged_in.put("/api/console/settings/http-allowlist", json={"content": "*"})
    assert put.status_code == 405


async def test_edits_on_disk_show_up(logged_in: AsyncClient, settings: Settings) -> None:
    path = settings.allowlist_file
    lines = ["# 주석", "example.com  # 설명", "*", ".Example.ORG", "203.0.113.7", ""]
    path.write_bytes(codecs.BOM_UTF8 + "\n".join(lines).encode())

    rules = (await allowlist(logged_in))["rules"]

    assert rules["allow_any"] is True
    assert sorted(rules["hosts"]) == ["203.0.113.7", "example.com"]
    assert rules["suffixes"] == [".example.org"]

    path.unlink()
    missing = await allowlist(logged_in)
    assert missing["exists"] is False and missing["content"] == ""
    assert missing["size_bytes"] is None and missing["modified_at"] is None
    assert missing["rules"] == {"hosts": [], "suffixes": [], "allow_any": False}


async def test_allowlist_needs_login(client: AsyncClient, account: dict[str, Any]) -> None:
    response = await client.get("/api/console/settings/http-allowlist")

    assert response.status_code == 401
