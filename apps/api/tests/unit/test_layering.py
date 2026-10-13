"""계층 규칙(설계 8.4, 13.2). import-linter를 두지 않아 직접 import를 AST로 확인한다.

루프·이벤트 버스는 HTTP 계층을 모르고, 루프는 DB도 모른다. 도구 코드는 Path·subprocess·httpx를 직접
쓰지 않고 SandboxBackend만 거친다.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def files_under(*parts: str) -> list[Path]:
    return sorted((APP.joinpath(*parts)).rglob("*.py"))


def forbidden(modules: set[str], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(
        module
        for module in modules
        if any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)
    )


@pytest.mark.parametrize("path", files_under("agent"), ids=lambda path: path.name)
def test_agent_does_not_know_http_or_db(path: Path) -> None:
    modules = imported_modules(path)

    assert (
        forbidden(
            modules, ("app.api", "app.auth", "app.db", "app.services", "app.runner", "fastapi")
        )
        == []
    )


@pytest.mark.parametrize("path", files_under("streaming"), ids=lambda path: path.name)
def test_streaming_does_not_know_http_layer(path: Path) -> None:
    assert forbidden(imported_modules(path), ("app.api", "app.auth")) == []


@pytest.mark.parametrize("path", files_under("agent", "tools"), ids=lambda path: path.name)
def test_tools_go_through_the_sandbox_backend(path: Path) -> None:
    modules = imported_modules(path)

    assert forbidden(modules, ("subprocess", "httpx", "asyncio.subprocess")) == []
    assert "pathlib.Path" not in modules


def test_the_rule_sees_real_imports() -> None:
    # 규칙이 아무것도 못 잡는 상태(경로 오타 등)가 아님을 확인한다.
    assert files_under("agent") and files_under("streaming")
    assert "app.db.engine" in imported_modules(APP / "streaming" / "event_bus.py")
