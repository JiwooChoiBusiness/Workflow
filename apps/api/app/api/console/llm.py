from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.errors import ApiError
from app.auth.dependencies import current_auth
from app.schemas.llm import LlmStatus, LlmTestRequest, LlmTestResult
from app.services.llm_status import LlmStatusService

router = APIRouter(prefix="/llm", tags=["llm"], dependencies=[Depends(current_auth)])


def get_llm_status(request: Request) -> LlmStatusService:
    return request.app.state.llm_status


LlmStatusDep = Annotated[LlmStatusService, Depends(get_llm_status)]


@router.get("", response_model=LlmStatus)
async def llm_status(service: LlmStatusDep) -> LlmStatus:
    return service.status()


@router.post("/test", response_model=LlmTestResult)
async def llm_test(service: LlmStatusDep, body: LlmTestRequest | None = None) -> LlmTestResult:
    key_index = body.key_index if body is not None else None
    if key_index is not None and key_index >= service.key_count:
        raise ApiError(
            422,
            "invalid_key_index",
            f"key_index는 0~{service.key_count - 1} 범위여야 한다",
            details={"key_count": service.key_count},
        )
    return await service.test(key_index)
