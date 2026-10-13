from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"


def load_settings(env_file: Path | None) -> Settings:
    # _env_file는 pydantic-settings가 실행 시에 받는 인자라 pyright가 보는 시그니처에는 없다.
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def test_env_example_parses() -> None:
    settings = load_settings(ENV_EXAMPLE)

    assert settings.encryption_key is None
    assert settings.server_port == 8500
    assert settings.data_dir == REPO_ROOT / "data"
    assert settings.web_dist_dir == REPO_ROOT / "apps" / "web" / "dist"
    assert settings.allowlist_file == REPO_ROOT / "data" / "allowlist.txt"
    assert settings.dev_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
    assert settings.runner_max_concurrent_runs == 1
    assert settings.shutdown_grace_sec == 20
    assert settings.memory_budget_mb == 300
    assert settings.sandbox_backend == "local"
    assert settings.sandbox_shell == "powershell"
    assert settings.sandbox_python is None
    assert settings.sandbox_memory_limit_mb == 1024
    assert settings.approval_mode_default == "all_shell"
    assert settings.keep_run_scripts is False
    assert settings.gemini_api_keys == []
    assert settings.gemini_model_fallback == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
    assert settings.llm_fake is False
    assert settings.llm_fake_script is None
    assert settings.e2b_api_key is None
    assert settings.secret_values() == []


def test_env_example_lists_every_setting() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert keys == {name.upper() for name in Settings.model_fields}


def test_comma_separated_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "key-a, key-b,,key-c")
    monkeypatch.setenv("DEV_ORIGINS", "http://localhost:5173")

    settings = load_settings(None)

    assert [k.get_secret_value() for k in settings.gemini_api_keys] == ["key-a", "key-b", "key-c"]
    assert settings.dev_origins == ["http://localhost:5173"]


def test_relative_paths_are_anchored_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", "var/data")
    monkeypatch.setenv("HTTP_ALLOWLIST_FILE", "var/allow.txt")
    monkeypatch.setenv("LLM_FAKE_SCRIPT", "tests/fake.json")

    settings = load_settings(None)

    assert settings.data_dir == REPO_ROOT / "var" / "data"
    assert settings.allowlist_file == REPO_ROOT / "var" / "allow.txt"
    assert settings.llm_fake_script == REPO_ROOT / "tests" / "fake.json"


def test_secret_values_collects_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("ENCRYPTION_KEY", "enc-value")
    monkeypatch.setenv("E2B_API_KEY", "e2b-value")

    settings = load_settings(None)

    assert settings.secret_values() == ["key-a", "key-b", "enc-value", "e2b-value"]


def test_absolute_data_dir_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    settings = load_settings(None)

    assert settings.data_dir == tmp_path
    assert settings.allowlist_file == tmp_path / "allowlist.txt"
