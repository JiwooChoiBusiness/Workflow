"""자식 결과 요약·자르기·tool_result 문구(오케스트레이션 3.4, 3.9)."""

import json
import uuid
from typing import Any

from app.agent.child_runs import (
    PAYLOAD_MAX_BYTES,
    RESULT_PREVIEW_BYTES,
    ChildFile,
    ChildResultSummary,
    can_call_children,
    child_correlation,
    child_files_dir,
    fit_payload,
    format_child_result,
    summarize_output,
    task_preview,
)
from app.agent.loop import child_tool_result
from app.schemas.events import ToolCallCompletedData

RUN_ID = uuid.UUID("01929999-0000-7000-8000-000000000001")


def summary(**fields: Any) -> ChildResultSummary:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "agent_slug": "researcher",
        "agent_name": "조사 담당",
        "status": "succeeded",
        "text": "근거 A, B",
        "steps": 7,
    }
    values.update(fields)
    return ChildResultSummary.model_validate(values)


def summarize(**fields: Any) -> ChildResultSummary:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "session_id": uuid.uuid4(),
        "agent_slug": "researcher",
        "agent_name": "조사 담당",
        "result_format": "text",
        "status": "succeeded",
        "output": {"status": "succeeded", "result": None, "text": "끝", "files": [], "usage": {}},
        "error": None,
        "steps": 3,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "duration_ms": 1200,
    }
    values.update(fields)
    return summarize_output(**values)


def test_text_result_headline_files_and_body() -> None:
    item = summary(
        files=[
            ChildFile(
                child_path="outputs/sources.md",
                parent_path=f"agents/researcher/{RUN_ID}/outputs/sources.md",
                size=14201,
            ),
            ChildFile(child_path="outputs/broken.md", size=10, copy_failed=True),
            ChildFile(child_path="big.bin", size=99, downloadable=False),
        ]
    )

    content, is_error = format_child_result(item)

    lines = content.splitlines()
    assert (
        lines[0]
        == f"[하위 에이전트 researcher(조사 담당) 완료: succeeded, 스텝 7, run_id {RUN_ID}]"
    )
    assert lines[1] == "결과 파일(부모 작업공간 상대 경로):"
    assert lines[2] == f"- agents/researcher/{RUN_ID}/outputs/sources.md (14,201바이트)"
    assert lines[3] == "- outputs/broken.md (10바이트, 복사 실패)"
    assert lines[4] == "- big.bin (99바이트, 보존하지 않음)"
    assert lines[5] == "---"
    assert lines[6] == "근거 A, B"
    assert is_error is False


def test_failure_cancel_and_step_limit_headlines() -> None:
    failed, failed_error = format_child_result(
        summary(
            status="failed",
            error={"code": "timeout", "message": "런 실행 시간 한도(1800초)를 넘었다"},
        )
    )
    cancelled, cancelled_error = format_child_result(
        summary(status="cancelled", error={"code": "input_timeout", "message": "24시간"})
    )
    limited, limited_error = format_child_result(summary(limit="steps"))

    assert failed.startswith(
        "[하위 에이전트 researcher(조사 담당) 실패: timeout — 런 실행 시간 한도(1800초)를 넘었다"
    )
    assert failed_error is True
    assert cancelled.startswith("[하위 에이전트 researcher(조사 담당) 취소: input_timeout — 24시간")
    assert cancelled_error is True
    assert "스텝 한도에 닿아 멈췄다" in limited.splitlines()[0]
    assert limited_error is False


def test_json_result_body_and_large_json_preview() -> None:
    small, _ = format_child_result(summary(result_format="json", result={"title": "보고서"}))
    assert small.split("---\n", 1)[1] == json.dumps(
        {"title": "보고서"}, ensure_ascii=False, indent=2
    )

    big = {"items": ["가" * 100] * 400}
    item = summarize(
        result_format="json",
        output={"status": "succeeded", "result": big, "text": "", "files": [], "usage": {}},
    )
    assert item.result is None and item.result_too_large
    assert item.result_preview is not None
    assert len(item.result_preview.encode("utf-8")) <= RESULT_PREVIEW_BYTES
    copied = item.model_copy(update={"result_path": f"agents/researcher/{RUN_ID}/result.json"})
    body = format_child_result(copied)[0].split("---\n", 1)[1]
    assert body.startswith("(JSON 결과가 32KB를 넘어 전문은 agents/researcher/")


