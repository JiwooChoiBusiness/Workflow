"""API 키 대상(2단계 설계 4.1, 6.3).

키는 에이전트 또는 워크플로우 하나에 묶이고, 워크플로우 키는 워크플로우의 kind로 workflow와
chatflow로 갈린다. 대상별 기본 권한과 허용 권한, 대상이 맞지 않는 호출의 오류를 본다.
"""

import uuid

import pytest

from app.db.models import ApiKey
from app.services.api_key_target import (
    allowed_scopes,
    api_target,
    target_columns,
    wrong_target,
)
from app.services.api_keys import (
    AuthenticatedApiKey,
    KeyTarget,
    default_scopes,
    key_target,
)


def test_default_scopes_follow_the_target() -> None:
    target = uuid.uuid4()

    assert default_scopes(KeyTarget.agent(target)) == ["chat", "tasks"]
    assert default_scopes(KeyTarget.workflow(target)) == ["workflow"]
    assert default_scopes(KeyTarget.workflow(target), chatflow=True) == ["chat"]
    assert allowed_scopes("agent") == {"chat", "tasks"}
    assert allowed_scopes("workflow") == {"workflow"}
    assert allowed_scopes("chatflow") == {"chat"}


def test_rows_and_authenticated_keys_point_at_their_target() -> None:
    agent_id, workflow_id = uuid.uuid4(), uuid.uuid4()
    agent_row = ApiKey(
        id=uuid.uuid4(), agent_id=agent_id, name="k", key_prefix="sk-wf-", key_hash="h"
    )
    flow_row = ApiKey(
        id=uuid.uuid4(), workflow_id=workflow_id, name="k", key_prefix="sk-wf-", key_hash="h2"
    )
    agent_key = AuthenticatedApiKey(
        key_id=agent_row.id, workspace_id=uuid.uuid4(), agent_id=agent_id, scopes=("chat",)
    )
    chat_key = AuthenticatedApiKey(
        key_id=flow_row.id,
        workspace_id=uuid.uuid4(),
        agent_id=None,
        scopes=("chat",),
        workflow_id=workflow_id,
        flow_kind="chatflow",
    )

    assert key_target(agent_row) == KeyTarget.agent(agent_id) == agent_key.target
    assert key_target(flow_row) == KeyTarget.workflow(workflow_id) == chat_key.target
    assert KeyTarget.agent(agent_id) != KeyTarget.workflow(agent_id)
    assert (agent_key.api_target, chat_key.api_target) == ("agent", "chatflow")
    assert api_target(KeyTarget.workflow(workflow_id), "workflow") == "workflow"
    assert target_columns(KeyTarget.workflow(workflow_id)) == {
        "agent_id": None,
        "workflow_id": workflow_id,
    }


def test_a_row_without_a_target_is_rejected() -> None:
    row = ApiKey(id=uuid.uuid4(), name="k", key_prefix="sk-wf-", key_hash="h")

    with pytest.raises(ValueError):
        key_target(row)


def test_wrong_target_names_both_sides() -> None:
    error = wrong_target("agent", "workflow", "chatflow")

    assert (error.status, error.code) == (400, "wrong_target")
    assert error.details == {"key_target": "agent", "expected": ["workflow", "chatflow"]}
