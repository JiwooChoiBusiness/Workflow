"""워크플로우·초안·발행 서비스(services/workflows.py, 2단계 설계 3.1, 4.1, 4.3, 9.4)."""

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import WfVersion
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.schemas.agent_version import EnvVarIn
from app.schemas.workflow import (
    ConversationVar,
    Graph,
    WorkflowCreate,
    WorkflowDraftCheck,
    WorkflowDraftUpdate,
    WorkflowPatch,
)
from app.security.crypto import cipher_from_settings
from app.services.errors import ServiceError
from app.services.workflows import WorkflowService, decrypt_env, graph_hash, stored_env

KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "wf.db")
    await upgrade_head(database)
    yield database
    await database.close()


@pytest.fixture
async def workspace_id(db: Database) -> uuid.UUID:
    async with db.write() as session:
        workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
        workspace.id = new_id()
        return workspace.id


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {"data_dir": tmp_path / "data", "encryption_key": SecretStr(KEY)}
    )


@pytest.fixture
def service(db: Database, settings: Settings) -> WorkflowService:
    return WorkflowService(db, settings)


def node(node_id: str, node_type: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "title": node_id, "config": config or {}}


def linear(*extra_refs: str) -> Graph:
    outputs = [{"name": f"o{i}", "var": ref} for i, ref in enumerate(extra_refs)]
    return Graph.model_validate(
        {
            "nodes": [node("start", "start"), node("end_1", "end", {"outputs": outputs})],
            "edges": [{"id": "e1", "source": "start", "target": "end_1"}],
        }
    )


async def update(
    service: WorkflowService,
    workspace_id: uuid.UUID,
    workflow_id: uuid.UUID,
    graph: Graph,
    **fields: Any,
) -> Any:
    draft = await service.get_draft(workspace_id, workflow_id)
    body = WorkflowDraftUpdate.model_validate(
        {
            "graph": graph.model_dump(mode="json"),
            "env_vars": [],
            "conversation_vars": [],
            "settings": draft.settings.model_dump(mode="json"),
            "updated_at": draft.updated_at,
            **fields,
        }
    )
    return await service.save_draft(workspace_id, workflow_id, body)


async def test_create_uses_template_graphs_and_free_slugs(
    service: WorkflowService, workspace_id: uuid.UUID
) -> None:
    first = await service.create_workflow(workspace_id, WorkflowCreate(name="Order Flow"))
    second = await service.create_workflow(workspace_id, WorkflowCreate(name="Order Flow"))
    chat = await service.create_workflow(
        workspace_id, WorkflowCreate(name="상담", kind="chatflow", template="chat_basic")
    )

    assert (first.slug, second.slug, chat.slug) == ("order-flow", "order-flow-2", "workflow")
    workflow_draft = await service.get_draft(workspace_id, first.id)
    chat_draft = await service.get_draft(workspace_id, chat.id)
    assert [n.type for n in workflow_draft.graph.nodes] == ["start", "end"]
    assert [n.type for n in chat_draft.graph.nodes] == ["start", "llm", "answer"]
    # 저장한 적 없는 템플릿 초안도 캔버스가 쓰는 미리 흘리기 표시를 가진다.
    assert chat_draft.graph.nodes[2].ui == {"prestream_sources": ["llm_1"]}
    assert workflow_draft.issues == [] and chat_draft.issues == []
    assert workflow_draft.graph_hash == graph_hash(workflow_draft.graph)
    with pytest.raises(ServiceError) as wrong:
        await service.create_workflow(workspace_id, WorkflowCreate(name="x", template="chat_basic"))
    assert wrong.value.code == "template_kind_mismatch"
    with pytest.raises(ServiceError) as taken:
        await service.create_workflow(workspace_id, WorkflowCreate(name="y", slug="order-flow"))
    assert taken.value.code == "slug_taken"


