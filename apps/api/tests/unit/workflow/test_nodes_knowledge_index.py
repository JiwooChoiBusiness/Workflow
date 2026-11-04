"""knowledge_index 노드(3단계 설계 8.2, M24).

문서 만들기와 상태 읽기는 가짜 KnowledgePort가 한다.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.schemas.knowledge import ProcessRule, RetrievalSettings
from app.schemas.retrieval import MetadataFilter
from app.workflow.graph import validate
from app.workflow.nodes.base import KnowledgeBaseRef
from app.workflow.nodes.knowledge_index import KnowledgeIndexRunner, final_status
from app.workflow.ports import (
    DuplicatePolicy,
    IndexedDocuments,
    KnowledgeHits,
    KnowledgePortError,
)

KB = uuid.UUID("0196f000-0000-7000-8000-0000000000c1")


class FakeIndex:
    """문서를 만들었다고 치고 statuses는 정해 둔 순서로 상태를 바꾼다."""

    def __init__(
        self,
        *,
        statuses: Sequence[str] = ("completed",),
        skipped: bool = False,
        error: KnowledgePortError | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.polls = 0
        self.sequence = list(statuses)
        self.skipped = skipped
        self.error = error
        self.created: list[uuid.UUID] = []

    async def retrieve(
        self,
        kb_ids: Sequence[uuid.UUID],
        query: str,
        settings: RetrievalSettings | None,
        metadata_filter: MetadataFilter | None,
        *,
        tier: str,
    ) -> KnowledgeHits:
        raise NotImplementedError

    def _made(self, count: int) -> IndexedDocuments:
        if self.error is not None:
            raise self.error
        self.created = [uuid.uuid4() for _ in range(count)]
        return IndexedDocuments(
            document_ids=self.created, batch="" if self.skipped else "b1", skipped=self.skipped
        )

    async def index_text(
        self,
        kb_id: uuid.UUID,
        *,
        name: str,
        text: str,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments:
        self.calls.append(
            {
                "source": "text",
                "kb_id": kb_id,
                "name": name,
                "text": text,
                "metadata": dict(metadata),
                "on_duplicate": on_duplicate,
                "process_rule": process_rule,
            }
        )
        return self._made(1)

    async def index_files(
        self,
        kb_id: uuid.UUID,
        files: Sequence[Mapping[str, Any]],
        *,
        name: str | None,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments:
        self.calls.append(
            {
                "source": "files",
                "files": [item["filename"] for item in files],
                "name": name,
                "on_duplicate": on_duplicate,
            }
        )
        return self._made(len(files))

    async def statuses(
        self, kb_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        status = self.sequence[min(self.polls, len(self.sequence) - 1)]
        self.polls += 1
        return {document_id: status for document_id in document_ids}


@pytest.fixture
def fast(kit: Any) -> Any:
    # 5초 대신 짧게 돈다.
    kit.registry.register(KnowledgeIndexRunner(poll_sec=0.01))
    return kit


def file_ref(name: str) -> dict[str, Any]:
    return {
        "file_id": str(uuid.uuid4()),
        "filename": name,
        "mime": "text/plain",
        "size_bytes": 3,
        "storage_key": "wf-files/x",
        "source": "wf_file",
    }


def graph_with(kit: Any, make_node: Any, config: dict[str, Any], fields: list[Any]) -> Any:
    return kit.graph(
        [
            make_node("start", "start", {"fields": fields}),
            make_node("index_1", "knowledge_index", {"kb_id": str(KB), **config}),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "ids", "var": "index_1.document_ids"},
                        {"name": "batch", "var": "index_1.batch"},
                        {"name": "status", "var": "index_1.status"},
                    ]
                },
            ),
        ],
        ["start>index_1", "index_1>end_1"],
    )


async def test_text_becomes_a_document_with_rendered_name_and_metadata(
    fast: Any, make_node: Any
) -> None:
    port = FakeIndex()
    graph = graph_with(
        fast,
        make_node,
        {
            "source": {"text_var": "start.body"},
            "name": "회의록 {{ start.day }}",
            "metadata": [{"name": "team", "value": "{{ start.team }}"}],
            "on_duplicate": "skip",
        },
        [
            {"name": "body", "type": "paragraph"},
            {"name": "day", "type": "string"},
            {"name": "team", "type": "string"},
        ],
    )

    result = await fast.run(
        graph,
        inputs={"body": "결정: 다음 주에 배포한다.", "day": "11-03", "team": " 결제 "},
        ports=fast.ports(knowledge=port),
    )

    assert result.outcome.status == "succeeded", result.outcome.error
    [call] = port.calls
    assert call["name"] == "회의록 11-03" and call["text"] == "결정: 다음 주에 배포한다."
    assert call["metadata"] == {"team": "결제"} and call["on_duplicate"] == "skip"
    assert result.outcome.outputs == {
        "ids": [str(port.created[0])],
        "batch": "b1",
        "status": "queued",
    }


async def test_default_name_is_the_run_time(fast: Any, make_node: Any) -> None:
    port = FakeIndex()
    graph = graph_with(
        fast,
        make_node,
        {"source": {"text_var": "start.body"}},
        [{"name": "body", "type": "string"}],
    )

    result = await fast.run(graph, inputs={"body": "글"}, ports=fast.ports(knowledge=port))

    assert result.outcome.status == "succeeded"
    # sys.now(UTC ISO)이다.
    assert port.calls[0]["name"].endswith("Z") and "T" in port.calls[0]["name"]
    assert port.calls[0]["on_duplicate"] == "replace"


async def test_wait_polls_until_the_documents_finish(fast: Any, make_node: Any) -> None:
    port = FakeIndex(statuses=("waiting", "indexing", "completed"))
    graph = graph_with(
        fast,
        make_node,
        {"source": {"text_var": "start.body"}, "wait": True},
        [{"name": "body", "type": "string"}],
    )

    result = await fast.run(graph, inputs={"body": "글"}, ports=fast.ports(knowledge=port))

    assert result.outcome.status == "succeeded"
    assert result.outcome.outputs["status"] == "completed"
    assert port.polls == 3


async def test_wait_gives_up_at_its_own_limit(fast: Any, make_node: Any) -> None:
    port = FakeIndex(statuses=("indexing",))
    fast.registry.register(KnowledgeIndexRunner(poll_sec=6.0))
    graph = graph_with(
        fast,
        make_node,
        {"source": {"text_var": "start.body"}, "wait": True, "timeout_sec": 10},
        [{"name": "body", "type": "string"}],
    )

    result = await fast.run(graph, inputs={"body": "글"}, ports=fast.ports(knowledge=port))

    assert result.outcome.status == "failed"
    assert result.outcome.error is not None
    assert result.outcome.error.code == "index_wait_timeout"
    assert result.outcome.error.retryable is False


def test_final_status_folds_documents() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    ids = [first, second]

    assert final_status({first: "completed", second: "completed"}, ids) == "completed"
    assert final_status({first: "completed", second: "indexing"}, ids) is None
    assert final_status({first: "paused", second: "completed"}, ids) == "paused"
    assert final_status({first: "paused", second: "error"}, ids) == "error"
    # 지운 문서는 상태가 없고 error로 본다.
    assert final_status({first: "completed"}, ids) == "error"


async def test_skipped_duplicate_returns_the_existing_document(fast: Any, make_node: Any) -> None:
    port = FakeIndex(skipped=True)
    graph = graph_with(
        fast,
        make_node,
        {"source": {"text_var": "start.body"}, "on_duplicate": "skip", "wait": True},
        [{"name": "body", "type": "string"}],
    )

    result = await fast.run(graph, inputs={"body": "글"}, ports=fast.ports(knowledge=port))

    assert result.outcome.outputs["status"] == "skipped"
    assert port.polls == 0
    node = next(data for data in result.of("node.completed") if data["node_id"] == "index_1")
    assert node["warnings"] == ["duplicate_skipped: 같은 이름의 문서가 있어 넣지 않았다"]


async def test_files_keep_their_names_unless_one_file_is_renamed(fast: Any, make_node: Any) -> None:
    port = FakeIndex()
    graph = graph_with(
        fast,
        make_node,
        {"source": {"files_var": "start.docs"}, "name": "보고서"},
        [{"name": "docs", "type": "file_list"}],
    )

    result = await fast.run(
        graph,
        inputs={"docs": [file_ref("a.txt"), file_ref("b.txt")]},
        ports=fast.ports(knowledge=port),
    )

    assert result.outcome.status == "succeeded", result.outcome.error
    assert port.calls[0]["files"] == ["a.txt", "b.txt"] and port.calls[0]["name"] is None
    assert len(result.outcome.outputs["ids"]) == 2
    node = next(data for data in result.of("node.completed") if data["node_id"] == "index_1")
    assert node["warnings"] == ["name_ignored: 파일이 여럿이라 문서 이름은 파일 이름 그대로다"]

    single = await fast.run(
        graph, inputs={"docs": [file_ref("a.txt")]}, ports=fast.ports(knowledge=port)
    )
    assert single.outcome.status == "succeeded"
    assert port.calls[1]["name"] == "보고서"


async def test_empty_text_and_port_errors_fail_the_node(fast: Any, make_node: Any) -> None:
    graph = graph_with(
        fast,
        make_node,
        {"source": {"text_var": "start.body"}},
        [{"name": "body", "type": "string"}],
    )

    empty = await fast.run(graph, inputs={"body": "  "}, ports=fast.ports(knowledge=FakeIndex()))
    assert empty.outcome.error is not None and empty.outcome.error.code == "source_empty"

    error = KnowledgePortError("knowledge_base_not_found", "지식 베이스를 찾을 수 없다")
    failed = await fast.run(
        graph, inputs={"body": "글"}, ports=fast.ports(knowledge=FakeIndex(error=error))
    )
    assert failed.outcome.error is not None
    assert failed.outcome.error.code == "knowledge_base_not_found"


def test_validation_rules(kit: Any, make_node: Any) -> None:
    graph = kit.graph(
        [
            make_node(
                "start",
                "start",
                {"fields": [{"name": "n", "type": "number"}, {"name": "f", "type": "file"}]},
            ),
            make_node("index_1", "knowledge_index", {}),
            make_node(
                "index_2",
                "knowledge_index",
                {
                    "kb_id": str(KB),
                    "source": {"text_var": "start.n", "files_var": "start.n"},
                },
            ),
            make_node(
                "index_3",
                "knowledge_index",
                {
                    "kb_id": str(KB),
                    "source": {"text_var": "start.f"},
                    "metadata": [{"name": "team", "value": "a"}, {"name": "team", "value": "b"}],
                },
            ),
            make_node("end_1", "end", {"outputs": []}),
        ],
        ["start>index_1", "index_1>index_2", "index_2>index_3", "index_3>end_1"],
    )
    paid = KnowledgeBaseRef(id=KB, name="급여", paid=True, fields={"year": "number"})

    found = {
        (issue.node_id, issue.level, issue.code, issue.field)
        for issue in validate(graph, "workflow", knowledge_bases={KB: paid})
    }

    assert ("index_1", "error", "kb_required", "kb_id") in found
    assert ("index_1", "error", "source_required", "source") in found
    assert ("index_2", "error", "invalid_config", "source") in found
    assert ("index_3", "error", "duplicate_name", "metadata[1].name") in found
    assert ("index_3", "error", "metadata_field_not_found", "metadata[0].name") in found
    assert ("index_3", "warning", "paid_key_required", "kb_id") in found
    # 글 변수가 파일이면 타입이 맞지 않는다는 경고가 붙는다.
    assert ("index_3", "warning", "type_mismatch", "source.text_var") in found
    missing = validate(graph, "workflow", knowledge_bases={})
    assert ("index_2", "knowledge_base_not_found") in {
        (issue.node_id, issue.code) for issue in missing
    }
