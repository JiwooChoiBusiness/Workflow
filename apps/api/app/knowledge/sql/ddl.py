"""sql_schema 노드와 describe_sql_source 도구가 모델에 주는 DDL 글(3단계 설계 10.2, 10.7).

표마다 `CREATE TABLE <이름> (<열> <타입>, -- <설명> (예: 샘플값) …); -- <표 설명>`을 쓰고 끝에
용어집을 붙인다. 열 이름·설명·샘플값은 파일과 사용자가 쓴 값이라 개행·제어 문자를 접어 한 줄로
만든다(14.1). 그래서 `--` 주석 밖으로 나와 다른 지시처럼 보이지 않는다. 숨긴 열과 숨긴 표는
SourceView에 이미 없다. 토큰 상한을 넘으면 표 목록만 주고 스키마 지식 베이스를 쓰라고 알린다.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.knowledge.sql.sources import SourceView, TableView
from app.knowledge.tokens import estimate_tokens
from app.schemas.citations import one_line

_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 따옴표 없이 쓰면 문법으로 읽히는 낱말. 이런 이름은 따옴표로 감싸 보여 준다.
_RESERVED = frozenset(
    {
        "all", "and", "any", "as", "asc", "between", "by", "case", "cast", "check", "column",
        "create", "cross", "default", "delete", "desc", "distinct", "drop", "else", "end",
        "except", "exists", "false", "from", "full", "group", "having", "in", "inner",
        "insert", "intersect", "into", "is", "join", "key", "left", "like", "limit", "natural",
        "not", "null", "offset", "on", "or", "order", "outer", "over", "partition", "primary",
        "range", "right", "rows", "select", "table", "then", "to", "true", "union", "update",
        "user", "using", "values", "when", "where", "window", "with",
    }
)  # fmt: skip
DIALECT_LABELS = {"duckdb": "DuckDB", "sqlite": "SQLite"}
SAMPLE_CHARS = 40
DESCRIPTION_CHARS = 300


def quote_identifier(name: str) -> str:
    folded = one_line(name) or name
    if _SIMPLE.match(folded) and folded.lower() not in _RESERVED:
        return folded
    return '"' + folded.replace('"', '""') + '"'


def _sample(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        text = one_line(value)
        if len(text) > SAMPLE_CHARS:
            text = text[:SAMPLE_CHARS] + "…"
        return "'" + text.replace("'", "''") + "'"
    return one_line(json.dumps(value, ensure_ascii=False, default=str))[:SAMPLE_CHARS]


def _note(text: str) -> str:
    folded = one_line(text)
    return folded[:DESCRIPTION_CHARS] + "…" if len(folded) > DESCRIPTION_CHARS else folded


def table_block(table: TableView, *, include_samples: bool) -> str:
    lines = [
        f"CREATE {'VIEW' if table.kind == 'view' else 'TABLE'} {quote_identifier(table.name)} ("
    ]
    count = len(table.columns)
    for index, column in enumerate(table.columns):
        line = f"  {quote_identifier(column.name)} {one_line(column.type) or 'TEXT'}"
        if index < count - 1:
            line += ","
        notes: list[str] = []
        if column.description:
            notes.append(_note(column.description))
        if include_samples and column.samples:
            notes.append("예: " + ", ".join(_sample(value) for value in column.samples))
        if notes:
            line += " -- " + "; ".join(notes)
        lines.append(line)
    tail = ");"
    about: list[str] = []
    if table.description:
        about.append(_note(table.description))
    if table.row_count is not None:
        about.append(f"{table.row_count}행")
    if about:
        tail += " -- " + ", ".join(about)
    lines.append(tail)
    return "\n".join(lines)


def table_summary(table: TableView) -> dict[str, Any]:
    """sql_schema 노드 출력 tables의 항목. 보이는 열만 담는다."""
    return {
        "name": table.name,
        "kind": table.kind,
        "description": table.description,
        "row_count": table.row_count,
        "columns": [
            {"name": column.name, "type": column.type, "description": column.description}
            for column in table.columns
        ],
    }


def _glossary_block(glossary: str) -> str:
    lines = [one_line(line) for line in glossary.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return "\n".join(["-- 용어집", *(f"-- {line}" for line in lines)])


def _head(name: str, dialect: str) -> str:
    label = DIALECT_LABELS.get(dialect, dialect)
    return f"-- 데이터 소스: {one_line(name)} ({label}). SQL은 {label} 문법의 SELECT 하나로 쓴다."


@dataclass(frozen=True)
class SchemaText:
    ddl: str
    tables: list[dict[str, Any]]
    glossary: str
    truncated: bool
    tokens: int
    # 고른 표 중 이 데이터 소스에 없는(지웠거나 숨긴) 이름.
    missing: list[str] = field(default_factory=list[str])


def schema_text(
    view: SourceView,
    *,
    name: str,
    glossary: str,
    tables: Sequence[str] = (),
    include_samples: bool = True,
    max_tokens: int = 8000,
) -> SchemaText:
    wanted = {item.lower() for item in tables}
    chosen = [table for table in view.tables if not wanted or table.name.lower() in wanted]
    missing = [item for item in tables if view.table(item) is None]
    head = _head(name, view.dialect)
    glossary_text = _glossary_block(glossary)
    blocks = [table_block(table, include_samples=include_samples) for table in chosen]
    full = "\n\n".join(part for part in [head, *blocks, glossary_text] if part)
    summaries = [table_summary(table) for table in chosen]
    tokens = estimate_tokens(full)
    if tokens <= max_tokens:
        return SchemaText(full, summaries, glossary, False, tokens, missing)
    lines = [
        head,
        "-- 스키마가 커서 표 목록만 싣는다. 필요한 표의 열은 스키마를 내보낸 지식 베이스에서 "
        "찾는다.",
    ]
    for table in chosen:
        about = [f"{len(table.columns)}열"]
        if table.row_count is not None:
            about.append(f"{table.row_count}행")
        line = f"-- {quote_identifier(table.name)}: " + ", ".join(about)
        if table.description:
            line += f" — {_note(table.description)}"
        lines.append(line)
    short = "\n".join(lines)
    if glossary_text:
        short += "\n\n" + glossary_text
    return SchemaText(short, summaries, glossary, True, estimate_tokens(short), missing)


def table_document(view: SourceView, table: TableView, *, source_name: str) -> str:
    """스키마를 지식 베이스로 내보낼 때 표 하나의 문서 글(10.7)."""
    return "\n\n".join([_head(source_name, view.dialect), table_block(table, include_samples=True)])
