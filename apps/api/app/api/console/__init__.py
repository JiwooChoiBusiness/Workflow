from fastapi import APIRouter, Depends

from app.api.console import auth, llm, models, setup, workspace
from app.auth.dependencies import csrf_protect
from app.schemas.errors import ErrorResponse

# 변경 요청(POST·PUT·PATCH·DELETE)의 CSRF 검사는 콘솔 라우터 전체에 건다. 이후 마일스톤이
# 붙이는 콘솔 라우터도 여기에 include하면 같은 검사를 받는다.
# responses의 4XX는 FastAPI가 OpenAPI에 기본으로 넣는 422 {detail: [...]} 형식을 실제 형식으로
# 바꾼다. 화면은 OpenAPI에서 타입을 만든다(설계 7.1).
router = APIRouter(
    prefix="/api/console",
    dependencies=[Depends(csrf_protect)],
    responses={"4XX": {"model": ErrorResponse, "description": "오류"}},
)
for module in (setup, auth, workspace, llm, models):
    router.include_router(module.router)
