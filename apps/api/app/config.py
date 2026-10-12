from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 저장소 루트의 .env. 컨테이너에는 이 파일이 없고 compose가 환경변수로 넣는다.
# parents[3] 대신 parent를 이어 쓰면 경로가 얕아도 IndexError가 나지 않는다.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        # KEY= 처럼 비워 둔 값은 설정하지 않은 것으로 본다.
        env_ignore_empty=True,
        # 검증 오류 메시지에 .env 값(API 키 등)이 그대로 찍히지 않게 한다.
        hide_input_in_errors=True,
    )

    log_level: str = "INFO"

    database_url: SecretStr | None = None
    redis_url: str = "redis://redis:6379/0"

    encryption_key: SecretStr | None = None
    internal_api_token: SecretStr | None = None
    session_secret: SecretStr | None = None

    sandbox_max_concurrent: int = 3
    runner_max_concurrent_runs: int = 3
    sandbox_idle_timeout_sec: int = 1800
    sandbox_runtime: str = ""

    gemini_api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    gemini_model_fallback: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    )
    gemini_openai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    anthropic_api_key: SecretStr | None = None

    @field_validator("gemini_api_keys", "gemini_model_fallback", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
