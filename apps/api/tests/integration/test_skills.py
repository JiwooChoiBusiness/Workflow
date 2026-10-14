import io
import stat
import uuid
import zipfile
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.sandbox.provisioning import WorkspaceProvisioner
from app.services.agents import AgentService, provision_content
from app.storage import make_storage

SkillZip = Callable[..., bytes]
FETCH = {"X-Requested-With": "fetch"}
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "file_ids",
    "updated_at",
)


def raw_zip(entries: list[tuple[zipfile.ZipInfo | str, bytes | str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    return buffer.getvalue()


async def upload(
    client: AsyncClient, data: bytes, *, agent_id: str | None = None, filename: str = "skill.zip"
) -> httpx.Response:
    path = f"/api/console/agents/{agent_id}/skills" if agent_id else "/api/console/skills"
    return await client.post(
        path, files={"file": (filename, data, "application/zip")}, headers=FETCH
    )


async def created(client: AsyncClient, data: bytes, *, agent_id: str | None = None) -> Any:
    response = await upload(client, data, agent_id=agent_id)
    assert response.status_code == 201, response.text
    return response.json()


async def create_agent(client: AsyncClient, name: str = "Helper") -> Any:
    response = await client.post("/api/console/agents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def set_skills(client: AsyncClient, agent_id: str, version_ids: list[str]) -> httpx.Response:
    draft = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["skill_version_ids"] = version_ids
    return await client.put(f"/api/console/agents/{agent_id}/draft", json=body)


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def test_upload_library_skill(logged_in: AsyncClient, skill_zip: SkillZip) -> None:
    data = skill_zip(files={"scripts/run.py": "print('hi')\n"})

    body = await created(logged_in, data)

    assert (body["scope"], body["slug"], body["name"]) == ("library", "csv-tools", "csv-tools")
    assert body["agent_id"] is None
    assert body["description"] == "CSV 파일을 읽고 요약하는 도구"
    assert body["current_version_number"] == 1
    current = body["current_version"]
    assert current["id"] == body["current_version_id"]
    assert current["manifest"]["files"] == [
        {"path": "SKILL.md", "size": len(current["skill_md"].encode())},
        {"path": "scripts/run.py", "size": 12},
    ]
    assert current["skill_md"].startswith("---\nname: csv-tools")
    assert [v["version_number"] for v in body["versions"]] == [1]
    listed = (await logged_in.get("/api/console/skills")).json()["items"]
    assert [item["id"] for item in listed] == [body["id"]]
    fetched = (await logged_in.get(f"/api/console/skills/{body['id']}")).json()
    assert fetched == body


async def test_package_is_stored_by_version_id(
    logged_in: AsyncClient, skill_zip: SkillZip, settings: Settings
) -> None:
    data = skill_zip(top=None)

    body = await created(logged_in, data)

    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    stored = settings.data_dir / "storage" / "skills" / workspace_id
    assert [p.name for p in stored.iterdir()] == [f"{body['current_version_id']}.zip"]
    assert (stored / f"{body['current_version_id']}.zip").read_bytes() == data
    assert body["current_version"]["size_bytes"] == len(data)


async def test_new_versions_and_name_mismatch(logged_in: AsyncClient, skill_zip: SkillZip) -> None:
    skill = await created(logged_in, skill_zip())
    versions = f"/api/console/skills/{skill['id']}/versions"

    second = await logged_in.post(
        versions,
        files={"file": ("v2.zip", skill_zip(description="새 설명"), "application/zip")},
        headers=FETCH,
    )
    mismatch = await logged_in.post(
        versions,
        files={"file": ("x.zip", skill_zip(name="other-tool"), "application/zip")},
        headers=FETCH,
    )

    assert second.status_code == 201, second.text
    assert second.json()["version_number"] == 2
    assert mismatch.status_code == 422
    assert error_code(mismatch) == "skill_name_mismatch"
    detail = (await logged_in.get(f"/api/console/skills/{skill['id']}")).json()
    assert detail["current_version_number"] == 2
    assert detail["description"] == "새 설명"
    assert [v["version_number"] for v in detail["versions"]] == [2, 1]
    # 목록에도 버전 요약이 있어 예전 버전을 고정한 초안을 상세 조회 없이 풀 수 있다.
    [listed] = (await logged_in.get("/api/console/skills")).json()["items"]
    assert listed["versions"] == detail["versions"]
    assert [v["id"] for v in listed["versions"]] == [
        second.json()["id"],
        skill["current_version_id"],
    ]
    assert set(listed["versions"][0]) == {
        "id",
        "version_number",
        "size_bytes",
        "sha256",
        "created_at",
    }


async def test_duplicate_name_is_a_conflict(logged_in: AsyncClient, skill_zip: SkillZip) -> None:
    first = await created(logged_in, skill_zip())

    again = await upload(logged_in, skill_zip())

    assert again.status_code == 409
    assert error_code(again) == "skill_exists"
    assert again.json()["error"]["details"]["skill_id"] == first["id"]


async def test_delete_hides_skill_and_frees_name(
    logged_in: AsyncClient, skill_zip: SkillZip
) -> None:
    skill = await created(logged_in, skill_zip())
    path = f"/api/console/skills/{skill['id']}"

    assert (await logged_in.delete(path, headers=FETCH)).status_code == 204
    assert (await logged_in.get(path)).status_code == 404
    assert (await logged_in.get("/api/console/skills")).json()["items"] == []
    again = await created(logged_in, skill_zip())
    assert again["id"] != skill["id"]


async def test_rejected_packages(logged_in: AsyncClient, skill_zip: SkillZip) -> None:
    head = "---\nname: csv-tools\ndescription: d\n---\n"
    link = zipfile.ZipInfo("csv-tools/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    cases = {
        "frontmatter_missing": skill_zip(skill_md="# csv-tools\n설명\n"),
        "package_path_invalid": raw_zip(
            [("csv-tools/SKILL.md", head), (zipfile.ZipInfo("csv-tools/../../evil.txt"), "x")]
        ),
        "package_symlink": raw_zip([("csv-tools/SKILL.md", head), (link, "/etc/passwd")]),
        "skill_md_too_large": skill_zip(skill_md=head + "x" * 33 * 1024),
        "skill_md_missing": raw_zip([("csv-tools/readme.md", "x")]),
        "package_invalid": b"not a zip at all",
        "skill_name_invalid": skill_zip(name="CSV Tools"),
    }

    for code, data in cases.items():
        response = await upload(logged_in, data)
        assert response.status_code == 422, (code, response.text)
        assert error_code(response) == code

    reserved = await upload(logged_in, skill_zip(files={"scripts/nul.py": "x"}))
    assert error_code(reserved) == "package_path_invalid"
    big = await upload(logged_in, skill_zip(files={"big.bin": bytes(51 * 1024 * 1024)}))
    assert big.status_code == 413
    assert error_code(big) == "package_too_large"
    assert (await logged_in.get("/api/console/skills")).json()["items"] == []


async def test_rejected_packages_leave_no_files(
    logged_in: AsyncClient, skill_zip: SkillZip, settings: Settings
) -> None:
    await upload(logged_in, skill_zip(skill_md="no frontmatter"))
    await upload(logged_in, b"junk")

    stored = settings.data_dir / "storage" / "skills"
    assert not stored.exists() or [p for p in stored.rglob("*") if p.is_file()] == []


async def test_upload_size_header_is_checked_before_reading(logged_in: AsyncClient) -> None:
    content = b"x" * (50 * 1024 * 1024 + 200 * 1024)

    response = await logged_in.post(
        "/api/console/skills",
        files={"file": ("big.zip", content, "application/zip")},
        headers=FETCH,
    )

    assert response.status_code == 413
    assert error_code(response) == "file_too_large"


async def test_upload_needs_multipart_and_csrf_header(
    logged_in: AsyncClient, skill_zip: SkillZip
) -> None:
    no_header = await logged_in.post(
        "/api/console/skills", files={"file": ("s.zip", skill_zip(), "application/zip")}
    )
    as_json = await logged_in.post("/api/console/skills", json={"file": "x"})
    no_file = await logged_in.post(
        "/api/console/skills", files={"other": ("s.zip", b"x", "application/zip")}, headers=FETCH
    )

    assert no_header.status_code == 415
    assert as_json.status_code == 415
    assert error_code(as_json) == "multipart_required"
    assert no_file.status_code == 422


async def test_skill_file_contents(logged_in: AsyncClient, skill_zip: SkillZip) -> None:
    skill = await created(
        logged_in,
        skill_zip(files={"scripts/run.py": "print('한글')\n", "img/logo.png": b"\x89PNG\x00\x01"}),
    )
    base = f"/api/console/skills/{skill['id']}/versions/{skill['current_version_id']}/files"

    text = await logged_in.get(f"{base}/scripts/run.py")
    binary = await logged_in.get(f"{base}/img/logo.png")
    skill_md = await logged_in.get(f"{base}/SKILL.md")
    # %2F는 라우터가 푼 뒤 path 매개변수로 들어온다. 이름을 정확히 비교하므로 밖으로 못 나간다.
    escape = await logged_in.get(f"{base}/scripts%2F..%2F..%2Fcsv-tools%2FSKILL.md")
    missing = await logged_in.get(f"{base}/nope.txt")

    assert text.status_code == 200
    assert text.text == "print('한글')\n"
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.headers["x-content-type-options"] == "nosniff"
    assert binary.content == b"\x89PNG\x00\x01"
    assert binary.headers["content-type"] == "application/octet-stream"
    assert binary.headers["content-disposition"].startswith("attachment;")
    assert skill_md.text.startswith("---\nname: csv-tools")
    assert escape.status_code == 404
    assert escape.json()["error"]["message"] == "파일을(를) 찾을 수 없다"
    assert missing.status_code == 404


async def test_embedded_skills_belong_to_one_agent(
    logged_in: AsyncClient, skill_zip: SkillZip
) -> None:
    agent = await create_agent(logged_in)
    other = await create_agent(logged_in, "Other")
    library = await created(logged_in, skill_zip())

    embedded = await created(logged_in, skill_zip(), agent_id=agent["id"])
    same_name_elsewhere = await created(logged_in, skill_zip(), agent_id=other["id"])
    duplicate = await upload(logged_in, skill_zip(), agent_id=agent["id"])

    assert (embedded["scope"], embedded["agent_id"]) == ("embedded", agent["id"])
    assert same_name_elsewhere["agent_id"] == other["id"]
    assert duplicate.status_code == 409
    listed = (await logged_in.get(f"/api/console/agents/{agent['id']}/skills")).json()["items"]
    assert [item["id"] for item in listed] == [embedded["id"]]
    assert [v["id"] for v in listed[0]["versions"]] == [embedded["current_version_id"]]
    library_list = (await logged_in.get("/api/console/skills")).json()["items"]
    assert [item["id"] for item in library_list] == [library["id"]]

    # 다른 에이전트의 임베디드 스킬은 초안에 넣을 수 없다.
    response = await set_skills(logged_in, agent["id"], [same_name_elsewhere["current_version_id"]])
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["type"] == "skill_version_not_found"

    # 에이전트를 지우면 그 임베디드 스킬도 보이지 않는다.
    await logged_in.delete(f"/api/console/agents/{agent['id']}", headers=FETCH)
    assert (await logged_in.get(f"/api/console/skills/{embedded['id']}")).status_code == 404
    gone = await upload(logged_in, skill_zip(name="x"), agent_id=agent["id"])
    assert gone.status_code == 404


async def test_draft_skill_references_are_checked(
    logged_in: AsyncClient, skill_zip: SkillZip
) -> None:
    agent = await create_agent(logged_in)
    skill = await created(logged_in, skill_zip())
    v2 = (
        await logged_in.post(
            f"/api/console/skills/{skill['id']}/versions",
            files={"file": ("v2.zip", skill_zip(), "application/zip")},
            headers=FETCH,
        )
    ).json()
    removed = await created(logged_in, skill_zip(name="old-tool"))
    await logged_in.delete(f"/api/console/skills/{removed['id']}", headers=FETCH)

    ok = await set_skills(logged_in, agent["id"], [v2["id"]])
    both = await set_skills(logged_in, agent["id"], [skill["current_version_id"], v2["id"]])
    deleted = await set_skills(logged_in, agent["id"], [removed["current_version_id"]])
    unknown = await set_skills(logged_in, agent["id"], [str(uuid.uuid4())])

    assert ok.status_code == 200, ok.text
    assert ok.json()["skill_version_ids"] == [v2["id"]]
    assert both.json()["error"]["details"][0]["type"] == "skill_duplicate"
    assert deleted.json()["error"]["details"][0]["type"] == "skill_deleted"
    assert unknown.json()["error"]["details"][0]["type"] == "skill_version_not_found"


async def test_twenty_library_and_twenty_embedded_skills_per_agent(
    logged_in: AsyncClient, skill_zip: SkillZip
) -> None:
    agent = await create_agent(logged_in)
    library = [
        (await created(logged_in, skill_zip(name=f"lib-{i}")))["current_version_id"]
        for i in range(21)
    ]
    embedded = [
        (await created(logged_in, skill_zip(name=f"emb-{i}"), agent_id=agent["id"]))[
            "current_version_id"
        ]
        for i in range(20)
    ]

    over = await upload(logged_in, skill_zip(name="emb-20"), agent_id=agent["id"])
    assert over.status_code == 409
    assert error_code(over) == "skill_limit_reached"

    too_many = await set_skills(logged_in, agent["id"], library)
    assert too_many.status_code == 422
    assert [d["type"] for d in too_many.json()["error"]["details"]] == ["too_many_library_skills"]

    full = await set_skills(logged_in, agent["id"], library[:20] + embedded)
    assert full.status_code == 200, full.text
    assert len(full.json()["skill_version_ids"]) == 40


async def test_runtime_version_provisions_files_and_skills(
    logged_in: AsyncClient, app: FastAPI, skill_zip: SkillZip, settings: Settings
) -> None:
    agent = await create_agent(logged_in)
    library = await created(
        logged_in, skill_zip(description="라이브러리판", files={"lib.txt": "library"})
    )
    embedded = await created(
        logged_in,
        skill_zip(description="임베디드판", files={"emb.txt": "embedded"}),
        agent_id=agent["id"],
    )
    other = await created(logged_in, skill_zip(name="pdf-tools", top=None))
    uploaded = await logged_in.post(
        f"/api/console/agents/{agent['id']}/files",
        files={"file": ("참고 자료.txt", "참고".encode(), "text/plain")},
        headers=FETCH,
    )
    assert uploaded.status_code == 201, uploaded.text
    draft = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["skill_version_ids"] = [
        library["current_version_id"],
        other["current_version_id"],
        embedded["current_version_id"],
    ]
    body["file_ids"] = [uploaded.json()["id"]]
    saved = await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text

    workspace_id = uuid.UUID(
        (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    )
    runtime = await AgentService(app.state.db, settings).load_runtime_version(
        workspace_id, uuid.UUID(draft["id"])
    )

    # 이름이 같으면 임베디드 스킬이 이긴다(설계 4.8).
    assert [(s.slug, s.scope, s.description) for s in runtime.skills] == [
        ("csv-tools", "embedded", "임베디드판"),
        ("pdf-tools", "library", "CSV 파일을 읽고 요약하는 도구"),
    ]
    assert runtime.skills[0].path == "skills/csv-tools/SKILL.md"
    assert [f.filename for f in runtime.files] == ["참고 자료.txt"]

    result = await WorkspaceProvisioner(app.state.sandbox).ensure(
        uuid.uuid4(),
        instance_created_at=None,
        content=provision_content(make_storage(settings), runtime),
    )
    root = result.handle.root
    try:
        assert (result.agent_files, result.skills) == (1, 2)
        assert (root / "agent-files" / "참고 자료.txt").read_text(encoding="utf-8") == "참고"
        assert (root / "skills" / "csv-tools" / "emb.txt").read_text() == "embedded"
        assert not (root / "skills" / "csv-tools" / "lib.txt").exists()
        assert (root / "skills" / "pdf-tools" / "SKILL.md").is_file()
    finally:
        await app.state.sandbox.release(result.handle, delete=True)
