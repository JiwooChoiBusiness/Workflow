"""API 키 대상(2단계 설계 4.1).

열 변경(api_key.workflow_id)은 M17이고 지금은 에이전트 대상뿐이다.
"""

import uuid

import pytest

from app.db.models import ApiKey
from app.services.api_keys import (
    ApiKeyService,
    AuthenticatedApiKey,
    KeyTarget,
    default_scopes,
    key_target,
)
from app.services.errors import ServiceError


def test_default_scopes_follow_the_target() -> None:
    target = uuid.uuid4()

    assert default_scopes(KeyTarget.agent(target)) == ["chat", "tasks"]
    assert default_scopes(KeyTarget.workflow(target)) == ["workflow"]
    assert default_scopes(KeyTarget.workflow(target), chatflow=True) == ["chat"]


def test_existing_rows_and_authenticated_keys_point_at_their_agent() -> None:
    agent_id = uuid.uuid4()
    row = ApiKey(id=uuid.uuid4(), agent_id=agent_id, name="k", key_prefix="sk-wf-", key_hash="h")
    key = AuthenticatedApiKey(
        key_id=row.id, workspace_id=uuid.uuid4(), agent_id=agent_id, scopes=("chat",)
    )

    assert key_target(row) == KeyTarget.agent(agent_id) == key.target
    assert KeyTarget.agent(agent_id) != KeyTarget.workflow(agent_id)


async def test_workflow_targets_are_refused_until_the_column_exists() -> None:
    service = ApiKeyService(db=None)  # pyright: ignore[reportArgumentType]
    target = KeyTarget.workflow(uuid.uuid4())

    with pytest.raises(ServiceError) as caught:
        await service._check_target(None, uuid.uuid4(), target)  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert caught.value.status == 422 and caught.value.code == "target_not_supported"
