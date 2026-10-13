import base64
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.db.engine import Database
from app.db.models import AgentVersion
from app.llm import make_adapter
from app.services.agents import AgentService
from app.services.llm_status import LlmStatusService

FETCH = {"X-Requested-With": "fetch"}
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
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
def settings(settings: Settings) -> Settings:
    # 환경변수 값을 암호화하려면 ENCRYPTION_KEY가 있어야 한다.
    return settings.model_copy(update={"encryption_key": SecretStr(ENCRYPTION_KEY)})


async def create_agent(client: AsyncClient, name: str = "Data Helper", **fields: Any) -> Any:
    response = await client.post("/api/console/agents", json={"name": name, **fields})
    assert response.status_code == 201, response.text
    return response.json()


async def get_draft(client: AsyncClient, agent_id: str) -> Any:
    response = await client.get(f"/api/console/agents/{agent_id}/draft")
    assert response.status_code == 200, response.text
    return response.json()


def body_from(draft: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """GET으로 받은 초안을 PUT 본문으로. 값이 없는 환경변수는 저장된 값을 그대로 둔다."""
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["env_vars"] = [
        {"name": item["name"], "secret": item["secret"]} for item in draft["env_vars"]
    ]
    body.update(changes)
    return body


async def save(client: AsyncClient, agent_id: str, body: dict[str, Any]) -> httpx.Response:
    return await client.put(f"/api/console/agents/{agent_id}/draft", json=body)


def problem_types(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    return {
        ".".join(str(part) for part in item["loc"][1:]): item["type"] for item in error["details"]
    }


async def test_agent_api_requires_login(client: AsyncClient, account: dict[str, Any]) -> None:
    assert (await client.get("/api/console/agents")).status_code == 401
    response = await client.post("/api/console/agents", json={"name": "x"})
    assert response.status_code == 401


async def test_create_list_and_get(logged_in: AsyncClient) -> None:
    created = await create_agent(logged_in, "Data Helper", icon="📊", description="CSV 정리")

    assert created["slug"] == "data-helper"
    assert created["published_version_id"] is None
    assert created["draft_version_id"] is not None
    listed = (await logged_in.get("/api/console/agents")).json()["items"]
    assert [agent["id"] for agent in listed] == [created["id"]]
    fetched = (await logged_in.get(f"/api/console/agents/{created['id']}")).json()
    assert fetched == created


async def test_slugs_are_generated_and_unique(logged_in: AsyncClient) -> None:
    first = await create_agent(logged_in, "데이터 분석가")
    second = await create_agent(logged_in, "데이터 정리")
    third = await create_agent(logged_in, "Data Helper!")
    fourth = await create_agent(logged_in, "data   helper")

    assert (first["slug"], second["slug"]) == ("agent", "agent-2")
    assert (third["slug"], fourth["slug"]) == ("data-helper", "data-helper-2")
    duplicate = await logged_in.post(
        "/api/console/agents", json={"name": "x", "slug": first["slug"]}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "slug_taken"
    invalid = await logged_in.post("/api/console/agents", json={"name": "x", "slug": "Bad Slug"})
    assert invalid.status_code == 422


async def test_new_draft_uses_workspace_defaults(logged_in: AsyncClient) -> None:
    await logged_in.patch(
        "/api/console/workspace",
        json={
            "settings": {
                "approval_mode_default": "http_writes",
                "default_limits": {"max_steps": 12},
            }
        },
    )
    agent = await create_agent(logged_in)

    draft = await get_draft(logged_in, agent["id"])

    assert draft["status"] == "draft"
    assert draft["version_number"] is None
    assert draft["approval_mode"] == "http_writes"
    assert draft["limits"]["max_steps"] == 12
    assert draft["tools"] == ["shell", "files", "http", "ask_user"]
    assert draft["model"] == {
        "model_id": None,
        "params": {
            "temperature": None,
            "top_p": None,
            "max_output_tokens": 16000,
            "reasoning_effort": None,
        },
    }
    assert draft["env_vars"] == []


async def test_save_draft_with_optimistic_lock(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    saved = await save(
        logged_in,
        agent["id"],
        body_from(draft, instructions="CSV를 정리한다", tools=["files", "shell"]),
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["instructions"] == "CSV를 정리한다"
    assert body["tools"] == ["files", "shell"]
    assert body["updated_at"] != draft["updated_at"]
    assert await get_draft(logged_in, agent["id"]) == body

    stale = await save(logged_in, agent["id"], body_from(draft, instructions="덮어쓰기"))
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "draft_conflict"
    assert error["details"]["updated_at"]
    assert (await get_draft(logged_in, agent["id"]))["instructions"] == "CSV를 정리한다"

    # 바로 이어서 두 번 저장해도 잠금 값은 매번 바뀐다.
    again = await save(logged_in, agent["id"], body_from(body))
    assert again.status_code == 200
    assert again.json()["updated_at"] != body["updated_at"]


async def test_save_draft_requires_lock_value(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    body = body_from(await get_draft(logged_in, agent["id"]))
    del body["updated_at"]

    response = await save(logged_in, agent["id"], body)

    assert problem_types(response) == {"updated_at": "missing"}


async def test_approval_mode_and_tool_groups_are_checked(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    response = await save(
        logged_in, agent["id"], body_from(draft, approval_mode="never", tools=["browser"])
    )

    types = problem_types(response)
    assert set(types) == {"approval_mode", "tools.0"}
    for mode in ("off", "http_writes", "all_shell"):
        draft = await get_draft(logged_in, agent["id"])
        ok = await save(logged_in, agent["id"], body_from(draft, approval_mode=mode))
        assert ok.status_code == 200
        assert ok.json()["approval_mode"] == mode


async def test_sandbox_limits_cannot_exceed_global_settings(
    logged_in: AsyncClient, settings: Settings
) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])
    too_much = {
        "memory_limit_mb": settings.sandbox_memory_limit_mb + 1,
        "max_processes": settings.sandbox_max_processes + 1,
    }

    response = await save(logged_in, agent["id"], body_from(draft, sandbox=too_much))

    assert problem_types(response) == {
        "sandbox.memory_limit_mb": "memory_limit_exceeded",
        "sandbox.max_processes": "max_processes_exceeded",
    }
    details = response.json()["error"]["details"]
    assert details[0]["ctx"] == {"max": settings.sandbox_memory_limit_mb}
    within = {"memory_limit_mb": settings.sandbox_memory_limit_mb, "max_processes": 8}
    ok = await save(logged_in, agent["id"], body_from(draft, sandbox=within))
    assert ok.status_code == 200
    assert ok.json()["sandbox"] == within


def model(model_id: str | None = None, **params: Any) -> dict[str, Any]:
    return {"model_id": model_id, "params": {"max_output_tokens": 16000, **params}}


async def test_reasoning_effort_follows_the_catalog(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    # 3.8은 minimal을 받지 않는다. model_id가 없으면 폴백 목록의 첫 모델(3.8)로 본다.
    for model_id in ("gemini-3.8-flash", None):
        response = await save(
            logged_in,
            agent["id"],
            body_from(draft, model=model(model_id, reasoning_effort="minimal")),
        )
        types = problem_types(response)
        assert types == {"model.params.reasoning_effort": "reasoning_effort_unsupported"}
        ctx = response.json()["error"]["details"][0]["ctx"]
        assert ctx == {"model": "gemini-3.8-flash", "allowed": ["low", "medium", "high"]}

    ok = await save(
        logged_in,
        agent["id"],
        body_from(draft, model=model("gemini-3.6-flash", reasoning_effort="minimal")),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["model"]["params"]["reasoning_effort"] == "minimal"


async def test_max_output_tokens_follows_the_catalog(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    response = await save(
        logged_in, agent["id"], body_from(draft, model=model(max_output_tokens=65_537))
    )

    assert problem_types(response) == {
        "model.params.max_output_tokens": "max_output_tokens_exceeded"
    }


async def test_model_id_without_live_list_allows_only_fallback(
    logged_in: AsyncClient, settings: Settings
) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    unknown = await save(logged_in, agent["id"], body_from(draft, model=model("gemini-9-ultra")))

    assert problem_types(unknown) == {"model.model_id": "model_not_available"}
    ctx = unknown.json()["error"]["details"][0]["ctx"]
    assert ctx == {"allowed": settings.gemini_model_fallback, "live_list": False}
    fallback = await save(logged_in, agent["id"], body_from(draft, model=model("gemini-3.7-flash")))
    assert fallback.status_code == 200
    draft = fallback.json()
    explicit = await save(
        logged_in,
        agent["id"],
        body_from(draft, model=model("gemini-9-ultra"), allow_unlisted_model=True),
    )
    assert explicit.status_code == 200
    assert explicit.json()["model"]["model_id"] == "gemini-9-ultra"
    # 허용 플래그는 저장하지 않는 요청 옵션이다.
    assert "allow_unlisted_model" not in explicit.json()


async def test_model_id_from_live_list(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    live = ["models/gemini-3.8-flash", "models/gemini-2.5-pro"]
    app.state.llm_status = LlmStatusService(
        settings,
        make_adapter(settings),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": i} for i in live]})
        ),
    )
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    before = await save(logged_in, agent["id"], body_from(draft, model=model("gemini-2.5-pro")))
    assert problem_types(before) == {"model.model_id": "model_not_available"}
    # 모델 선택 화면이 /models를 부르면 라이브 목록이 생긴다.
    assert (await logged_in.get("/api/console/models")).json()["live_ok"] is True
    after = await save(logged_in, agent["id"], body_from(draft, model=model("gemini-2.5-pro")))
    assert after.status_code == 200, after.text
    missing = await save(
        logged_in, agent["id"], body_from(after.json(), model=model("gemini-9-ultra"))
    )
    assert missing.json()["error"]["details"][0]["ctx"]["live_list"] is True


async def test_problems_are_reported_together(logged_in: AsyncClient, settings: Settings) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])

    response = await save(
        logged_in,
        agent["id"],
        body_from(
            draft,
            model=model("gemini-9-ultra", reasoning_effort="high"),
            sandbox={"memory_limit_mb": settings.sandbox_memory_limit_mb * 2},
            env_vars=[{"name": "NEW_VAR"}],
        ),
    )

    assert problem_types(response) == {
        "sandbox.memory_limit_mb": "memory_limit_exceeded",
        "model.model_id": "model_not_available",
        "env_vars.0.value": "env_value_required",
    }
    assert (await get_draft(logged_in, agent["id"]))["updated_at"] == draft["updated_at"]


async def test_env_vars_are_encrypted_and_secrets_hidden(
    logged_in: AsyncClient, db: Database
) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])
    env = [
        {"name": "API_TOKEN", "value": "tok-123456", "secret": True},
        {"name": "MODE", "value": "fast"},
    ]

    saved = await save(logged_in, agent["id"], body_from(draft, env_vars=env))

    assert saved.status_code == 200, saved.text
    assert saved.json()["env_vars"] == [
        {"name": "API_TOKEN", "secret": True, "value": None},
        {"name": "MODE", "secret": False, "value": "fast"},
    ]
    assert "tok-123456" not in saved.text
    async with db.read() as session:
        stored = (
            await session.scalars(
                select(AgentVersion.env_vars).where(AgentVersion.id == draft["id"])
            )
        ).one()
    assert all(item["value_encrypted"].startswith("v1|") for item in stored)
    assert "tok-123456" not in str(stored) and "fast" not in str(stored)

    # 값을 보내지 않으면 저장된 비밀값을 그대로 둔다.
    kept = await save(logged_in, agent["id"], body_from(saved.json(), instructions="그대로"))
    assert kept.status_code == 200
    async with db.read() as session:
        after = (
            await session.scalars(
                select(AgentVersion.env_vars).where(AgentVersion.id == draft["id"])
            )
        ).one()
    assert after[0]["value_encrypted"] == stored[0]["value_encrypted"]

    # 비밀값을 일반 값으로 바꾸려면 값을 다시 받는다.
    reveal = body_from(kept.json())
    reveal["env_vars"][0]["secret"] = False
    assert problem_types(await save(logged_in, agent["id"], reveal)) == {
        "env_vars.0.value": "env_value_required"
    }


async def test_env_vars_need_encryption_key(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])
    app.state.settings = settings.model_copy(update={"encryption_key": None})

    response = await save(
        logged_in, agent["id"], body_from(draft, env_vars=[{"name": "A", "value": "b"}])
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "encryption_key_missing"
    # 환경변수가 없는 초안은 키 없이도 저장한다.
    assert (await save(logged_in, agent["id"], body_from(draft))).status_code == 200


async def test_publish_copies_the_draft_and_rollback_switches_versions(
    logged_in: AsyncClient,
) -> None:
    agent = await create_agent(logged_in)
    agent_id = agent["id"]
    draft = await get_draft(logged_in, agent_id)
    await save(logged_in, agent_id, body_from(draft, instructions="첫 지시문"))

    first = await logged_in.post(f"/api/console/agents/{agent_id}/publish", json={})
    assert first.status_code == 201, first.text
    v1 = first.json()["version"]
    assert (v1["status"], v1["version_number"], v1["instructions"]) == ("published", 1, "첫 지시문")
    assert v1["published_at"] is not None and v1["snapshot_id"] is None
    assert first.json()["agent"]["published_version_id"] == v1["id"]
    assert first.json()["agent"]["published_version_number"] == 1

    draft = await get_draft(logged_in, agent_id)
    await save(logged_in, agent_id, body_from(draft, instructions="둘째 지시문"))
    second = await logged_in.post(
        f"/api/console/agents/{agent_id}/publish", json={"change_note": "지시문 수정"}
    )
    v2 = second.json()["version"]
    assert (v2["version_number"], v2["change_note"]) == (2, "지시문 수정")

    # 발행 버전은 불변이다. 초안을 고쳐도 v1은 그대로다.
    detail = (await logged_in.get(f"/api/console/agents/{agent_id}/versions/{v1['id']}")).json()
    assert detail["instructions"] == "첫 지시문"
    versions = (await logged_in.get(f"/api/console/agents/{agent_id}/versions")).json()["items"]
    assert [(v["version_number"], v["is_current"]) for v in versions] == [(2, True), (1, False)]

    restored = await logged_in.post(
        f"/api/console/agents/{agent_id}/versions/{v1['id']}/restore", json={}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["published_version_id"] == v1["id"]
    assert restored.json()["published_version_number"] == 1
    versions = (await logged_in.get(f"/api/console/agents/{agent_id}/versions")).json()["items"]
    assert [(v["version_number"], v["is_current"]) for v in versions] == [(2, False), (1, True)]
    # 롤백은 초안을 건드리지 않는다.
    assert (await get_draft(logged_in, agent_id))["instructions"] == "둘째 지시문"

    third = await logged_in.post(f"/api/console/agents/{agent_id}/publish", json={})
    assert third.json()["version"]["version_number"] == 3


async def test_restore_rejects_draft_and_unknown_versions(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)
    other = await create_agent(logged_in, "Other")
    published = (
        await logged_in.post(f"/api/console/agents/{other['id']}/publish", json={})
    ).json()["version"]
    base = f"/api/console/agents/{agent['id']}/versions"

    draft = await logged_in.post(f"{base}/{agent['draft_version_id']}/restore", json={})
    foreign = await logged_in.post(f"{base}/{published['id']}/restore", json={})

    assert draft.status_code == 409
    assert draft.json()["error"]["code"] == "version_not_published"
    assert foreign.status_code == 404


async def test_published_env_vars_survive_later_draft_edits(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    agent = await create_agent(logged_in)
    draft = await get_draft(logged_in, agent["id"])
    await save(
        logged_in,
        agent["id"],
        body_from(
            draft, env_vars=[{"name": "TOKEN", "value": "first-secret-7f3a", "secret": True}]
        ),
    )
    v1 = (await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})).json()
    draft = await get_draft(logged_in, agent["id"])
    await save(
        logged_in,
        agent["id"],
        body_from(
            draft, env_vars=[{"name": "TOKEN", "value": "second-secret-9b1c", "secret": True}]
        ),
    )

    service = AgentService(app.state.db, app.state.settings)
    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    runtime = await service.load_runtime_version(
        uuid.UUID(workspace_id), uuid.UUID(v1["version"]["id"])
    )

    assert runtime.env == {"TOKEN": "first-secret-7f3a"}
    assert runtime.version_number == 1
    # 복호화한 값은 repr(로그)에 나오지 않는다.
    assert "first-secret-7f3a" not in repr(runtime)


async def test_patch_agent_meta(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in, icon="🤖")
    await create_agent(logged_in, "Taken", slug="taken")
    path = f"/api/console/agents/{agent['id']}"

    patched = await logged_in.patch(
        path,
        json={"name": "새 이름", "description": "설명", "webapp_enabled": True, "slug": "fresh"},
    )
    cleared = await logged_in.patch(path, json={"icon": None})
    untouched = await logged_in.patch(path, json={})
    conflict = await logged_in.patch(path, json={"slug": "taken"})

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["name"], body["description"], body["webapp_enabled"], body["slug"]) == (
        "새 이름",
        "설명",
        True,
        "fresh",
    )
    assert body["icon"] == "🤖"
    assert cleared.json()["icon"] is None
    assert untouched.json()["name"] == "새 이름"
    assert conflict.status_code == 409


async def test_delete_hides_agent_and_frees_slug(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in, "Helper", slug="helper")
    path = f"/api/console/agents/{agent['id']}"

    deleted = await logged_in.delete(path, headers=FETCH)

    assert deleted.status_code == 204
    assert (await logged_in.get(path)).status_code == 404
    assert (await logged_in.get(f"{path}/draft")).status_code == 404
    assert (await logged_in.delete(path, headers=FETCH)).status_code == 404
    assert (await logged_in.get("/api/console/agents")).json()["items"] == []
    again = await create_agent(logged_in, "Helper", slug="helper")
    assert again["id"] != agent["id"]
    # 지운 행과 새 행이 같은 slug를 쓴다. 살아 있는 행끼리만 유일하다.
    dup = await logged_in.post("/api/console/agents", json={"name": "x", "slug": "helper"})
    assert dup.status_code == 409


async def test_delete_requires_csrf_header(logged_in: AsyncClient) -> None:
    agent = await create_agent(logged_in)

    response = await logged_in.delete(f"/api/console/agents/{agent['id']}")

    assert response.status_code == 415


async def test_unknown_agent_is_404(logged_in: AsyncClient) -> None:
    missing = "/api/console/agents/00000000-0000-7000-8000-000000000000"

    for response in (
        await logged_in.get(missing),
        await logged_in.get(f"{missing}/draft"),
        await logged_in.post(f"{missing}/publish", json={}),
        await logged_in.get(f"{missing}/versions"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