def test_summarize_output_maps_the_task_output() -> None:
    file_id = uuid.uuid4()
    item = summarize(
        status="failed",
        error={"code": "output_missing", "message": "제출하지 않았다"},
        output={
            "status": "failed",
            "result": None,
            "text": "x" * 40_000,
            "files": [
                {"id": str(file_id), "path": "outputs/a.md", "size_bytes": 5, "downloadable": True}
            ],
            "usage": {},
            "limit": "steps",
        },
    )

    assert item.status == "failed" and item.error is not None
    assert item.error.code == "output_missing"
    assert item.limit == "steps"
    assert "[..." in item.text and len(item.text.encode("utf-8")) < 33 * 1024
    assert item.files == [
        ChildFile(session_file_id=file_id, child_path="outputs/a.md", size=5, downloadable=True)
    ]
    assert item.usage.input_tokens == 10 and item.duration_ms == 1200
    # 실패인데 오류 정보가 없으면 failed 코드를 채운다.
    assert summarize(status="failed").error is not None


def test_payload_is_kept_under_64kb() -> None:
    files = [
        ChildFile(child_path=f"outputs/{'긴이름' * 20}{index}.md", size=1) for index in range(200)
    ]
    item = fit_payload(summary(text="가" * 10_000, files=files))

    assert len(item.model_dump_json().encode("utf-8")) <= PAYLOAD_MAX_BYTES
    assert item.files_omitted > 0
    assert item.files_omitted + len(item.files) == 200
    assert "앞부분만" in item.text
    assert "그 밖" in format_child_result(item)[0]


def test_tool_result_fills_the_required_completed_fields() -> None:
    result = child_tool_result(
        summary(status="failed", error={"code": "timeout", "message": "t"}, duration_ms=4321)
    )

    assert result.status == "error" and result.is_error and result.timed_out
    assert result.duration_ms == 4321
    assert result.child_run_id == str(RUN_ID)
    assert result.output_excerpt == result.content
    assert result.output_bytes == len(result.content.encode("utf-8"))
    ToolCallCompletedData.model_validate(
        {
            "tool_call_id": "c",
            "status": result.status,
            "duration_ms": result.duration_ms,
            "output_excerpt": result.output_excerpt,
            "output_bytes": result.output_bytes,
            "timed_out": result.timed_out,
            "child_run_id": result.child_run_id,
        }
    )
    assert child_tool_result(summary(status="cancelled")).status == "cancelled"
    assert child_tool_result(summary()).status == "ok"
    missing = child_tool_result(None)
    assert missing.is_error and missing.child_run_id is None


def test_depth_and_small_helpers() -> None:
    assert can_call_children(0, 2, 20)
    assert can_call_children(1, 2, 1)
    assert not can_call_children(2, 2, 20)
    assert not can_call_children(0, 2, 0)
    assert child_files_dir("writer", RUN_ID) == f"agents/writer/{RUN_ID}"
    assert len(task_preview("가" * 2000).encode("utf-8")) <= 2048
    assert task_preview("짧다") == "짧다"
    assert child_correlation({"workflow_run_id": "x"}) is None
    assert child_correlation(None) is None
    parsed = child_correlation(
        {
            "parent_run_id": str(uuid.uuid4()),
            "parent_session_id": str(uuid.uuid4()),
            "root_run_id": str(uuid.uuid4()),
            "tool_call_id": "c",
            "depth": 2,
            "ancestors": [str(uuid.uuid4())],
        }
    )
    assert parsed is not None and parsed.depth == 2