async def test_draft_saves_with_issues_and_uses_an_optimistic_lock(
    service: WorkflowService, workspace_id: uuid.UUID
) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    before = await service.get_draft(workspace_id, workflow.id)

    saved = await update(service, workspace_id, workflow.id, linear("nowhere.value"))

    assert saved.updated_at > before.updated_at
    assert [issue.code for issue in saved.issues if issue.level == "error"]
    stale = WorkflowDraftUpdate(graph=linear(), updated_at=before.updated_at)
    with pytest.raises(ServiceError) as conflict:
        await service.save_draft(workspace_id, workflow.id, stale)
    assert conflict.value.status == 409 and conflict.value.code == "draft_conflict"


async def test_env_vars_keep_secrets_encrypted_and_hidden(
    service: WorkflowService, db: Database, settings: Settings, workspace_id: uuid.UUID
) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    graph = linear("env.API_BASE", "env.TOKEN")

    saved = await update(
        service,
        workspace_id,
        workflow.id,
        graph,
        env_vars=[
            {"name": "API_BASE", "value": "https://example.com"},
            {"name": "TOKEN", "value": "secret-token-123", "secret": True},
        ],
    )

    assert saved.issues == []
    by_name = {item.name: item for item in saved.env_vars}
    assert by_name["API_BASE"].value == "https://example.com"
    assert by_name["TOKEN"].value is None and by_name["TOKEN"].has_value is True
    async with db.read() as session:
        version = await session.get(WfVersion, saved.id)
        assert version is not None
        stored = stored_env(version.env_vars)
    assert "secret-token-123" not in str(version.env_vars)
    env, secrets = decrypt_env(stored, lambda: cipher_from_settings(settings))
    assert env == {"API_BASE": "https://example.com", "TOKEN": "secret-token-123"}
    assert secrets == ["TOKEN"]

    # 값 없이 보내면 저장된 값을 그대로 둔다. 일반 값을 비밀로 바꾸면 그 값을 암호화한다.
    kept = await update(
        service,
        workspace_id,
        workflow.id,
        graph,
        env_vars=[{"name": "API_BASE", "secret": True}, {"name": "TOKEN", "secret": True}],
    )
    assert all(item.secret and item.has_value for item in kept.env_vars)
    with pytest.raises(ServiceError) as reveal:
        await update(
            service,
            workspace_id,
            workflow.id,
            graph,
            env_vars=[{"name": "TOKEN", "secret": False}],
        )
    assert reveal.value.code == "validation_error"
    with pytest.raises(ServiceError) as missing:
        await update(
            service, workspace_id, workflow.id, graph, env_vars=[{"name": "NEW", "secret": False}]
        )
    assert missing.value.details[0]["type"] == "env_value_required"


async def test_secret_env_needs_an_encryption_key(
    db: Database, tmp_path: Path, workspace_id: uuid.UUID
) -> None:
    service = WorkflowService(db, Settings.model_validate({"data_dir": tmp_path / "d"}))
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))

    with pytest.raises(ServiceError) as missing:
        await update(
            service,
            workspace_id,
            workflow.id,
            linear(),
            env_vars=[{"name": "TOKEN", "value": "secret-token-123", "secret": True}],
        )

    assert missing.value.status == 503 and missing.value.code == "encryption_key_missing"


async def test_conversation_vars_belong_to_chatflows(
    service: WorkflowService, workspace_id: uuid.UUID
) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    with pytest.raises(ServiceError) as refused:
        await update(
            service,
            workspace_id,
            workflow.id,
            linear(),
            conversation_vars=[{"name": "topic", "type": "string"}],
        )
    assert refused.value.details[0]["type"] == "conversation_vars_not_allowed"

    huge = Graph.model_validate(
        {
            "nodes": [
                node("start", "start"),
                node("template_1", "template", {"template": "가" * 1_100_000}),
            ]
        }
    )
    with pytest.raises(ServiceError) as too_large:
        await update(service, workspace_id, workflow.id, huge)
    assert too_large.value.code == "graph_too_large"


