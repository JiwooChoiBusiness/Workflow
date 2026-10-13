"""파일 올리기·내려받기(설계 6.4).

세션 파일은 세션에 속하므로(5.1 session_file.session_id) 올릴 때 대화를 정한다.
conversation_id를 주지 않으면 빈 대화를 하나 만들어 거기에 둔다. 이 파일은 그 대화의 메시지
첨부나 과업 입력으로 쓴다.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData, UploadFile

from app.api.console.sessions import SessionServiceDep
from app.api.downloads import stored_file_response
from app.api.errors import ApiError
from app.api.uploads import single_upload, upload_chunks
from app.api.v1.common import file_scope, key_scope
from app.auth.dependencies import ApiKeyAuth
from app.db.models.enums import SessionSource
from app.schemas.service_api import ExternalUser, FileUploadOut
from app.services.sessions import MAX_UPLOAD_BYTES

router = APIRouter(prefix="/files", tags=["service-api"])

_USER_MAX_CHARS = 255


def _form_text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        return None
    text = value.strip()
    return text or None


def _form_user(form: FormData) -> str | None:
    user = _form_text(form, "user")
    if user is not None and len(user) > _USER_MAX_CHARS:
        raise ApiError(422, "validation_error", f"user는 {_USER_MAX_CHARS}자 이하다")
    return user


def _form_conversation(form: FormData) -> uuid.UUID | None:
    raw = _form_text(form, "conversation_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(422, "validation_error", "conversation_id가 UUID가 아니다") from None


UPLOAD_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "user": {"type": "string"},
                        "conversation_id": {"type": "string", "format": "uuid"},
                    },
                }
            }
        },
    }
}


@router.post("/upload", status_code=201, response_model=FileUploadOut, openapi_extra=UPLOAD_OPENAPI)
async def upload_file(
    request: Request, key: ApiKeyAuth, sessions: SessionServiceDep
) -> FileUploadOut:
    """multipart/form-data: file, user(선택), conversation_id(선택)."""
    async with single_upload(request, max_bytes=MAX_UPLOAD_BYTES, what="파일") as upload:
        # single_upload가 읽어 둔 같은 폼이다(Starlette가 요청마다 한 번만 파싱한다).
        form = await request.form()
        user = _form_user(form)
        conversation_id = _form_conversation(form)
        if conversation_id is not None:
            scope = key_scope(key, user=user, only=SessionSource.API)
            await sessions.check_session(key.workspace_id, conversation_id, scope)
        else:
            created = await sessions.create_published(
                key.workspace_id,
                key.agent_id,
                source="api",
                api_key_id=key.key_id,
                external_user=user,
            )
            conversation_id = created.id
        stored = await sessions.upload(
            key.workspace_id,
            conversation_id,
            filename=upload.filename,
            content_type=upload.content_type,
            chunks=upload_chunks(upload),
        )
    return FileUploadOut(
        file_id=stored.id,
        filename=stored.filename,
        size=stored.size_bytes,
        mime=stored.mime,
        conversation_id=conversation_id,
    )


@router.get("/{file_id}/download", response_class=Response)
async def download_file(
    file_id: uuid.UUID,
    request: Request,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    user: Annotated[ExternalUser | None, Query()] = None,
) -> Response:
    """이 에이전트의 대화·과업 세션 파일(첨부, 런 결과)."""
    row = await sessions.scoped_file(key.workspace_id, file_id, file_scope(key, user=user))
    storage = request.app.state.storage
    stored_key = row.storage_key or ""
    if await storage.size(stored_key) is None:
        return JSONResponse(
            {"error": {"code": "file_missing", "message": "저장소에 파일이 없다"}},
            status_code=404,
        )
    return stored_file_response(storage, stored_key, filename=row.filename, mime=row.mime)
