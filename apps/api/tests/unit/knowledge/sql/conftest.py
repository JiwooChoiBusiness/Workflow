"""Text2SQL 라이브러리 시험 공용 픽스처. 표는 sales·customers와 숨긴 표 secret이다.

--import-mode=importlib에서는 시험 모듈이 conftest를 import할 수 없어 도우미도 픽스처로 넘긴다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.knowledge.sql.sources import SourceView, source_view
from app.schemas.sql_source import (
    ColumnNote,
    SqlAnnotations,
    SqlColumn,
    SqlSchemaSnapshot,
    SqlTable,
    TableNote,
)


def _table(name: str, columns: list[tuple[str, str]], **extra: Any) -> SqlTable:
    return SqlTable(name=name, columns=[SqlColumn(name=n, type=t) for n, t in columns], **extra)


def snapshot() -> SqlSchemaSnapshot:
    return SqlSchemaSnapshot(
        tables=[
            _table(
                "sales",
                [
                    ("id", "BIGINT"),
                    ("region", "VARCHAR"),
                    ("amount", "DOUBLE"),
                    ("card", "VARCHAR"),
                    ("sold_on", "DATE"),
                ],
                row_count=500,
            ),
            _table("customers", [("id", "BIGINT"), ("name", "VARCHAR"), ("email", "VARCHAR")]),
            _table("secret", [("x", "INTEGER")]),
        ],
        captured_at=datetime(2026, 11, 3, tzinfo=UTC),
        source_hash="h",
    )


def annotations() -> SqlAnnotations:
    return SqlAnnotations(
        tables={
            "sales": TableNote(columns={"card": ColumnNote(hidden=True)}),
            "customers": TableNote(columns={"email": ColumnNote(hidden=True)}),
            "secret": TableNote(hidden=True),
        }
    )


@pytest.fixture
def view() -> Callable[[str], SourceView]:
    """방언 이름 → 숨김이 걸린 SourceView."""

    def make(dialect: str) -> SourceView:
        kind = "sqlite" if dialect == "sqlite" else "files"
        return source_view(kind, snapshot(), annotations())

    return make


@pytest.fixture
def plain_view() -> Callable[[str], SourceView]:
    """숨김이 없는 SourceView. * 를 풀어 쓰지 않는 경우를 본다."""

    def make(dialect: str) -> SourceView:
        kind = "sqlite" if dialect == "sqlite" else "files"
        return source_view(kind, snapshot(), SqlAnnotations())

    return make