async def test_validate_checks_unsaved_graphs_with_their_env_names(
    service: WorkflowService, workspace_id: uuid.UUID
) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    graph = linear("env.API_BASE")

    unknown = await service.validate_draft(
        workspace_id, workflow.id, WorkflowDraftCheck(graph=graph)
    )
    declared = await service.validate_draft(
        workspace_id,
        workflow.id,
        WorkflowDraftCheck(graph=graph, env_vars=[EnvVarIn(name="API_BASE", value="x")]),
    )
    stored = await service.validate_draft(workspace_id, workflow.id, None)

    assert unknown.ok is False and declared.ok is True and stored.ok is True
    chat = await service.create_workflow(workspace_id, WorkflowCreate(name="chat", kind="chatflow"))
    chat_graph = Graph.model_validate(
        {
            "nodes": [
                node("start", "start"),
                node("answer_1", "answer", {"text": "{{ conv.topic }}"}),
            ],
            "edges": [{"id": "e1", "source": "start", "target": "answer_1"}],
        }
    )
    with_var = await service.validate_draft(
        workspace_id,
        chat.id,
        WorkflowDraftCheck(
            graph=chat_graph, conversation_vars=[ConversationVar(name="topic", type="string")]
        ),
    )
    assert with_var.ok is True


async def test_publish_copies_the_draft_and_restore_switches_versions(
    service: WorkflowService, db: Database, workspace_id: uuid.UUID
) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    await update(service, workspace_id, workflow.id, linear("nowhere.value"))
    with pytest.raises(ServiceError) as invalid:
        await service.publish(workspace_id, workflow.id)
    assert invalid.value.code == "draft_invalid" and invalid.value.details

    await update(service, workspace_id, workflow.id, linear())
    first = await service.publish(workspace_id, workflow.id, "첫 발행")
    second = await service.publish(workspace_id, workflow.id)

    assert first.version.version_number == 1 and first.version.status == "published"
    assert second.workflow.published_version_number == 2
    versions = await service.list_versions(workspace_id, workflow.id)
    assert [(item.version_number, item.is_current) for item in versions.items] == [
        (2, True),
        (1, False),
    ]
    restored = await service.restore_version(workspace_id, workflow.id, first.version.id)
    assert restored.published_version_id == first.version.id
    with pytest.raises(ServiceError):
        await service.restore_version(
            workspace_id, workflow.id, workflow.draft_version_id or new_id()
        )
    # 발행 버전은 초안을 고쳐도 그대로다.
    await update(service, workspace_id, workflow.id, linear("start.nothing"))
    detail = await service.get_version(workspace_id, workflow.id, first.version.id)
    assert detail.graph == linear() and detail.change_note == "첫 발행"


async def test_patch_and_delete(service: WorkflowService, workspace_id: uuid.UUID) -> None:
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))

    patched = await service.update_workflow(
        workspace_id, workflow.id, WorkflowPatch(name="새 이름", icon=None, webapp_enabled=True)
    )
    await service.delete_workflow(workspace_id, workflow.id)

    assert patched.name == "새 이름" and patched.webapp_enabled and patched.icon is None
    assert (await service.list_workflows(workspace_id)).items == []
    with pytest.raises(ServiceError) as gone:
        await service.get_workflow(workspace_id, workflow.id)
    assert gone.value.status == 404
    # 지운 워크플로우의 slug는 다시 쓸 수 있다.
    again = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    assert again.slug == workflow.slug


def test_node_types_include_every_registered_runner(service: WorkflowService) -> None:
    types = {item.type for item in service.node_types().items}

    assert {"start", "end", "answer", "template", "if_else", "llm", "agent"} <= types


async def test_lock_values_change_even_within_the_clock_resolution(
    db: Database, settings: Settings, workspace_id: uuid.UUID
) -> None:
    from app.db.types import utcnow

    frozen = utcnow()
    service = WorkflowService(db, settings, clock=lambda: frozen)
    workflow = await service.create_workflow(workspace_id, WorkflowCreate(name="flow"))
    first = await update(service, workspace_id, workflow.id, linear())
    second = await update(service, workspace_id, workflow.id, linear())

    assert second.updated_at - first.updated_at >= timedelta(microseconds=1)
