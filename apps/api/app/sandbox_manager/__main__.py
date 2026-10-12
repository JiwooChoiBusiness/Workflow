import uvicorn
from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging

PORT = 7100

app = FastAPI(title="sandbox-manager")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    configure_logging(get_settings().log_level)
    # log_config=None이어야 uvicorn이 자기 로깅 설정으로 JSON 핸들러를 덮어쓰지 않는다.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
