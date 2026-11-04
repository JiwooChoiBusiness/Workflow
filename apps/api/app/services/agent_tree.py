"""에이전트 버전과 call_agent가 부를 하위 에이전트 버전의 트리(3단계 설계 14.4).

런의 키 등급은 그 런이 닿을 수 있는 지식 베이스·데이터 소스 전부로 정한다. 자식 런만 paid로 두면
자식의 답을 받은 부모(워크플로우의 뒤 llm, 지휘자 에이전트)의 다음 모델 호출이 무료 키로 가서 유료
전용 자료가 무료 등급으로 흘러간다. 그래서 워크플로우는 agent 노드가 부르는 에이전트까지, 에이전트
런은 하위 에이전트까지 따라가 knowledge.kb_ids와 sql_sources를 모으고, 하나라도 유료면 런 전체를
paid로 올린다.

버전 고르기는 자식 런을 만들 때와 같다(발행 버전 우선, 초안 부모만 자식의 초안). 순환은 방문
집합이, 깊이는 MAX_TREE_DEPTH가 끊는다.
"""

import uuid
from collections.abc import Collection, Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.child_runs import VersionKind
from app.db.models import Agent, AgentVersion
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.agents import AgentRepository
from app.llm.key_pool import KeyTier
from app.schemas.agent_version import SubAgentRef
from app.services.run_tier import agent_knowledge, agent_sql_sources, higher, kb_tier, sql_tier

# ORCHESTRATION_MAX_DEPTH(최대 3)보다 하나 더 내려가 본다.
MAX_TREE_DEPTH = 4


def resolve_version(agent: Agent, *, draft_parent: bool) -> tuple[uuid.UUID, VersionKind] | None:
    """자식이 쓸 버전(오케스트레이션 2.4). 발행 버전이 우선이고 초안 부모만 자식의 초안을 쓴다."""
    if agent.published_version_id is not None:
        return agent.published_version_id, "published"
    if draft_parent and agent.draft_version_id is not None:
        return agent.draft_version_id, "draft"
    return None


def _sub_agent_ids(version: AgentVersion) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for raw in version.sub_agents or []:
        try:
            found.add(SubAgentRef.model_validate(raw).agent_id)
        except ValidationError:
            continue
    return found


async def tree_refs(
    session: AsyncSession, workspace_id: uuid.UUID, roots: Sequence[AgentVersion]
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """roots와 그 아래 하위 에이전트 버전들의 (knowledge.kb_ids, sql_sources)."""
    agents = AgentRepository(session, workspace_id)
    versions = AgentVersionRepository(session, workspace_id)
    kb_ids: set[uuid.UUID] = set()
    source_ids: set[uuid.UUID] = set()
    seen: set[uuid.UUID] = set()
    level = list(roots)
    for _ in range(MAX_TREE_DEPTH + 1):
        wanted: dict[uuid.UUID, bool] = {}
        for version in level:
            if version.id in seen:
                continue
            seen.add(version.id)
            kb_ids.update(agent_knowledge(version.knowledge).kb_ids)
            source_ids.update(agent_sql_sources(version.sql_sources))
            draft = version.version_number is None
            for agent_id in _sub_agent_ids(version):
                wanted[agent_id] = wanted.get(agent_id, False) or draft
        if not wanted:
            break
        chosen: list[uuid.UUID] = []
        for agent in await agents.get_many_active(wanted):
            picked = resolve_version(agent, draft_parent=wanted[agent.id])
            if picked is not None and picked[0] not in seen:
                chosen.append(picked[0])
        level = await versions.get_many(chosen) if chosen else []
    return kb_ids, source_ids


async def tree_tier(
    session: AsyncSession, workspace_id: uuid.UUID, roots: Sequence[AgentVersion]
) -> KeyTier:
    kb_ids, source_ids = await tree_refs(session, workspace_id, roots)
    return higher(
        await kb_tier(session, workspace_id, kb_ids),
        await sql_tier(session, workspace_id, source_ids),
    )


async def agents_tier(
    session: AsyncSession, workspace_id: uuid.UUID, agent_ids: Collection[uuid.UUID]
) -> KeyTier:
    """워크플로우 agent 노드가 부르는 에이전트들(발행 버전)과 그 하위 에이전트의 등급."""
    if not agent_ids:
        return "free"
    rows = await AgentRepository(session, workspace_id).get_many_active(agent_ids)
    published = [row.published_version_id for row in rows if row.published_version_id is not None]
    roots = await AgentVersionRepository(session, workspace_id).get_many(published)
    return await tree_tier(session, workspace_id, roots)
