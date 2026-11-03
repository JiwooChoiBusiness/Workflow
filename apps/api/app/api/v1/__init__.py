from fastapi import APIRouter

from app.api.v1 import (
    agent_tasks,
    chat_messages,
    conversations,
    datasets,
    files,
    info,
    messages,
    workflows,
)
from app.schemas.errors import ErrorResponse

# 서비스 API(설계 6.4, 2단계 6.3, 3단계 8.5). Authorization: Bearer <API 키>로 인증하고(쿠키·CSRF
# 검사 없음), 키에 묶인 에이전트나 워크플로우의 발행 버전, 또는 지식 베이스 하나(datasets)를 쓴다.
# 키당 분당 60번(app/auth/dependencies.py api_key_auth). 경로가 받지 않는 대상의 키는 400
# wrong_target이다.
router = APIRouter(
    prefix="/api/v1",
    responses={"4XX": {"model": ErrorResponse, "description": "오류"}},
)
for module in (
    info,
    chat_messages,
    conversations,
    messages,
    files,
    agent_tasks,
    workflows,
    datasets,
):
    router.include_router(module.router)
