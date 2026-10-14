"""code 노드(2단계 설계 2.3, 9.1, M13).

실제 로컬 백엔드(임시 DATA_DIR/wf-workspaces)에서 샌드박스 파이썬을 띄운다. 메모리·프로세스 상한과
하우스키핑은 Windows Job Object가 필요해 tests/windows/test_code_node.py에서 본다.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.workflow.code_runner import CodeError, CodeRun, wrapper_source
from app.workflow.graph import validate
from app.workflow.nodes.code import code_failure
from app.workflow.ports import NodeSettings

Node = Callable[..., dict[str, Any]]


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[LocalWorkspaceBackend]:
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    backend = LocalWorkspaceBackend(settings, workspaces_dir=tmp_path / "data" / "wf-workspaces")
    yield backend
    await backend.aclose()


def code_graph(kit: Any, make_node: Node, config: dict[str, Any], **extra: Any) -> Any:
    outputs = config.get("outputs", [])
    return kit.graph(
        [
            make_node(
                "start",
                "start",
                {"fields": [{"name": "n", "type": "number"}, {"name": "word", "type": "string"}]},
            ),
            make_node("code_1", "code", config, **extra),
            make_node(
                "end_1",
                "end",
                {"outputs": [{"name": o["name"], "var": f"code_1.{o['name']}"} for o in outputs]},
            ),
        ],
        ["start>code_1", "code_1>end_1"],
    )


INPUTS = [{"name": "n", "var": "start.n"}, {"name": "word", "var": "start.word"}]


async def run_code_node(
    kit: Any,
    make_node: Node,
    sandbox: LocalWorkspaceBackend,
    code: str,
    outputs: list[dict[str, str]],
    **config: Any,
) -> Any:
    graph = code_graph(
        kit, make_node, {"code": code, "inputs": INPUTS, "outputs": outputs, **config}
    )
    ports = kit.ports(sandbox=sandbox, node_settings=NodeSettings(code_timeout_sec=7))
    return await kit.run(graph, inputs={"n": 4, "word": "안녕"}, ports=ports)


def record(run: Any) -> Any:
    found = next(
        e.node_run
        for e in run.events
        if e.type == "node.completed" and e.data["node_id"] == "code_1"
    )
    assert found is not None
    return found


async def test_main_gets_inputs_and_its_dict_becomes_outputs(
    kit: Any, make_node: Node, sandbox: LocalWorkspaceBackend
) -> None:
    code = (
        "import json\n"
        "def main(n, word):\n"
        "    print('계산', n)\n"
        "    return {'double': n * 2, 'text': word * 2, 'items': [word], 'extra': 1}\n"
    )
    run = await run_code_node(
        kit,
        make_node,
        sandbox,
        code,
        [
            {"name": "double", "type": "string"},
            {"name": "text", "type": "string"},
            {"name": "items", "type": "array[string]"},
        ],
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"double": "8", "text": "안녕안녕", "items": ["안녕"]}
    done = record(run)
    assert done.inputs == {"n": 4, "word": "안녕"}
    assert done.process["stdout"] == "계산 4\n" and done.process["exit_code"] == 0
    assert done.process["timeout_sec"] == 7
    assert any(w.startswith("undeclared_output: extra") for w in done.warnings)


async def test_exceptions_report_the_users_line_numbers(
    kit: Any, make_node: Node, sandbox: LocalWorkspaceBackend
) -> None:
    code = "def main(n, word):\n    total = n\n    return {'v': total / 0}\n"
    run = await run_code_node(kit, make_node, sandbox, code, [{"name": "v", "type": "number"}])

    assert run.outcome.error is not None
    assert run.outcome.error.code == "code_error"
    assert run.outcome.error.message == "ZeroDivisionError: division by zero"
    trace = record(run).process["traceback"]
    assert 'File "node_code_1.py", line 3, in main' in trace
    assert "return {'v': total / 0}" in trace
    assert "_main" not in trace


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("def main(n, word):\n    return [n]\n", "invalid_output"),
        ("def main(n, word):\n    return {'v': {1, 2}}\n", "invalid_output"),
        ("def main(n, word):\n    return {'other': 1}\n", "invalid_output"),
        ("def main(n, word):\n    return {'v': 'x' * (300 * 1024)}\n", "output_too_large"),
        ("def main(n, word):\n    while True:\n        print('y' * 4096)\n", "stdout_too_large"),
        ("def main(n):\n    return {'v': n}\n", "code_error"),
    ],
)
async def test_bad_returns_and_runaway_output_end_with_their_codes(
    kit: Any, make_node: Node, sandbox: LocalWorkspaceBackend, code: str, expected: str
) -> None:
    run = await run_code_node(kit, make_node, sandbox, code, [{"name": "v", "type": "any"}])

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == expected


async def test_timeout_stops_the_process(
    kit: Any, make_node: Node, sandbox: LocalWorkspaceBackend
) -> None:
    code = "import time\ndef main(n, word):\n    time.sleep(30)\n    return {}\n"
    run = await run_code_node(kit, make_node, sandbox, code, [], timeout_sec=1)

    assert run.outcome.error is not None
    assert (run.outcome.error.code, run.outcome.error.retryable) == ("timeout", True)
    assert record(run).process["timed_out"] is True
    assert record(run).duration_ms is not None and record(run).duration_ms < 15_000


async def test_reruns_do_not_read_an_earlier_result(
    kit: Any, make_node: Node, sandbox: LocalWorkspaceBackend
) -> None:
    good = "def main(n, word):\n    return {'v': 1}\n"
    crash = "import os\ndef main(n, word):\n    os._exit(3)\n"
    run_id = uuid.uuid4()
    ports = kit.ports(sandbox=sandbox)
    first = await kit.run(
        code_graph(kit, make_node, {"code": good, "inputs": INPUTS, "outputs": [{"name": "v"}]}),
        inputs={"n": 1, "word": "a"},
        ports=ports,
        run_id=run_id,
    )
    second = await kit.run(
        code_graph(kit, make_node, {"code": crash, "inputs": INPUTS, "outputs": [{"name": "v"}]}),
        inputs={"n": 1, "word": "a"},
        ports=ports,
        run_id=run_id,
    )

    assert first.outcome.status == "succeeded"
    assert second.outcome.error is not None
    assert second.outcome.error.code == "code_crashed"
    assert "종료 코드 3" in second.outcome.error.message


def test_failure_mapping_without_a_result() -> None:
    assert code_failure(CodeRun(exit_code=0, result={}), 30) is None
    killed = CodeRun(killed=True)
    assert code_failure(killed, 30) == ("code_killed", "코드 프로세스가 중지됐다", True)
    oom = code_failure(CodeRun(exit_code=1, peak_memory_bytes=255 * 2**20), 30)
    assert oom is not None and oom[0] == "memory_limit"
    syntax = CodeRun(error=CodeError(kind="syntax", message="1행: invalid syntax"))
    assert code_failure(syntax, 30) == ("invalid_code", "1행: invalid syntax", False)


def test_wrapper_keeps_code_and_inputs_as_literals() -> None:
    source = wrapper_source(
        node_id="code_1",
        exec_id="wf-code_1-1",
        code="def main(x):\n    return {'x': '{{ not a template }}'}\n",
        inputs={"x": "}{"},
    )

    compile(source, "wrapper.py", "exec")
    assert "'node_code_1.py'" in source and "wf-code_1-1.output.json" in source


def test_validation_rules(kit: Any, make_node: Node) -> None:
    def issues(config: dict[str, Any]) -> list[tuple[str, str | None]]:
        graph = code_graph(kit, make_node, {"inputs": INPUTS, **config})
        return [(i.code, i.field) for i in validate(graph, "workflow") if i.node_id == "code_1"]

    assert issues({"code": "def main(n, word):\n    return {}\n"}) == []
    assert issues({"code": "def main(:\n"}) == [("code_syntax", "code")]
    assert issues({"code": "x = 1\n"}) == [("code_main_missing", "code")]
    assert issues({"code": ""}) == [("code_main_missing", "code")]
    duplicated = issues(
        {
            "code": "def main(n, word):\n    return {}\n",
            "outputs": [{"name": "a"}, {"name": "a"}],
        }
    )
    assert duplicated == [("duplicate_name", "outputs[1].name")]
    assert issues({"code": "def main():\n    return {}\n", "timeout_sec": 61})[0] == (
        "invalid_config",
        "timeout_sec",
    )
