from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


async def test_healthz_and_readyz() -> None:
    # model_validate는 .env와 환경변수를 읽지 않으므로 로컬 설정과 무관하게 돈다.
    app = create_app(Settings.model_validate({}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/healthz", "/readyz"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.json() == {"status": "ok"}
