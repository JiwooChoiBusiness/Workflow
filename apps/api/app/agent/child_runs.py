"""하위 에이전트 호출(call_agent)의 순수 규칙(오케스트레이션 설계 2.1, 3.3, 3.4, 3.9, 9장).

루프·런 실행기·2단계 agent 노드가 함께 쓰는 타입과 함수만 둔다. DB와 HTTP를 모르므로 자식 런을
만드는 일은 루프가 LoopPorts.start_child로 밖(app/services/child_runs.py)에 맡긴다.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent.tools.base import excerpt, utf8_len
from app.schemas.agent_version import ResultSpec
from app.schemas.events import ToolCallStatus, UsageTotal

CALL_AGENT = "call_agent"
MAX_TASK_CHARS = 20_000
MAX_CHILD_FILES = 20
MAX_CHILD_FILE_BYTES = 50 * 1024 * 1024
MAX_CHILD_FILES_BYTES = 200 * 1024 * 1024
TASK_PREVIEW_BYTES = 2 * 1024
# 자식 결과를 부모에 싣는 크기(3.4, 3.9). 글은 도구 결과 규칙(앞 24KB + 뒤 8KB)으로 자른다.
RESULT_JSON_BYTES = 32 * 1024
RESULT_PREVIEW_BYTES = 24 * 1024
# run.resume_payload 열 하나에 들어가는 상한.
PAYLOAD_MAX_BYTES = 64 * 1024
SHRUNK_TEXT_BYTES = 8 * 1024
AGENTS_DIR = "agents"
RESULT_FILE = "result.json"

VersionKind = Literal["draft", "published"]
ChildEndStatus = Literal["succeeded", "failed", "cancelled"]


class SubAgentChoice(BaseModel):
    """이 런이 call_agent로 부를 수 있는 에이전트 하나. 런을 시작할 때 실행기가 만든다."""

    agent_id: uuid.UUID
    slug: str
    name: str
    # agent.description(버전과 무관한 에이전트 정보라 지금 값)이다.
    description: str = ""
    result: ResultSpec = Field(default_factory=ResultSpec)
    approval_mode: Literal["inherit", "off"] = "inherit"

    @property
    def schema_from_call(self) -> bool:
        """호출할 때 모델이 output_schema로 결과 스키마를 정하는 항목인지."""
        return self.result.format == "json" and self.result.json_schema is None


class ChildCall(BaseModel):
    """call_agent 호출 한 번. 모델이 준 입력과 고른 항목."""

    tool_call_id: str
    # 멈출 스텝 메시지. 넘길 파일의 session_file 행이 이 메시지를 가리킨다.
    message_id: uuid.UUID
    agent: SubAgentChoice
    task: str
    files: list[str] = Field(default_factory=list[str])
    output_schema: dict[str, Any] | None = None


class ChildStarted(BaseModel):
    """자식 런 행을 만들었다(큐에는 부모가 대기 상태를 저장한 뒤에 넣는다, 3.3)."""

    child_run_id: uuid.UUID
    child_session_id: uuid.UUID
    agent_id: uuid.UUID
    agent_slug: str
    agent_name: str
    version: VersionKind
    depth: int
    result_format: Literal["text", "json"]
    task_preview: str

    def event_data(self, tool_call_id: str) -> dict[str, Any]:
        """child_run.started의 data."""
        return {"tool_call_id": tool_call_id, **self.model_dump(mode="json")}


class ChildStartError(BaseModel):
    """자식을 만들지 못했다. 런은 계속하고 이 내용이 is_error 도구 결과가 된다(3.3)."""

    code: str
    message: str

    def content(self) -> str:
        return f"{self.code}: {self.message}"


class ChildCorrelation(BaseModel):
    """자식 런의 run.input.correlation(7장).

    조회 조건에는 쓰지 않고 깊이·순환 검사와 표시에 읽는다.
    """

    parent_run_id: uuid.UUID
    parent_session_id: uuid.UUID
    root_run_id: uuid.UUID
    tool_call_id: str
    depth: int
    # 뿌리부터 부모까지의 에이전트 id. 이 런이 다시 부를 수 없는 에이전트다(순환).
    ancestors: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])
    parent_agent_name: str = ""


def child_correlation(raw: Mapping[str, Any] | None) -> ChildCorrelation | None:
    """call_agent가 만든 과업 런의 correlation이면 그 값. 워크플로우 노드 자식 등은 None."""
    if not raw or "parent_run_id" not in raw:
        return None
    try:
        return ChildCorrelation.model_validate(dict(raw))
    except ValidationError:
        return None


class ChildError(BaseModel):
    code: str
    message: str = ""


class ChildFile(BaseModel):
    """자식 런의 결과 파일 하나와 부모 작업공간의 복사 위치."""

    session_file_id: uuid.UUID | None = None
    # 자식 작업공간 상대 경로.
    child_path: str
    # 부모 작업공간 상대 경로. 복사하지 않는 항목(files=false)이나 복사 전이면 None.
    parent_path: str | None = None
    size: int = 0
    downloadable: bool = True
    copy_failed: bool = False


class ChildResultSummary(BaseModel):
    """자식 런이 끝났을 때 부모 재개에 싣는 값(ResumePayload.child, 3.4)."""

    run_id: uuid.UUID
    session_id: uuid.UUID | None = None
    agent_slug: str
    agent_name: str
    result_format: Literal["text", "json"] = "text"
    status: ChildEndStatus
    error: ChildError | None = None
    # 스텝 한도에 닿아 끝났으면 "steps"(succeeded지만 머리말에 적는다).
    limit: Literal["steps"] | None = None
    text: str = ""
    result: dict[str, Any] | None = None
    # JSON 결과가 32KB를 넘으면 result 대신 앞부분만 싣고 전문은 부모 작업공간 파일로 둔다.
    result_preview: str | None = None
    result_path: str | None = None
    files: list[ChildFile] = Field(default_factory=list[ChildFile])
    files_omitted: int = 0
    steps: int = 0
    usage: UsageTotal = Field(default_factory=UsageTotal)
    # 자식의 벽시계 시간(ended_at − queued_at). tool_call.completed의 duration_ms가 된다.
    duration_ms: int = 0

    @property
    def result_too_large(self) -> bool:
        return self.result is None and self.result_preview is not None


def task_preview(task: str) -> str:
    data = task.encode("utf-8")
    if len(data) <= TASK_PREVIEW_BYTES:
        return task
    return data[:TASK_PREVIEW_BYTES].decode("utf-8", errors="ignore")


def child_files_dir(slug: str, run_id: uuid.UUID) -> str:
    """부모 작업공간에서 자식 결과 파일이 놓이는 폴더(4.2)."""
    return f"{AGENTS_DIR}/{slug}/{run_id}"


def can_call_children(depth: int, max_depth: int, max_child_calls: int) -> bool:
    """이 런에 call_agent를 붙일지. 깊이 한도에 닿았거나 호출 수가 0이면 붙이지 않는다(3.9)."""
    return depth < max_depth and max_child_calls > 0


def summarize_output(
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID | None,
    agent_slug: str,
    agent_name: str,
    result_format: Literal["text", "json"],
    status: str,
    output: Mapping[str, Any] | None,
    error: Mapping[str, Any] | None,
    steps: int,
    usage: Mapping[str, Any] | None,
    duration_ms: int,
) -> ChildResultSummary:
    """자식 런의 run.output(4.10)을 부모에게 줄 요약으로 줄인다. 크기는 3.4·3.9 규칙대로 자른다."""
    out: Mapping[str, Any] = output or {}
    end: ChildEndStatus = (
        "succeeded" if status == "succeeded" else "cancelled" if status == "cancelled" else "failed"
    )
    child_error: ChildError | None = None
    if error:
        child_error = ChildError(
            code=str(error.get("code") or end), message=str(error.get("message") or "")
        )
    elif end == "failed":
        child_error = ChildError(code="failed")
    raw_result = out.get("result")
    result: dict[str, Any] | None = None
    preview: str | None = None
    if isinstance(raw_result, dict):
        dumped = json.dumps(raw_result, ensure_ascii=False, indent=2)
        if utf8_len(dumped) <= RESULT_JSON_BYTES:
            result = dict(raw_result)  # pyright: ignore[reportUnknownArgumentType]
        else:
            preview = dumped.encode("utf-8")[:RESULT_PREVIEW_BYTES].decode("utf-8", errors="ignore")
    files: list[ChildFile] = []
    raw_files = out.get("files")
    if isinstance(raw_files, list):
        for item in raw_files:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, Mapping):
                continue
            entry: Mapping[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
            raw_id = entry.get("id")
            try:
                file_id = uuid.UUID(str(raw_id)) if raw_id is not None else None
            except ValueError:
                file_id = None
            files.append(
                ChildFile(
                    session_file_id=file_id,
                    child_path=str(entry.get("path") or ""),
                    size=int(entry.get("size_bytes") or 0),
                    downloadable=bool(entry.get("downloadable", True)),
                )
            )
    limit = out.get("limit")
    summary = ChildResultSummary(
        run_id=run_id,
        session_id=session_id,
        agent_slug=agent_slug,
        agent_name=agent_name,
        result_format=result_format,
        status=end,
        error=child_error,
        limit="steps" if limit == "steps" else None,
        text=excerpt(str(out.get("text") or "")),
        result=result,
        result_preview=preview,
        files=files,
        steps=steps,
        usage=UsageTotal.model_validate(dict(usage or {})),
        duration_ms=max(duration_ms, 0),
    )
    return fit_payload(summary)


def _payload_size(summary: ChildResultSummary) -> int:
    return utf8_len(summary.model_dump_json())


def fit_payload(summary: ChildResultSummary) -> ChildResultSummary:
    """run.resume_payload 64KB 안에 들게 줄인다. 글을 먼저 줄이고 그다음 파일 목록을 줄인다."""
    if _payload_size(summary) <= PAYLOAD_MAX_BYTES:
        return summary
    text = summary.text
    if utf8_len(text) > SHRUNK_TEXT_BYTES:
        head = text.encode("utf-8")[:SHRUNK_TEXT_BYTES].decode("utf-8", errors="ignore")
        summary = summary.model_copy(
            update={"text": f"{head}\n[... 결과 글이 길어 앞부분만 싣는다 ...]"}
        )
    files = list(summary.files)
    omitted = summary.files_omitted
    while files and _payload_size(summary) > PAYLOAD_MAX_BYTES:
        cut = max(len(files) // 2, 1)
        omitted += len(files) - cut if len(files) > 1 else 1
        files = files[:cut] if len(files) > 1 else []
        summary = summary.model_copy(update={"files": files, "files_omitted": omitted})
    return summary


def tool_status(summary: ChildResultSummary) -> ToolCallStatus:
    if summary.status == "succeeded":
        return "ok"
    return "cancelled" if summary.status == "cancelled" else "error"


def _headline(summary: ChildResultSummary) -> str:
    who = (
        f"{summary.agent_slug}({summary.agent_name})" if summary.agent_name else summary.agent_slug
    )
    tail = f"스텝 {summary.steps}, run_id {summary.run_id}"
    if summary.status == "succeeded":
        limit = ", 스텝 한도에 닿아 멈췄다" if summary.limit == "steps" else ""
        return f"[하위 에이전트 {who} 완료: succeeded{limit}, {tail}]"
    label = "취소" if summary.status == "cancelled" else "실패"
    error = summary.error
    code = error.code if error is not None else summary.status
    reason = f" — {error.message}" if error is not None and error.message else ""
    return f"[하위 에이전트 {who} {label}: {code}{reason}, {tail}]"


def _file_lines(summary: ChildResultSummary) -> list[str]:
    if not summary.files and not summary.files_omitted:
        return []
    copied = any(item.parent_path is not None for item in summary.files)
    lines = [
        "결과 파일(부모 작업공간 상대 경로):"
        if copied
        else "결과 파일(하위 에이전트 세션에 있다. 이 작업공간으로는 복사하지 않았다):"
    ]
    for item in summary.files:
        size = f"{item.size:,}바이트"
        if not item.downloadable:
            lines.append(f"- {item.child_path} ({size}, 보존하지 않음)")
        elif item.copy_failed:
            lines.append(f"- {item.child_path} ({size}, 복사 실패)")
        else:
            lines.append(f"- {item.parent_path or item.child_path} ({size})")
    if summary.files_omitted:
        lines.append(f"- 그 밖 {summary.files_omitted}개(목록이 길어 생략)")
    return lines


def _body(summary: ChildResultSummary) -> str:
    if summary.result_format == "json" and summary.result is not None:
        return json.dumps(summary.result, ensure_ascii=False, indent=2)
    if summary.result_format == "json" and summary.result_preview is not None:
        where = summary.result_path or "하위 에이전트의 결과"
        return (
            f"(JSON 결과가 {RESULT_JSON_BYTES // 1024}KB를 넘어 전문은 {where}에 있다. 앞부분:)\n"
            f"{summary.result_preview}"
        )
    return summary.text or "(글 없음)"


def format_child_result(summary: ChildResultSummary) -> tuple[str, bool]:
    """call_agent의 tool_result 본문과 is_error(3.4)."""
    parts = [_headline(summary), *_file_lines(summary), "---", _body(summary)]
    return excerpt("\n".join(parts)), summary.status != "succeeded"


def merge_text_parts(parts: Sequence[str]) -> str:
    return "\n".join(part for part in parts if part)
