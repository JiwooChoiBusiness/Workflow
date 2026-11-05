"""워크플로우 카드에서 고른 보기를 자식 런의 보기로 되돌리기(WorkflowRunService.provide_input).

엔진은 agent 노드 카드의 보기를 워크플로우 비밀 env로 가려 남긴다. 자식은 자기 런에 저장한 보기로
답을 검증하므로 서비스가 같은 자리의 보기로 바꿔 넘긴다. 가려서 구분할 수 없게 된 보기는 422다.
"""

import pytest

from app.agent.interactions import InteractionReply
from app.services.errors import ServiceError
from app.services.workflow_runs import unmask_answer

# 비밀 탐지 도구에 걸리지 않게 실행 시 조립한다.
TOKEN = "tok-" + "51ab" * 5
OTHER = "tok-" + "c03f" * 5


def test_an_answer_picked_from_a_masked_option_is_forwarded_as_the_raw_option() -> None:
    reply = InteractionReply(answer="***", note="메모")

    forwarded = unmask_answer(reply, ["***", "아니오"], [TOKEN, "아니오"])

    assert forwarded.answer == TOKEN and forwarded.note == "메모"
    assert reply.answer == "***"
    # 같은 원문이 두 자리에 있어 가린 보기가 같아졌으면 어느 쪽을 골라도 같은 답이다.
    assert unmask_answer(reply, ["***", "***"], [TOKEN, TOKEN]).answer == TOKEN


def test_unchanged_options_and_free_text_pass_through() -> None:
    shown, raw = ["***", "아니오"], [TOKEN, "아니오"]

    for reply in (
        InteractionReply(answer="아니오"),
        InteractionReply(answer="보기 밖의 답"),
        InteractionReply(answer=TOKEN),
        InteractionReply(decision="approve", session_auto_approve=True),
    ):
        assert unmask_answer(reply, shown, raw) == reply
    # 보기가 없거나 자리 수가 맞지 않으면 대응시키지 않고 자식의 검증에 맡긴다.
    masked = InteractionReply(answer="***")
    assert unmask_answer(masked, None, raw) == masked
    assert unmask_answer(masked, shown, None) == masked
    assert unmask_answer(masked, ["***"], raw) == masked


def test_options_that_collide_after_masking_are_refused() -> None:
    shown, raw = ["***", "***", "아니오"], [TOKEN, OTHER, "아니오"]
    masked = InteractionReply(answer="***")

    with pytest.raises(ServiceError) as caught:
        unmask_answer(masked, shown, raw)
    # 보기 자체가 "***"여도 가린 보기와 겹치면 어느 쪽을 골랐는지 알 수 없다.
    with pytest.raises(ServiceError) as literal:
        unmask_answer(masked, ["***", "***"], ["***", TOKEN])

    assert (caught.value.status, caught.value.code) == (422, "answer_ambiguous")
    assert literal.value.code == "answer_ambiguous"
    # 겹치지 않은 보기는 그대로 고를 수 있다.
    assert unmask_answer(InteractionReply(answer="아니오"), shown, raw).answer == "아니오"
