import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.agent_version import (
    MAX_ENV_VARS,
    TOOL_GROUPS,
    AgentVersionConfig,
    DraftUpdate,
)

STAMP = "2026-10-13T01:02:03.123456Z"


def draft(**fields: Any) -> DraftUpdate:
    return DraftUpdate.model_validate({"updated_at": STAMP, **fields})


def error_locs(fields: dict[str, Any]) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as info:
        draft(**fields)
    return [tuple(error["loc"]) for error in info.value.errors()]


def test_defaults() -> None:
    config = AgentVersionConfig()

    assert config.tools == list(TOOL_GROUPS)
    assert config.approval_mode == "all_shell"
    assert config.model.model_id is None
    assert config.model.params.max_output_tokens == 16000
    assert config.model.params.reasoning_effort is None
    assert config.limits.max_steps == 30
    assert (config.sandbox.memory_limit_mb, config.sandbox.max_processes) == (None, None)
    assert config.skill_version_ids == [] and config.file_ids == []


def test_full_draft_round_trips() -> None:
    skill = uuid.uuid4()
    body = draft(
        instructions="데이터를 정리한다",
        model={
            "model_id": "gemini-3.6-flash",
            "params": {
                "temperature": 0.2,
                "top_p": 0.9,
                "max_output_tokens": 8000,
                "reasoning_effort": "minimal",
            },
        },
        tools=["files", "shell"],
        limits={"max_steps": 10, "tool_timeout_sec": 300},
        sandbox={"memory_limit_mb": 512, "max_processes": 16},
        approval_mode="http_writes",
        env_vars=[{"name": "API_TOKEN", "value": "s3cr3t", "secret": True}, {"name": "MODE"}],
        skill_version_ids=[str(skill)],
        allow_unlisted_model=True,
    )

    assert body.updated_at.tzinfo is not None
    assert body.skill_version_ids == [skill]
    assert body.env_vars[1].value is None and body.env_vars[1].secret is False
    assert body.model.params.reasoning_effort == "minimal"


def test_updated_at_is_required_and_needs_a_timezone() -> None:
    with pytest.raises(ValidationError):
        DraftUpdate.model_validate({})
    with pytest.raises(ValidationError):
        DraftUpdate.model_validate({"updated_at": "2026-10-13T01:02:03"})


@pytest.mark.parametrize(
    ("fields", "loc"),
    [
        ({"approval_mode": "never"}, ("approval_mode",)),
        ({"tools": ["shell", "browser"]}, ("tools", 1)),
        ({"tools": ["shell", "shell"]}, ("tools",)),
        (
            {"model": {"params": {"reasoning_effort": "max"}}},
            ("model", "params", "reasoning_effort"),
        ),
        ({"model": {"params": {"temperature": 2.5}}}, ("model", "params", "temperature")),
        ({"model": {"params": {"top_p": 0}}}, ("model", "params", "top_p")),
        ({"model": {"params": {"max_output_tokens": 0}}}, ("model", "params", "max_output_tokens")),
        ({"model": {"model_id": "gemini 3"}}, ("model", "model_id")),
        ({"model": {"model_id": ""}}, ("model", "model_id")),
        ({"model": {"provider": "openai"}}, ("model", "provider")),
        ({"sandbox": {"memory_limit_mb": 0}}, ("sandbox", "memory_limit_mb")),
        ({"sandbox": {"cpu": 2}}, ("sandbox", "cpu")),
        ({"limits": {"max_steps": 201}}, ("limits", "max_steps")),
        ({"env_vars": [{"name": "1BAD", "value": "x"}]}, ("env_vars", 0, "name")),
        ({"env_vars": [{"name": "A-B", "value": "x"}]}, ("env_vars", 0, "name")),
        ({"env_vars": [{"name": "A", "value": "x" * 40000}]}, ("env_vars", 0, "value")),
        ({"skill_version_ids": ["not-a-uuid"]}, ("skill_version_ids", 0)),
        ({"instructions": "x" * 100_001}, ("instructions",)),
        ({"unknown": 1}, ("unknown",)),
    ],
)
def test_rejects(fields: dict[str, Any], loc: tuple[str | int, ...]) -> None:
    assert loc in error_locs(fields)


def test_duplicate_references_are_rejected() -> None:
    same = str(uuid.uuid4())

    assert ("skill_version_ids",) in error_locs({"skill_version_ids": [same, same]})
    assert ("file_ids",) in error_locs({"file_ids": [same, same]})


def test_env_var_names_are_unique_ignoring_case() -> None:
    locs = error_locs({"env_vars": [{"name": "Token", "value": "a"}, {"name": "TOKEN"}]})

    assert locs == [("env_vars",)]


def test_env_var_count_limit() -> None:
    many = [{"name": f"V{i}", "value": "x"} for i in range(MAX_ENV_VARS + 1)]

    assert ("env_vars",) in error_locs({"env_vars": many})


def test_model_id_accepts_provider_style_names() -> None:
    for model_id in ("gemini-3.8-flash", "models/gemini-2.5-pro", "tunedModels/abc_1:v2"):
        assert draft(model={"model_id": model_id}).model.model_id == model_id
