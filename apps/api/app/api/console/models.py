from fastapi import APIRouter, Depends

from app.api.console.llm import LlmStatusDep
from app.auth.dependencies import current_auth
from app.schemas.llm import ModelList

router = APIRouter(prefix="/models", tags=["llm"], dependencies=[Depends(current_auth)])


@router.get("", response_model=ModelList)
async def list_models(service: LlmStatusDep) -> ModelList:
    # 에이전트 빌더의 모델 선택용. 폴백 목록을 앞에 두고 라이브 목록을 뒤에 붙인다.
    # 폴백 목록의 모델에는 능력표(app/llm/catalog.py) 값이 붙는다.
    return await service.models()
