from fastapi import FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="platform-api")
    app.include_router(health.router)
    return app


app = create_app()
