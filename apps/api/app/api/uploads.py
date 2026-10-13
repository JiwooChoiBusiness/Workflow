"""멀티파트 업로드 받기.

FastAPI의 UploadFile 매개변수는 의존성(로그인·CSRF 검사)보다 먼저 본문 전체를 임시 파일로 받는다.
여기서는 그 검사가 끝난 핸들러 안에서 Content-Length를 먼저 보고 나서 본문을 읽는다.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from starlette.datastructures import UploadFile

from app.api.errors import ApiError
from app.storage.base import CHUNK_SIZE

# 파일 말고 멀티파트 경계·헤더에 드는 몫.
MULTIPART_OVERHEAD = 64 * 1024


def multipart_openapi(field: str = "file") -> dict[str, Any]:
    """openapi_extra. 화면이 OpenAPI에서 타입을 만들 때 요청 모양을 알 수 있게 한다."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": [field],
                        "properties": {field: {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    }


def _check_length(request: Request, max_bytes: int, what: str) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        raise ApiError(411, "length_required", "Content-Length 헤더가 필요하다")
    try:
        length = int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "Content-Length 값이 올바르지 않다") from None
    if length > max_bytes + MULTIPART_OVERHEAD:
        raise ApiError(
            413,
            "file_too_large",
            f"{what}은(는) {max_bytes // 2**20}MB 이하여야 한다",
            details={"max_bytes": max_bytes},
        )


@asynccontextmanager
async def single_upload(
    request: Request, *, max_bytes: int, what: str, field: str = "file"
) -> AsyncIterator[UploadFile]:
    """multipart/form-data의 파일 하나. 블록을 나가면 임시 파일을 지운다."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(415, "multipart_required", "multipart/form-data로 파일을 보낸다")
    _check_length(request, max_bytes, what)
    form = await request.form(max_files=1, max_fields=4)
    try:
        item = form.get(field)
        if not isinstance(item, UploadFile):
            raise ApiError(
                422,
                "validation_error",
                "요청 값이 올바르지 않다",
                details=[{"loc": ["body", field], "msg": "파일이 없다", "type": "missing"}],
            )
        yield item
    finally:
        await form.close()


async def upload_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(CHUNK_SIZE):
        yield chunk
