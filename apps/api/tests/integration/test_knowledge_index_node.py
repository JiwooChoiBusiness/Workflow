"""knowledge_index 노드의 파일 소스와 실패 처리(3단계 설계 8.2, M24 검토).

파일 변수로 문서를 만들면 data_source_type이 workflow다. 문서를 만든 뒤의 실패(메타데이터, 이름
바꾸기, 옛 문서 지우기)는 다시 돌리면 문서가 또 생기므로 재시도하지 않고 만든 문서 id를 남긴다.
기다리는 중에 런을 멈추면 이미 만든 문서는 지식 레인이 끝까지 색인한다.

포트 단위 시험은 앱의 서비스와 같은 WorkflowKnowledgePort를 직접 만든다.
"""

import asyncio
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import WfFile, Workspace
from app.db.models.enums import WfFileKind
from app.db.repositories.workspaces import WorkspaceRepository
from app.services.errors import ServiceError, not_found
from app.services.knowledge_documents import KnowledgeDocumentService
from app.services.workflow_knowledge import WorkflowKnowledgePort
from app.workflow.nodes import default_registry
from app.workflow.ports import KnowledgePortError

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings.model_copy(update={"llm_fake": True, "kb_embed_rpm": 6000})


def node(node_id: str, node_type: str, config: dict[str, Any], x: float = 0) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def chain(*ids: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{index}", "source": source, "target": target}
        for index, (source, target) in enumerate(zip(ids, ids[1:], strict=False), start=1)
    ]


async def make_kb(client: AsyncClient, name: str) -> dict[str, Any]:
    response = await client.post(
        "/api/console/knowledge", json={"name": name, "data_policy": {"acknowledged": True}}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def save_flow(client: AsyncClient, graph: dict[str, Any]) -> str:
    created = await client.post(
        "/api/console/workflows", json={"name": "자료 넣기", "kind": "workflow"}
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await client.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert [issue for issue in saved.json()["issues"] if issue["level"] == "error"] == []
    return workflow_id


async def documents_of(client: AsyncClient, kb_id: str) -> list[dict[str, Any]]:
    return (await client.get(f"/api/console/knowledge/{kb_id}/documents")).json()["items"]


async def port_of(app: FastAPI) -> WorkflowKnowledgePort:
    async with app.state.db.read() as session:
        workspace = await WorkspaceRepository(session).get_default()
    assert workspace is not None
    return WorkflowKnowledgePort(
        db=app.state.db,
        settings=app.state.settings,
        workspace_id=workspace.id,
        runtime=app.state.knowledge,
        llm=app.state.llm,
        storage=app.state.storage,
        lane=app.state.knowledge_lane,
    )


async def index_text(port: WorkflowKnowledgePort, kb: dict[str, Any], **changes: Any) -> Any:
    values: dict[str, Any] = {
        "name": "회의",
        "text": "결정: 배포는 화요일이다.",
        "process_rule": None,
        "metadata": {},
        "on_duplicate": "replace",
    }
    values.update(changes)
    return await port.index_text(uuid.UUID(kb["id"]), **values)


async def test_a_file_variable_becomes_a_workflow_document(logged_in: AsyncClient) -> None:
    kb = await make_kb(logged_in, "회의록")
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "doc", "type": "file"}]}),
            node(
                "index_1",
                "knowledge_index",
                {
                    "kb_id": kb["id"],
                    "source": {"files_var": "start.doc"},
                    "name": "11월 회의",
                    "wait": True,
                    "timeout_sec": 60,
                },
                200,
            ),
            node("end_1", "end", {"outputs": [{"name": "ids", "var": "index_1.document_ids"}]}),
        ],
        "edges": chain("start", "index_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph)
    upload = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/files",
        files={"file": ("회의.txt", "결정: 배포는 화요일이다.".encode(), "text/plain")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text

    response = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"doc": upload.json()["id"]}, "response_mode": "blocking"},
    )

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "succeeded", run["error"]
    [document] = await documents_of(logged_in, kb["id"])
    assert document["id"] == run["outputs"]["ids"][0]
    # 파일 하나면 노드의 이름으로 바꾸고, 어디서 왔는지는 workflow다.
    assert document["name"] == "11월 회의"
    assert document["data_source_type"] == "workflow"
    assert document["indexing_status"] == "completed"


