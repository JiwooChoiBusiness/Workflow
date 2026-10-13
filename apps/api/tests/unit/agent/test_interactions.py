"""승인 판정과 질문·승인 응답 검사(설계 3.10, 4.9)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.agent.interactions import (
    InteractionError,
    InteractionReply,
    PendingInteraction,
    effective_approval_mode,
    rejection_text,
    requires_approval,
    resolve_reply,
)

SHELLISH = ["shell", "run_python", "install_packages"]
SAFE = ["read_file", "write_file", "edit_file", "list_files", "ask_user"]


@pytest.mark.parametrize("tool", SHELLISH)
def test_all_shell_requires_approval_for_processes(tool: str) -> None:
    assert requires_approval("all_shell", tool, {})
    assert not requires_approval("http_writes", tool, {})
    assert not requires_approval("off", tool, {})


@pytest.mark.parametrize("tool", SAFE)
def test_file_tools_never_need_approval(tool: str) -> None:
    for mode in ("all_shell", "http_writes", "off"):
        assert not requires_approval(mode, tool, {})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("method", "expected"),
    [("GET", False), ("HEAD", False), ("get", False), ("POST", True), ("DELETE", True)],
)
def test_http_writes_need_approval_in_both_strict_modes(method: str, expected: bool) -> None:
    for mode in ("all_shell", "http_writes"):
        assert requires_approval(mode, "http_request", {"method": method}) is expected  # pyright: ignore[reportArgumentType]
    assert not requires_approval("off", "http_request", {"method": method})


def test_session_override_only_loosens() -> None:
    assert effective_approval_mode("all_shell", "off") == "off"
    assert effective_approval_mode("all_shell", None) == "all_shell"
    assert effective_approval_mode("http_writes", "something") == "http_writes"


def pending(kind: str = "approval", **fields: Any) -> PendingInteraction:
    values: dict[str, Any] = {
        "interaction_id": "int-1",
        "kind": kind,
        "tool_call_id": "call_1",
        "tool_name": "shell" if kind == "approval" else "ask_user",
        "message_id": uuid.uuid4(),
        "step": 1,
        "created_at": datetime.now(UTC),
    }
    values.update(fields)
    return PendingInteraction.model_validate(values)


def test_approval_reply() -> None:
    payload = resolve_reply(
        pending(), InteractionReply(decision="approve", session_auto_approve=True)
    )

    assert (payload.kind, payload.decision, payload.session_auto_approve) == (
        "approval",
        "approve",
        True,
    )
    assert payload.resolved_event_data() == {
        "interaction_id": "int-1",
        "kind": "approval",
        "decision": "approve",
        "note": None,
        "session_auto_approve": True,
    }


def test_reject_never_turns_on_auto_approve() -> None:
    payload = resolve_reply(
        pending(), InteractionReply(decision="reject", note="안 됨", session_auto_approve=True)
    )

    assert payload.session_auto_approve is False
    assert payload.note == "안 됨"


@pytest.mark.parametrize(
    ("kind", "reply", "code"),
    [
        ("approval", InteractionReply(answer="네"), "decision_required"),
        ("approval", InteractionReply(), "decision_required"),
        ("question", InteractionReply(decision="approve"), "answer_required"),
        ("question", InteractionReply(), "answer_required"),
    ],
)
def test_reply_must_match_the_kind(kind: str, reply: InteractionReply, code: str) -> None:
    with pytest.raises(InteractionError) as caught:
        resolve_reply(pending(kind), reply)

    assert caught.value.code == code


def test_question_with_fixed_options() -> None:
    fixed = pending("question", question="색?", options=["빨강", "파랑"], allow_free_text=False)

    assert resolve_reply(fixed, InteractionReply(answer="파랑")).answer == "파랑"
    with pytest.raises(InteractionError):
        resolve_reply(fixed, InteractionReply(answer="초록"))
    free = pending("question", question="색?", options=["빨강"], allow_free_text=True)
    assert resolve_reply(free, InteractionReply(answer="초록")).answer == "초록"


def test_rejection_text() -> None:
    assert rejection_text("위험") == "사용자가 이 작업을 거절했습니다: 위험"
    assert rejection_text(None) == "사용자가 이 작업을 거절했습니다."
    assert rejection_text("  ") == "사용자가 이 작업을 거절했습니다."


def test_event_data_by_kind_and_expiry() -> None:
    approval = pending(input={"command": "dir"}, cwd=".", timeout_sec=120)
    question = pending("question", question="이름?", options=None, allow_free_text=True)

    assert approval.event_data()["input"] == {"command": "dir"}
    assert "question" not in approval.event_data()
    assert question.event_data()["question"] == "이름?"
    assert "input" not in question.event_data()
    old = pending(created_at=datetime.now(UTC) - timedelta(hours=25))
    assert old.expired(datetime.now(UTC), 24)
    assert not approval.expired(datetime.now(UTC), 24)
