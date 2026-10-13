"""과업 지시 조립(설계 4.10): 템플릿 채우기, 출력 스키마 검사, 첫 메시지, 과업 블록."""

import uuid
from typing import Any

import pytest

from app.agent.prompt_builder import task_block
from app.agent.task_service import (
    TaskSpecError,
    check_output_schema,
    file_reference,
    render_instruction,
    task_message,
)
from app.schemas.task import TaskFile

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
    "additionalProperties": False,
}


def test_render_fills_variables_in_a_sandbox() -> None:
    rendered = render_instruction(
        "{{a}}와 {{b}}를 더한다. 목록: {{ items | join(', ') }}", {"a": 2, "b": 3, "items": ["x"]}
    )

    assert rendered == "2와 3를 더한다. 목록: x"


@pytest.mark.parametrize(
    "template",
    [
        "{{ missing }}를 쓴다",
        "{% for x in %}",
        "{{ ''.__class__.__mro__ }}",
        "   ",
    ],
)
def test_render_rejects_typos_syntax_errors_unsafe_access_and_blank(template: str) -> None:
    with pytest.raises(TaskSpecError) as caught:
        render_instruction(template, {})

    assert caught.value.code == "instruction_invalid"


def test_output_schema_must_be_a_closed_object() -> None:
    check_output_schema(SCHEMA)

    for bad in (
        {**SCHEMA, "type": "array"},
        {key: value for key, value in SCHEMA.items() if key != "additionalProperties"},
        {**SCHEMA, "properties": {"sum": {"type": "integr"}}},
    ):
        with pytest.raises(TaskSpecError) as caught:
            check_output_schema(bad)
        assert caught.value.code == "output_schema_invalid"


def test_file_reference_is_exactly_one_file_id_key() -> None:
    file_id = uuid.uuid4()

    assert file_reference({"file_id": str(file_id)}) == file_id
    assert file_reference({"file_id": str(file_id), "x": 1}) is None
    assert file_reference("text") is None
    with pytest.raises(TaskSpecError):
        file_reference({"file_id": "not-a-uuid"})


def test_task_message_lists_inputs_and_files() -> None:
    files = [
        TaskFile(
            session_file_id=uuid.uuid4(),
            path="task/files/report.csv",
            filename="report.csv",
            mime="text/csv",
            size_bytes=12,
            storage_key="uploads/s/f",
        )
    ]

    text = task_message(
        "보고서를 요약한다.", {"doc": "task/files/report.csv", "n": 3, "long": "가" * 600}, files
    )

    assert text.startswith("보고서를 요약한다.")
    assert "입력 값(전체는 task/inputs.json):" in text
    assert "- n: 3" in text and "- doc: task/files/report.csv" in text
    assert "(600자 중 앞부분)" in text
    assert "- task/files/report.csv (12바이트, text/csv)" in text


def test_task_block_asks_for_submit_result_with_the_full_schema() -> None:
    block = task_block(SCHEMA, submit_tool="submit_result")
    plain = task_block(None, submit_tool="submit_result")

    assert block.startswith("# 과업")
    assert "`submit_result`" in block and "task/inputs.json" in block
    assert '"additionalProperties": false' in block
    assert "submit_result" not in plain and "마지막 답변" in plain
