"""하위 에이전트 저장·발행 검사와 경고(오케스트레이션 2.3, O-A5)."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.schemas.agent import AgentCreate
from app.schemas.agent_version import AgentVersionOut, DraftUpdate, ResultSpec
from app.services.agents import AgentService
from app.services.errors import ServiceError

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "additionalProperties": False,
}


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[AgentService, uuid.UUID]]:
    db = Database(tmp_path / "agents.db")
    await upgrade_head(db)
    async with db.write() as session:
        workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
        workspace.id = new_id()
        workspace_id = workspace.id
    try:
        yield (
            AgentService(db, Settings.model_validate({"data_dir": tmp_path / "data"})),
            workspace_id,
        )
    finally:
        await db.close()


async def agent(
    pair: tuple[AgentService, uuid.UUID], slug: str, description: str = "설명"
) -> uuid.UUID:
    agents, workspace_id = pair
    created = await agents.create_agent(
        workspace_id, AgentCreate(name=f"{slug} 이름", slug=slug, description=description)
    )
    return created.id


async def save(
    pair: tuple[AgentService, uuid.UUID], agent_id: uuid.UUID, **changes: Any
) -> AgentVersionOut:
    agents, workspace_id = pair
    draft = await agents.get_draft(workspace_id, agent_id)
    body = draft.model_dump(
        include={
            "instructions",
            "model",
            "tools",
            "limits",
            "sandbox",
            "approval_mode",
            "skill_version_ids",
            "file_ids",
            "updated_at",
        },
        by_alias=True,
    )
    body.update(changes)
    return await agents.save_draft(workspace_id, agent_id, DraftUpdate.model_validate(body))


def problem_types(exc: pytest.ExceptionInfo[ServiceError]) -> list[str]:
    return [item["type"] for item in exc.value.details]


async def test_self_reference_cycle_and_missing_agent_are_rejected(
    service: tuple[AgentService, uuid.UUID],
) -> None:
    a = await agent(service, "a")
    b = await agent(service, "b")
    await save(service, a, sub_agents=[{"agent_id": str(b)}])

    with pytest.raises(ServiceError) as self_ref:
        await save(service, a, sub_agents=[{"agent_id": str(a)}])
    with pytest.raises(ServiceError) as cycle:
        await save(service, b, sub_agents=[{"agent_id": str(a)}])
    with pytest.raises(ServiceError) as missing:
        await save(service, a, sub_agents=[{"agent_id": str(uuid.uuid4())}])

    assert self_ref.value.status == 422 and problem_types(self_ref) == ["agent_ref_self"]
    assert problem_types(cycle) == ["agent_ref_cycle"]
    [item] = cycle.value.details
    assert item["loc"] == ["body", "sub_agents", 0, "agent_id"]
    assert item["ctx"]["path"] == ["b", "a", "b"]
    assert problem_types(missing) == ["agent_ref_not_found"]


async def test_warnings_for_empty_description_unpublished_and_deep_chains(
    service: tuple[AgentService, uuid.UUID],
) -> None:
    a = await agent(service, "a")
    b = await agent(service, "b", description="")
    c = await agent(service, "c")
    d = await agent(service, "d")
    await save(service, c, sub_agents=[{"agent_id": str(d)}])
    await save(service, b, sub_agents=[{"agent_id": str(c)}])

    saved = await save(service, a, sub_agents=[{"agent_id": str(b)}])

    codes = [(item.code, item.agent_id) for item in saved.warnings]
    assert ("agent_description_empty", b) in codes
    assert ("agent_unpublished", b) in codes
    # a → b → c → d는 깊이 3이라 기본 한도 2를 넘는다.
    assert ("chain_too_deep", None) in codes
    assert saved.sub_agents[0].agent_id == b
    # 경고는 저장 응답에만 있다.
    agents, workspace_id = service
    assert (await agents.get_draft(workspace_id, a)).warnings == []


async def test_result_schema_is_checked(service: tuple[AgentService, uuid.UUID]) -> None:
    a = await agent(service, "a")
    b = await agent(service, "b")

    with pytest.raises(ServiceError) as bad:
        await save(
            service,
            a,
            sub_agents=[
                {"agent_id": str(b), "result": {"format": "json", "schema": {"type": "x"}}}
            ],
        )
    with pytest.raises(ValidationError):
        ResultSpec.model_validate({"format": "text", "schema": SCHEMA})
    saved = await save(
        service,
        a,
        sub_agents=[{"agent_id": str(b), "result": {"format": "json", "schema": SCHEMA}}],
    )

    assert problem_types(bad) == ["output_schema_invalid"]
    assert bad.value.details[0]["loc"] == ["body", "sub_agents", 0, "result", "schema"]
    dumped = saved.model_dump(mode="json", by_alias=True)
    assert dumped["sub_agents"][0]["result"] == {"format": "json", "schema": SCHEMA, "files": True}


async def test_saving_without_the_field_keeps_the_list(
    service: tuple[AgentService, uuid.UUID],
) -> None:
    a = await agent(service, "a")
    b = await agent(service, "b")
    await save(service, a, sub_agents=[{"agent_id": str(b), "approval_mode": "off"}])

    # 하위 에이전트를 모르는 화면의 저장(필드 없음)은 목록을 지우지 않는다.
    kept = await save(service, a, instructions="바꿨다")
    cleared = await save(service, a, sub_agents=[])

    assert [item.agent_id for item in kept.sub_agents] == [b]
    assert kept.sub_agents[0].approval_mode == "off"
    assert cleared.sub_agents == []


async def test_publish_needs_published_sub_agents_and_copies_the_list(
    service: tuple[AgentService, uuid.UUID],
) -> None:
    agents, workspace_id = service
    a = await agent(service, "a")
    b = await agent(service, "b", description="")
    await save(service, a, sub_agents=[{"agent_id": str(b)}])

    with pytest.raises(ServiceError) as blocked:
        await agents.publish(workspace_id, a)
    await agents.publish(workspace_id, b)
    published = await agents.publish(workspace_id, a)

    assert blocked.value.code == "draft_invalid"
    [item] = blocked.value.details
    assert item["type"] == "agent_ref_unpublished"
    assert item["loc"] == ["draft", "sub_agents", 0, "agent_id"]
    assert "먼저 발행한다" in item["msg"]
    assert [warning.code for warning in published.warnings] == ["agent_description_empty"]
    assert [ref.agent_id for ref in published.version.sub_agents] == [b]
