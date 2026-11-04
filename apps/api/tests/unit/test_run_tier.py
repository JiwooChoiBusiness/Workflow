"""런의 키 등급 규칙(3단계 설계 14.4, M24).

KB를 읽어 등급을 정하는 흐름(런 만들기, 자식 상속)은 tests/integration/test_tier_inheritance.py가
본다.
"""

import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.schemas.common import RunLimits
from app.services.errors import ServiceError
from app.services.run_tier import (
    agent_knowledge,
    higher,
    paid_key_configured,
    require_paid_key,
    run_limits_of,
    tier_of,
    with_tier,
)
from app.services.workflow_knowledge import graph_tier
from app.workflow.nodes.base import KnowledgeBaseRef


def settings(tmp_path: Path, paid: str | None) -> Settings:
    values: dict[str, object] = {"data_dir": tmp_path, "gemini_api_keys": ["AIza" + "1" * 35]}
    if paid is not None:
        values["gemini_paid_api_key"] = SecretStr(paid)
    return Settings.model_validate(values)


def test_higher_tier_wins() -> None:
    assert higher("free", "free") == "free"
    assert higher("free", "paid") == "paid"
    assert higher("paid", "free") == "paid"


def test_tier_lives_next_to_run_limits() -> None:
    limits = with_tier(RunLimits(max_steps=7).model_dump(mode="json"), "paid")

    assert tier_of(limits) == "paid"
    # RunLimits는 extra=forbid라 tier를 떼고 읽는다.
    assert run_limits_of(limits).max_steps == 7
    assert tier_of({}) == "free" and tier_of(None) == "free"
    assert run_limits_of(None) == RunLimits()


def test_paid_key_must_exist_for_paid_runs(tmp_path: Path) -> None:
    without = settings(tmp_path, None)
    blank = settings(tmp_path, "   ")
    with_key = settings(tmp_path, "paid-" + "7" * 30)

    assert not paid_key_configured(without) and not paid_key_configured(blank)
    assert paid_key_configured(with_key)
    require_paid_key(without, "free")
    require_paid_key(with_key, "paid")
    with pytest.raises(ServiceError) as caught:
        require_paid_key(blank, "paid")
    assert caught.value.status == 422 and caught.value.code == "paid_key_missing"


def test_graph_tier_is_paid_when_any_knowledge_base_is() -> None:
    free = KnowledgeBaseRef(id=uuid.uuid4(), name="공개")
    paid = KnowledgeBaseRef(id=uuid.uuid4(), name="급여", paid=True)

    assert graph_tier({}) == "free"
    assert graph_tier({free.id: free}) == "free"
    assert graph_tier({free.id: free, paid.id: paid}) == "paid"


def test_broken_agent_knowledge_reads_as_empty() -> None:
    kb_id = uuid.uuid4()

    assert agent_knowledge({"kb_ids": [str(kb_id)]}).kb_ids == [kb_id]
    assert agent_knowledge(None).kb_ids == []
    assert agent_knowledge({"kb_ids": "x"}).kb_ids == []
