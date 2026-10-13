import hashlib
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.api.console import files as files_router
from app.config import Settings
from app.services import files as files_service
from app.services.files import guess_mime, sanitize_filename

FETCH = {"X-Requested-With": "fetch"}
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "updated_at",
)


async def create_agent(client: AsyncClient, name: str = "Helper") -> Any:
    response = await client.post("/api/console/agents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def upload(
    client: AsyncClient, agent_id: str, name: str, data: bytes, mime: str = "text/plain"
) -> httpx.Response:
    return await client.post(
        f"/api/console/agents/{agent_id}/files",
        files={"file": (name, data, mime)},
        headers=FETCH,
    )


async def set_files(client: AsyncClient, agent_id: str, file_ids: list[str]) -> httpx.Response:
    draft = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["file_ids"] = file_ids
    return await client.put(f"/api/console/agents/{agent_id}/draft", json=body)


async def list_files(client: AsyncClient, agent_id: str) -> list[Any]:
    response = await client.get(f"/api/console/agents/{agent_id}/files")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_upload_stores_file_and_metadata(logged_in: AsyncClient, settings: Settings) -> None:
    agent = await create_agent(logged_in)
    data = "이름,값\n가,1\n".encode()

    response = await upload(logged_in, agent["id"], "데이터.csv", data, "text/csv")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["filename"], body["mime"], body["size_bytes"]) == (
        "데이터.csv",
        "text/csv",
        len(data),
    )
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert (body["in_draft"], body["published_versions"]) == (False, [])
    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    stored = settings.data_dir / "storage" / "agent-files" / workspace_id / body["id"]
    assert stored.read_bytes() == data
    assert await list_files(logged_in, agent["id"]) == [body]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("..\\..\\evil:name?.txt", "evil_name_.txt"),
        ("/etc/passwd", "passwd"),
        ("CON.txt", "_CON.txt"),
        ("trailing. . ", "trailing"),
        ("", "file"),
        ("..", "file"),
        ("한글가.txt", "한글가.txt"),
        ("a" * 120 + ".csv", "a" * 96 + ".csv"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_guess_mime() -> None:
    assert guess_mime("a.csv", "text/csv; charset=utf-8") == "text/csv"
    assert guess_mime("a.json", "application/octet-stream") == "application/json"
    assert guess_mime("a.unknownext", "not a mime") == "application/octet-stream"


async def test_unsafe_names_are_cleaned_on_upload(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)

    response = await upload(logged_in, agent["id"], "..\\..\\nul.txt", b"x")

    assert response.status_code == 201
    assert response.json()["filename"] == "_nul.txt"


async def test_file_size_limits(
    logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(files_service, "MAX_AGENT_FILE_BYTES", 1024)
    monkeypatch.setattr(files_router, "MAX_AGENT_FILE_BYTES", 1024)
    agent = await create_agent(logged_in)

    # 본문 크기 머리글로 먼저 걸러진다.
    by_header = await upload(logged_in, agent["id"], "big.bin", bytes(200 * 1024))
    # 머리글은 통과해도 받으면서 센 크기로 걸러진다.
    by_stream = await upload(logged_in, agent["id"], "mid.bin", bytes(2048))
    fits = await upload(logged_in, agent["id"], "ok.bin", bytes(1024))

    assert by_header.status_code == 413
    assert by_stream.status_code == 413
    assert by_stream.json()["error"]["code"] == "file_too_large"
    assert fits.status_code == 201
    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    folder = settings.data_dir / "storage" / "agent-files" / workspace_id
    assert [p.name for p in folder.iterdir()] == [fits.json()["id"]]


async def test_draft_file_references_and_usage_flags(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    other = await create_agent(logged_in, "Other")
    first = (await upload(logged_in, agent["id"], "data.csv", b"1")).json()
    same_name = (await upload(logged_in, agent["id"], "DATA.csv", b"2")).json()
    foreign = (await upload(logged_in, other["id"], "x.txt", b"3")).json()

    conflict = await set_files(logged_in, agent["id"], [first["id"], same_name["id"]])
    not_mine = await set_files(logged_in, agent["id"], [foreign["id"]])
    ok = await set_files(logged_in, agent["id"], [first["id"]])

    assert conflict.json()["error"]["details"][0]["type"] == "file_name_conflict"
    assert not_mine.json()["error"]["details"][0]["type"] == "file_not_found"
    assert ok.status_code == 200, ok.text
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    flags = {
        item["id"]: (item["in_draft"], item["published_versions"])
        for item in await list_files(logged_in, agent["id"])
    }
    assert flags == {first["id"]: (True, [1]), same_name["id"]: (False, [])}


async def test_delete_refuses_files_in_use(logged_in: AsyncClient, settings: Settings) -> None:
    agent = await create_agent(logged_in)
    used = (await upload(logged_in, agent["id"], "used.txt", b"u")).json()
    spare = (await upload(logged_in, agent["id"], "spare.txt", b"s")).json()
    await set_files(logged_in, agent["id"], [used["id"]])
    base = f"/api/console/agents/{agent['id']}/files"

    in_draft = await logged_in.delete(f"{base}/{used['id']}", headers=FETCH)
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    await set_files(logged_in, agent["id"], [])
    in_published = await logged_in.delete(f"{base}/{used['id']}", headers=FETCH)
    removed = await logged_in.delete(f"{base}/{spare['id']}", headers=FETCH)
    again = await logged_in.delete(f"{base}/{spare['id']}", headers=FETCH)

    assert in_draft.status_code == 409
    assert in_draft.json()["error"]["details"] == {"in_draft": True, "published_versions": []}
    assert in_published.status_code == 409
    assert in_published.json()["error"]["details"] == {
        "in_draft": False,
        "published_versions": [1],
    }
    assert removed.status_code == 204
    assert again.status_code == 404
    assert [item["id"] for item in await list_files(logged_in, agent["id"])] == [used["id"]]
    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    folder = settings.data_dir / "storage" / "agent-files" / workspace_id
    assert sorted(p.name for p in folder.iterdir()) == [used["id"]]


async def test_upload_to_unknown_agent(logged_in: AsyncClient, settings: Settings) -> None:
    missing = "00000000-0000-7000-8000-000000000000"

    response = await upload(logged_in, missing, "a.txt", b"x")

    assert response.status_code == 404
    assert not (settings.data_dir / "storage" / "agent-files").exists()
