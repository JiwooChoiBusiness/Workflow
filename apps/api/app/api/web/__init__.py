from fastapi import APIRouter, Depends

from app.api.web import apps, sessions
from app.auth.dependencies import csrf_protect
from app.schemas.errors import ErrorResponse

# 웹앱(/apps/<slug>) 화면이 쓰는 API(설계 6.3). 콘솔과 같은 쿠키 로그인과 CSRF 검사를 쓰고,
# 발행되어 webapp_enabled인 에이전트의 webapp 세션만 다룬다.
router = APIRouter(
    prefix="/api/web",
    dependencies=[Depends(csrf_protect)],
    responses={"4XX": {"model": ErrorResponse, "description": "오류"}},
)
for module in (apps, sessions):
    router.include_router(module.router)
