from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.errors import ErrorBody, ErrorResponse

# 오류 형식은 전 API 공통 {"error": {"code", "message", "details"?}}이다(설계 6.4).
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


class ApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


async def _http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        return error_response(500, "internal_error", _phrase(500))
    headers = exc.headers
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    if isinstance(exc, ApiError):
        return error_response(
            exc.status_code, exc.code, exc.message, details=exc.details, headers=headers
        )
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    return error_response(
        exc.status_code,
        _STATUS_CODES.get(exc.status_code, f"http_{exc.status_code}"),
        message or _phrase(exc.status_code),
        headers=headers,
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    # 기본 응답에 들어가는 input(보낸 값 그대로)은 비밀번호일 수 있어 뺀다.
    details = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg", ""), "type": error.get("type")}
        for error in errors
    ]
    return error_response(422, "validation_error", "요청 값이 올바르지 않다", details=details)


async def _unhandled(request: Request, exc: Exception) -> Response:
    # 응답만 같은 형식으로 바꾼다. 예외는 Starlette가 다시 던져 uvicorn이 로그를 남긴다.
    return error_response(500, "internal_error", "서버 내부 오류")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
