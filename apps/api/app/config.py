from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# apps/api/app/config.py에서 네 단계 위. parents[3] 대신 parent를 이어 쓰면 경로가 얕아도
# IndexError가 나지 않는다. 패키지를 편집 모드로 설치하므로 이 파일은 항상 저장소 안에 있다.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # KEY= 처럼 비워 둔 값은 설정하지 않은 것으로 본다.
        env_ignore_empty=True,
        # 검증 오류 메시지에 .env 값(API 키 등)이 그대로 찍히지 않게 한다.
        hide_input_in_errors=True,
    )

    encryption_key: SecretStr | None = None

    server_port: int = Field(default=8500, ge=1, le=65535)
    log_level: str = "INFO"
    data_dir: Path = REPO_ROOT / "data"
    web_dist_dir: Path = REPO_ROOT / "apps" / "web" / "dist"
    dev_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    runner_max_concurrent_runs: int = Field(default=1, ge=1)
    shutdown_grace_sec: float = Field(default=20, ge=0)
    waiting_input_ttl_hours: int = Field(default=24, ge=1)
    run_event_retention_days: int = Field(default=90, ge=1)
    memory_budget_mb: int = Field(default=300, ge=1)

    sandbox_backend: Literal["local", "e2b"] = "local"
    sandbox_shell: Literal["powershell", "pwsh", "bash"] = "powershell"
    sandbox_shell_mode: Literal["file", "encoded"] = "file"
    sandbox_python: Path | None = None
    sandbox_memory_limit_mb: int = Field(default=1024, ge=1)
    sandbox_max_processes: int = Field(default=64, ge=1)
    sandbox_tool_timeout_sec: int = Field(default=120, ge=1)
    sandbox_tool_max_timeout_sec: int = Field(default=900, ge=1)
    workspace_max_bytes: int = Field(default=2 * 1024**3, ge=0)
    snapshot_max_bytes: int = Field(default=1024**3, ge=0)
    approval_mode_default: Literal["off", "http_writes", "all_shell"] = "all_shell"
    http_allowlist_file: Path | None = None
    http_max_body_bytes: int = Field(default=5 * 1024**2, ge=0)
    keep_run_scripts: bool = False

    gemini_api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    gemini_model_fallback: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    )
    gemini_openai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_key_cooldown_sec: int = Field(default=60, ge=0)
    llm_fake: bool = False
    llm_fake_script: Path | None = None

    e2b_api_key: SecretStr | None = None
    e2b_template: str | None = None

    @field_validator("gemini_api_keys", "gemini_model_fallback", "dev_origins", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "data_dir", "web_dist_dir", "http_allowlist_file", "llm_fake_script", mode="after"
    )
    @classmethod
    def _anchor_to_repo_root(cls, value: Path | None) -> Path | None:
        # 상대 경로는 실행 위치가 아니라 저장소 루트 기준이다. uv run은 하위 폴더에서도 불린다.
        if value is None or value.is_absolute():
            return value
        return REPO_ROOT / value

    @property
    def allowlist_file(self) -> Path:
        return self.http_allowlist_file or self.data_dir / "allowlist.txt"

    def secret_values(self) -> list[str]:
        # 로그 마스킹이 형식과 관계없이 그대로 지울 값들.
        secrets = [*self.gemini_api_keys, self.encryption_key, self.e2b_api_key]
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