async def test_a_file_of_another_workspace_is_not_found(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await make_kb(logged_in, "회의록")
    async with app.state.db.write() as session:
        other = Workspace(name="남의 것", slug="other-space", settings={})
        session.add(other)
        await session.flush()
        stranger = WfFile(
            workspace_id=other.id,
            kind=WfFileKind.UPLOAD,
            filename="남.txt",
            mime="text/plain",
            size_bytes=3,
            sha256="0" * 64,
            storage_key="wf-files/other/남.txt",
        )
        session.add(stranger)
        await session.flush()
        ref = {
            "file_id": str(stranger.id),
            "filename": "남.txt",
            "mime": "text/plain",
            "size_bytes": 3,
            "storage_key": stranger.storage_key,
            "source": "wf_file",
        }
    port = await port_of(app)

    with pytest.raises(KnowledgePortError) as caught:
        await port.index_files(
            uuid.UUID(kb["id"]),
            [ref],
            name=None,
            process_rule=None,
            metadata={},
            on_duplicate="replace",
        )

    assert caught.value.code == "file_not_found"
    assert await documents_of(logged_in, kb["id"]) == []


async def test_failures_after_creating_are_not_retried_and_name_the_documents(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = await make_kb(logged_in, "회의록")
    port = await port_of(app)
    old = await index_text(port, kb, on_duplicate="add")

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise ServiceError(500, "storage_failed", "원본을 지우지 못했다")

    monkeypatch.setattr(KnowledgeDocumentService, "delete_document", broken)
    with pytest.raises(KnowledgePortError) as caught:
        await index_text(port, kb)

    error = caught.value
    assert error.code == "storage_failed" and not error.retryable
    names = {item["id"]: item["name"] for item in await documents_of(logged_in, kb["id"])}
    [created] = [document_id for document_id in names if document_id != str(old.document_ids[0])]
    assert created in error.message
    # 새 문서를 먼저 넣었으므로 옛 문서도 그대로 있다.
    assert str(old.document_ids[0]) in names


async def test_an_old_document_that_is_already_gone_is_skipped(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = await make_kb(logged_in, "회의록")
    port = await port_of(app)
    await index_text(port, kb, on_duplicate="add")

    async def gone(*args: Any, **kwargs: Any) -> None:
        raise not_found("문서")

    monkeypatch.setattr(KnowledgeDocumentService, "delete_document", gone)

    replaced = await index_text(port, kb)

    assert len(replaced.document_ids) == 1 and not replaced.skipped


async def test_metadata_values_are_checked_before_creating(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await make_kb(logged_in, "회의록")
    field = await logged_in.post(
        f"/api/console/knowledge/{kb['id']}/metadata", json={"name": "year", "type": "number"}
    )
    assert field.status_code == 201, field.text
    port = await port_of(app)

    with pytest.raises(KnowledgePortError) as caught:
        await index_text(port, kb, metadata={"year": "올해"})

    assert caught.value.code == "metadata_value_invalid"
    assert await documents_of(logged_in, kb["id"]) == []


async def test_stopping_a_waiting_run_leaves_the_document_to_finish(
    logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 색인이 끝나도 노드가 다음 확인까지 오래 자게 해 기다리는 중에 멈춘다.
    monkeypatch.setattr(default_registry().get("knowledge_index"), "poll_sec", 30.0)
    kb = await make_kb(logged_in, "회의록")
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "body", "type": "paragraph"}]}),
            node(
                "index_1",
                "knowledge_index",
                {"kb_id": kb["id"], "source": {"text_var": "start.body"}, "wait": True},
                200,
            ),
            node("end_1", "end", {"outputs": [{"name": "status", "var": "index_1.status"}]}),
        ],
        "edges": chain("start", "index_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph)
    started = asyncio.create_task(
        logged_in.post(
            f"/api/console/workflows/{workflow_id}/draft/runs",
            json={"inputs": {"body": "결정: 배포는 화요일이다."}, "response_mode": "blocking"},
        )
    )
    deadline = time.monotonic() + 10
    while not await documents_of(logged_in, kb["id"]):
        assert time.monotonic() < deadline, "문서가 생기지 않았다"
        await asyncio.sleep(0.05)
    [listed] = (await logged_in.get(f"/api/console/workflows/{workflow_id}/runs")).json()["items"]

    stopped = await logged_in.post(f"/api/console/wf-runs/{listed['id']}/stop", headers=FETCH)

    assert stopped.status_code == 200, stopped.text
    finished = (await started).json()
    assert finished["status"] == "cancelled"
    # 멈춘 런이 만든 문서는 지우지 않는다. 지식 레인이 끝까지 색인한다.
    while True:
        [document] = await documents_of(logged_in, kb["id"])
        if document["indexing_status"] == "completed":
            break
        assert time.monotonic() < deadline, document
        await asyncio.sleep(0.05)
