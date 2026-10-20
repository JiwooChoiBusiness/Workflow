"""list_operator 노드(2단계 설계 2.4). filter → sort → slice 순서와 first_record·last_record·item,
파일 배열의 name·size·mime·extension 필터, 객체 key, 검증 규칙을 본다. 배열은 상류 test_flaky 노드가
config.value로 낸다.
"""

from typing import Any

import pytest

from app.workflow.graph import validate

Node = Any


def file_ref(name: str, size: int, mime: str) -> dict[str, Any]:
    return {
        "file_id": f"id-{name}",
        "filename": name,
        "mime": mime,
        "size_bytes": size,
        "storage_key": f"wf-files/w/{name}",
        "source": "wf_file",
    }


FILES = [
    file_ref("보고서.PDF", 3000, "application/pdf"),
    file_ref("사진.png", 900, "image/png"),
    file_ref("메모.txt", 120, "text/plain"),
    file_ref("요약.pdf", 50, "application/pdf"),
]


def list_graph(kit: Any, make_node: Node, values: Any, **config: Any) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("list_1", "test_flaky", {"value": values}),
            make_node("list_operator_1", "list_operator", {"input_var": "list_1.value", **config}),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": name, "var": f"list_operator_1.{name}"}
                        for name in ("result", "count", "first_record", "last_record", "item")
                    ]
                },
            ),
        ],
        ["start>list_1", "list_1>list_operator_1", "list_operator_1>end_1"],
    )


async def test_numbers_are_filtered_sorted_sliced_and_picked(kit: Any, make_node: Node) -> None:
    graph = list_graph(
        kit,
        make_node,
        [5, 1, 9, 3, 7, 2],
        filter={"conditions": [{"op": ">", "value": 2}]},
        sort={"order": "desc"},
        slice={"offset": 1, "limit": 2},
        extract_index=-1,
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {
        "result": [7, 5],
        "count": 2,
        "first_record": 7,
        "last_record": 5,
        "item": 5,
    }


async def test_files_filter_by_extension_and_sort_by_size(kit: Any, make_node: Node) -> None:
    graph = list_graph(
        kit,
        make_node,
        FILES,
        filter={
            "logical": "or",
            "conditions": [
                {"key": "extension", "op": "=", "value": "pdf"},
                {"key": "mime", "op": "starts_with", "value": "text/"},
            ],
        },
        sort={"key": "size", "order": "asc"},
        extract_index=1,
    )

    run = await kit.run(graph)

    outputs = run.outcome.outputs
    assert [item["filename"] for item in outputs["result"]] == [
        "요약.pdf",
        "메모.txt",
        "보고서.PDF",
    ]
    assert outputs["item"]["filename"] == "메모.txt"
    assert outputs["count"] == 3


async def test_file_name_and_size_conditions(kit: Any, make_node: Node) -> None:
    graph = list_graph(
        kit,
        make_node,
        FILES,
        filter={
            "conditions": [
                {"key": "name", "op": "contains", "value": "보고"},
                {"key": "size", "op": "≥", "value": 1000},
            ]
        },
    )

    run = await kit.run(graph)

    assert [item["filename"] for item in run.outcome.outputs["result"]] == ["보고서.PDF"]


async def test_objects_use_dotted_keys_and_missing_values_sort_last(
    kit: Any, make_node: Node
) -> None:
    rows = [
        {"name": "가", "meta": {"rank": 2}},
        {"name": "나"},
        {"name": "다", "meta": {"rank": 1}},
    ]
    graph = list_graph(kit, make_node, rows, sort={"key": "meta.rank", "order": "desc"})

    run = await kit.run(graph)

    assert [row["name"] for row in run.outcome.outputs["result"]] == ["가", "다", "나"]


@pytest.mark.parametrize(
    ("values", "index", "item", "first"),
    [
        (None, 0, None, None),
        ([], 0, None, None),
        (["a", "b"], 5, None, "a"),
        # 배열이 아닌 값은 1원소 배열로 본다(3.2).
        ("하나", 0, "하나", "하나"),
    ],
)
async def test_edges_of_the_input(
    kit: Any, make_node: Node, values: Any, index: int, item: Any, first: Any
) -> None:
    graph = list_graph(kit, make_node, values, extract_index=index)

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["item"] == item
    assert run.outcome.outputs["first_record"] == first


async def test_mixed_types_sort_without_errors(kit: Any, make_node: Node) -> None:
    graph = list_graph(kit, make_node, ["b", 3, {"x": 1}, 1, "a", True], sort={})

    run = await kit.run(graph)

    # 같은 키(1과 true)는 원래 순서를 지킨다.
    assert run.outcome.outputs["result"] == [1, True, 3, "a", "b", {"x": 1}]


def test_validation_rules(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "docs", "type": "file_list"}]}),
            make_node(
                "list_operator_1",
                "list_operator",
                {
                    "input_var": "start.docs",
                    "filter": {
                        "conditions": [
                            {"key": "name", "op": ">", "value": 1},
                            {"key": "type", "op": "=", "value": "image"},
                            {"key": "name", "op": "regex", "value": "(" * 600},
                        ]
                    },
                    "sort": {"key": "owner"},
                },
            ),
            make_node("end_1", "end"),
        ],
        ["start>list_operator_1", "list_operator_1>end_1"],
    )

    found = {(issue.code, issue.field) for issue in validate(graph, "workflow")}

    assert ("invalid_operator", "filter.conditions[0].op") in found
    assert ("invalid_config", "filter.conditions[1].key") in found
    assert ("invalid_config", "filter.conditions[2].value") in found
    assert ("invalid_config", "sort.key") in found


def test_catalog_entry() -> None:
    from app.workflow.nodes import default_registry

    described = {item.type: item for item in default_registry().describe()}
    entry = described["list_operator"]

    assert [(o.name, o.type) for o in entry.outputs] == [
        ("result", "array[any]"),
        ("count", "number"),
        ("first_record", "any"),
        ("last_record", "any"),
        ("item", "any"),
    ]
    assert entry.error_strategies == ["fail"]
    assert set(entry.config_schema["properties"]) == {
        "input_var",
        "filter",
        "sort",
        "slice",
        "extract_index",
    }
