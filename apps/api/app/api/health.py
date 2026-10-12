from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    # 아직 붙은 의존 서비스가 없다. DB·Redis 연결이 생기면 여기서 확인한다.
    return {"status": "ok"}
