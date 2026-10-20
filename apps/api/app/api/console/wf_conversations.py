"""챗플로우 대화 콘솔 API(2단계 설계 6.1).

조회·제목·삭제·메시지·대화 변수와 로그 화면용 목록이다. 미리보기 대화를 만들고 고르는 경로는
workflows.py(/workflows/{id}/preview/conversations)에 있다. 메시지를 보내면 런이 생기고, 런의
이벤트·중지·승인 응답은 /wf-runs/{rid}의 경로를 쓴다.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.console.runs import SSE_CONTENT
from app.api.console.wf_runs import WfRunServiceDep
from app.api.downloads import stored_download
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.wf_conversation import (
    ConversationSource,
    WfChatResult,
    WfConversationDetail,
    WfConversationList,
    WfConversationOut,
    WfConversationPatch,
    WfConversationVariables,
    WfMessageCreate,
    WfMessageList,
)
from app.services.logs import assume_local
from app.services.wf_conversations import StartedChat, WfConversationService
from app.services.workflow_files import WorkflowFileService
from app.streaming.sse import sse_response

router = APIRouter(prefix="/wf-conversations", tags=["wf-conversations"])


def get_conversation_service(
    request: Request, db: DbDep, settings: SettingsDep, runs: WfRunServiceDep
) -> WfConversationService:
    state = request.app.state
    return WfConversationService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.workflow_supervisor,
        storage=state.storage,
        runs=runs,
    )


ConversationServiceDep = Annotated[WfConversationService, Depends(get_conversation_service)]

MESSAGE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "model": WfChatResult,
        "description": "response_mode=streaming(기본)이면 SSE, blocking이면 WfChatResult",
        "content": SSE_CONTENT,
    }
}


def chat_headers(started: StartedChat) -> dict[str, str]:
    # 첫 이벤트를 읽기 전에도 중지·응답과 메시지 갱신에 쓸 수 있게 헤더로도 준다.
    return {
        "X-Run-Id": str(started.run_id),
        "X-Conversation-Id": str(started.conversation_id),
        "X-Message-Id": str(started.message_id),
    }


async def respond(
    service: WfConversationService,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: WfMessageCreate,
) -> Response:
    """메시지를 보내고 SSE 또는 blocking 결과로 답한다. 웹앱도 범위를 본 뒤 이것을 쓴다."""
    started = await service.send_message(workspace_id, conversation_id, body, subscribe=True)
    headers = chat_headers(started)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await service.wait_result(workspace_id, started)
    return JSONResponse(result.model_dump(mode="json"), headers=headers)


@router.get("", response_model=WfConversationList)
async def list_conversations(
    auth: CurrentAuth,
    service: ConversationServiceDep,
    workflow_id: uuid.UUID | None = None,
    source: ConversationSource | None = None,
    api_key_id: uuid.UUID | None = None,
    external_user: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfConversationList:
    """대화 목록(최근 대화 순, 로그 화면용). 지운 대화는 나오지 않는다.

    기간은 대화를 만든 시각 기준 [since, until)이다. 시간대 없이 온 시각은 서버 PC 시간대로 본다.
    """
    return await service.list_conversations(
        auth.workspace_id,
        workflow_id=workflow_id,
        source=source,
        api_key_id=api_key_id,
        external_user=external_user,
        since=assume_local(since),
        until=assume_local(until),
        limit=limit,
        offset=offset,
    )


@router.get("/{conversation_id}", response_model=WfConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, service: ConversationServiceDep
) -> WfConversationDetail:
    """active_run이 있으면 GET /wf-runs/{id}/events?after=0으로 다시 붙는다."""
    return await service.get(auth.workspace_id, conversation_id)


@router.patch("/{conversation_id}", response_model=WfConversationOut)
async def patch_conversation(
    conversation_id: uuid.UUID,
    body: WfConversationPatch,
    auth: CurrentAuth,
    service: ConversationServiceDep,
) -> WfConversationOut:
    return await service.update(auth.workspace_id, conversation_id, body)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, service: ConversationServiceDep
) -> None:
    """돌던 런을 멈추고 지운다. 런·메시지는 로그로 남고 메시지에 붙인 업로드는 지운다."""
    await service.delete(auth.workspace_id, conversation_id)


@router.get("/{conversation_id}/messages", response_model=WfMessageList)
async def list_messages(
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    service: ConversationServiceDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> WfMessageList:
    """seq 오름차순. 기본은 마지막 100개이고, 앞을 더 보려면 before_seq=첫 메시지의 seq."""
    return await service.list_messages(
        auth.workspace_id, conversation_id, before_seq=before_seq, limit=limit
    )


@router.post("/{conversation_id}/messages", response_model=None, responses=MESSAGE_RESPONSES)
async def send_message(
    conversation_id: uuid.UUID,
    body: WfMessageCreate,
    auth: CurrentAuth,
    service: ConversationServiceDep,
) -> Response:
    """메시지를 보내고 런을 만든다. 진행 중인 런(입력 대기 포함)이 있으면 409 run_in_progress.

    streaming이면 첫 이벤트가 workflow.queued이고 workflow.completed 뒤에 닫힌다. 성공한 런은 그
    앞에 answer.completed{text, message_id}를 낸다.
    """
    return await respond(service, auth.workspace_id, conversation_id, body)


@router.get("/{conversation_id}/variables", response_model=WfConversationVariables)
async def conversation_variables(
    conversation_id: uuid.UUID, auth: CurrentAuth, service: ConversationServiceDep
) -> WfConversationVariables:
    """대화 변수의 지금 값. 성공한 런이 끝날 때만 바뀐다."""
    return await service.variables(auth.workspace_id, conversation_id)


@router.get("/{conversation_id}/files/{file_id}/download", response_class=Response)
async def download_conversation_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DbDep,
) -> Response:
    """메시지에 붙은 업로드. 대화의 첨부 링크가 쓴다."""
    storage = request.app.state.storage
    target = await WorkflowFileService(db, storage).conversation_file(
        auth.workspace_id, conversation_id, file_id
    )
    return await stored_download(
        storage, target.storage_key, filename=target.filename, mime=target.mime
    )
