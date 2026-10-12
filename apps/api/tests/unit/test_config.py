from pathlib import Path

import pytest

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[4] / ".env.example"


def load_settings(env_file: Path | None) -> Settings:
    # _env_file는 pydantic-settings가 실행 시에 받는 인자라 pyright가 보는 시그니처에는 없다.
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def test_env_example_parses() -> None:
    settings = load_settings(ENV_EXAMPLE)

    assert settings.encryption_key is None
    assert settings.gemini_api_keys == []
    assert settings.gemini_model_fallback == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
    assert settings.sandbox_max_concurrent == 3
    assert settings.sandbox_runtime == ""


def test_gemini_api_keys_split_on_commas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "key-a, key-b,,key-c")

    settings = load_settings(None)

    assert [k.get_secret_value() for k in settings.gemini_api_keys] == ["key-a", "key-b", "key-c"]
