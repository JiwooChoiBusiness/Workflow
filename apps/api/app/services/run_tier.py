"""런의 키 등급(3단계 설계 14.4).

런의 등급은 만들 때 정해 run.limits·wf_run.limits의 tier에 남긴다. 워크플로우는 그래프가 가리키는
지식 베이스·데이터 소스와 agent 노드가 부르는 에이전트 트리, 에이전트는 버전과 하위 에이전트 트리의
knowledge.kb_ids·sql_sources 중 하나라도 key_tier=paid면 paid다(트리는 agent_tree.py). 자식 과업은
부모 런의 등급과 자기 트리의 등급 중 높은 쪽이다. 실행기는 런을 시작·재개할 때 다시 계산해
올리기만 한다. paid인데 유료 키(GEMINI_PAID_API_KEY)가 없으면 런을 만들지 않는다(paid_key_missing).

run.limits는 RunLimits(extra=forbid)로 읽으므로 tier는 읽기 전에 떼어 낸다(run_limits_of).
"""

import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import SqlSource
from app.db.repositories.knowledge import KnowledgeBaseRepository
from app.db.repositories.sql_sources import SqlSourceRepository
from app.llm.key_pool import KeyTier
from app.schemas.agent_version import AgentKnowledge
from app.schemas.common import RunLimits
from app.services.errors import ServiceError
from app.services.knowledge_runtime import key_tier_of

TIER_KEY = "tier"
ERR_PAID_KEY_MISSING = "paid_key_missing"


def higher(first: KeyTier, second: KeyTier) -> KeyTier:
    return "paid" if "paid" in (first, second) else "free"


def tier_of(limits: Mapping[str, Any] | None) -> KeyTier:
    """런 행의 limits에 남긴 등급. 3단계 전에 만든 런은 free다."""
    return "paid" if (limits or {}).get(TIER_KEY) == "paid" else "free"


def run_limits_of(limits: Mapping[str, Any] | None) -> RunLimits:
    """에이전트 런 행의 limits를 RunLimits로. tier는 RunLimits의 필드가 아니라 뺀다."""
    raw = {key: value for key, value in (limits or {}).items() if key != TIER_KEY}
    return RunLimits.model_validate(raw)


def with_tier(limits: Mapping[str, Any], tier: KeyTier) -> dict[str, Any]:
    return {**limits, TIER_KEY: tier}


def paid_key_configured(settings: Settings) -> bool:
    secret = settings.gemini_paid_api_key
    return secret is not None and bool(secret.get_secret_value().strip())


def paid_key_missing() -> ServiceError:
    return ServiceError(
        422,
        ERR_PAID_KEY_MISSING,
        "유료 키 전용 지식 베이스나 데이터 소스를 쓰는 런이라 유료 키(GEMINI_PAID_API_KEY)가 "
        "있어야 한다",
    )


def require_paid_key(settings: Settings, tier: KeyTier) -> None:
    if tier == "paid" and not paid_key_configured(settings):
        raise paid_key_missing()


async def kb_tier(
    session: AsyncSession, workspace_id: uuid.UUID, kb_ids: Collection[uuid.UUID]
) -> KeyTier:
    """살아 있는 지식 베이스 중 하나라도 유료면 paid. 지운 것은 보지 않는다."""
    if not kb_ids:
        return "free"
    rows = await KnowledgeBaseRepository(session, workspace_id).get_many_active(kb_ids)
    return "paid" if any(key_tier_of(row) == "paid" for row in rows) else "free"


def agent_knowledge(raw: Mapping[str, Any] | None) -> AgentKnowledge:
    """agent_version.knowledge 열. 깨졌으면 비어 있는 것으로 본다."""
    try:
        return AgentKnowledge.model_validate(raw or {})
    except ValidationError:
        return AgentKnowledge()


def sql_key_tier_of(row: SqlSource) -> KeyTier:
    policy: Mapping[str, Any] = row.data_policy or {}
    return "paid" if policy.get("key_tier") == "paid" else "free"


async def sql_tier(
    session: AsyncSession, workspace_id: uuid.UUID, source_ids: Collection[uuid.UUID]
) -> KeyTier:
    """살아 있는 데이터 소스 중 하나라도 유료면 paid. 지운 것은 보지 않는다(3단계 10.6)."""
    if not source_ids:
        return "free"
    rows = await SqlSourceRepository(session, workspace_id).get_many_active(source_ids)
    return "paid" if any(sql_key_tier_of(row) == "paid" for row in rows) else "free"


def agent_sql_sources(raw: Sequence[Any] | None) -> list[uuid.UUID]:
    """agent_version.sql_sources 열. 읽을 수 없는 항목은 건너뛴다."""
    found: list[uuid.UUID] = []
    for item in raw or []:
        try:
            source_id = uuid.UUID(str(item))
        except ValueError:
            continue
        if source_id not in found:
            found.append(source_id)
    return found
