"""승인·질문 정책(설계 3.10, 4.9).

도구 호출에 승인이 필요한지는 이 모듈 한 곳에서만 정한다(12.3). 루프는 승인이나 질문이 필요하면
PendingInteraction을 남기고 런을 waiting_input으로 끝낸다. 답이 오면 API가 ResumePayload를 만들어
같은 런을 다시 큐에 넣는다.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApprovalMode
from app.schemas.events import InteractionKind

SHELL_TOOLS = frozenset({"shell", "run_python", "install_packages"})
READ_ONLY_HTTP_METHODS = frozenset({"GET", "HEAD"})
# session.approval_override가 가질 수 있는 유일한 값. 에이전트 설정보다 느슨한 쪽으로만 덮어쓴다.
SESSION_AUTO_APPROVE = "off"
REJECTED_TEXT = "사용자가 이 작업을 거절했습니다"

Decision = Literal["approve", "reject"]


def effective_approval_mode(run_mode: ApprovalMode, session_override: str | None) -> ApprovalMode:
    return "off" if session_override == SESSION_AUTO_APPROVE else run_mode


def requires_approval(mode: ApprovalMode, tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    """all_shell은 http_writes의 상위 집합이다(12.2)."""
    if mode == "off":
        return False
    if tool_name == "http_request":
        method = str(tool_input.get("method", "GET")).upper()
        return method not in READ_ONLY_HTTP_METHODS
    return mode == "all_shell" and tool_name in SHELL_TOOLS


def rejection_text(note: str | None) -> str:
    # 오류가 아니라 사용자 결정이라 is_error=false로 보낸다. 모델이 다른 방법을 찾게 한다(3.10).
    note = (note or "").strip()
    return f"{REJECTED_TEXT}: {note}" if note else f"{REJECTED_TEXT}."


class PendingInteraction(BaseModel):
    """run.pending_interaction 열. 재개에 필요한 루프 상태도 함께 둔다."""

    interaction_id: str
    kind: InteractionKind
    tool_call_id: str
    tool_name: str
    # 멈춘 스텝의 assistant 메시지. 그 메시지의 tool_use를 이어서 처리한다.
    message_id: uuid.UUID
    step: int
    # 병렬 호출 묶음 중 이미 결과가 나온 호출(4.4).
    completed_tool_call_ids: list[str] = Field(default_factory=list[str])
    # waiting_input 대기 시간을 빼고 실행한 시간. max_run_seconds 계산에 이어 쓴다(4.6).
    elapsed_ms: int = 0
    created_at: datetime
    question: str | None = None
    options: list[str] | None = None
    allow_free_text: bool | None = None
    input: dict[str, Any] | None = None
    cwd: str | None = None
    timeout_sec: int | None = None

    def event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "interaction_id": self.interaction_id,
            "kind": self.kind,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
        }
        if self.kind == "question":
            data.update(
                question=self.question,
                options=self.options,
                allow_free_text=self.allow_free_text,
            )
        else:
            data.update(input=self.input, cwd=self.cwd, timeout_sec=self.timeout_sec)
        return data

    def expired(self, now: datetime, ttl_hours: float) -> bool:
        return now - self.created_at > timedelta(hours=ttl_hours)


class InteractionReply(BaseModel):
    """POST /runs/{id}/input 본문에서 응답 부분."""

    answer: str | None = None
    decision: Decision | None = None
    note: str | None = None
    session_auto_approve: bool = False


class ResumePayload(BaseModel):
    """run.resume_payload 열."""

    interaction_id: str
    kind: InteractionKind
    answer: str | None = None
    decision: Decision | None = None
    note: str | None = None
    session_auto_approve: bool = False

    def resolved_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"interaction_id": self.interaction_id, "kind": self.kind}
        if self.kind == "question":
            data["answer"] = self.answer
        else:
            data.update(
                decision=self.decision,
                note=self.note,
                session_auto_approve=self.session_auto_approve,
            )
        return data


class InteractionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_reply(pending: PendingInteraction, reply: InteractionReply) -> ResumePayload:
    if pending.kind == "question":
        if reply.answer is None or reply.decision is not None:
            raise InteractionError("answer_required", "질문에는 answer로 답한다")
        allow_free = pending.allow_free_text is not False
        if pending.options and not allow_free and reply.answer not in pending.options:
            raise InteractionError(
                "answer_not_in_options", "이 질문은 보기 중 하나로만 답할 수 있다"
            )
        return ResumePayload(
            interaction_id=pending.interaction_id, kind="question", answer=reply.answer
        )
    if reply.decision is None or reply.answer is not None:
        raise InteractionError(
            "decision_required", "승인 요청에는 decision(approve 또는 reject)으로 답한다"
        )
    approve = reply.decision == "approve"
    return ResumePayload(
        interaction_id=pending.interaction_id,
        kind="approval",
        decision=reply.decision,
        note=None if approve else reply.note,
        session_auto_approve=approve and reply.session_auto_approve,
    )
