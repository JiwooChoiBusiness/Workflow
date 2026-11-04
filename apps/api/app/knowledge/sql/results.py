"""질의 결과 처리(3단계 설계 10.5, 10.6).

샌드박스 스크립트가 값을 JSON으로 바꿔(날짜 → ISO, Decimal → 글, bytes → 길이 표시) 행 목록으로
남긴다. 여기서는 행을 열 이름 → 값의 객체로 바꿔 변수 상한(1MB) 안에서 자르고, 모델에 보내는 글
(result_text)을 만든다. result_text는 앞 llm_rows행의 마크다운 표이고 llm_bytes를 넘으면 열이 아니라
행을 줄인다. 표는 데이터 표식(<document>)으로 감싸 표 안의 글이 지시로 읽히지 않게 한다(14.1).
"""

import json
from collections.abc import Sequence
from typing import Any

from app.schemas.citations import document_block, one_line

ROWS_MAX_BYTES = 1024 * 1024
CELL_CHARS = 200


def unique_columns(columns: Sequence[str]) -> list[str]:
    """같은 이름이 둘 이상이면(조인한 두 표의 id 등) 뒤의 것에 _2, _3을 붙인다."""
    taken: set[str] = set()
    out: list[str] = []
    for name in columns:
        base = name or "column"
        candidate, number = base, 2
        while candidate in taken:
            candidate = f"{base}_{number}"
            number += 1
        taken.add(candidate)
        out.append(candidate)
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def row_objects(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], *, max_bytes: int = ROWS_MAX_BYTES
) -> tuple[list[dict[str, Any]], bool]:
    """(객체 행, 상한에 걸려 잘렸는지)."""
    objects: list[dict[str, Any]] = []
    total = 2
    for row in rows:
        item = dict(zip(columns, row, strict=False))
        total += _size(item) + 1
        if total > max_bytes:
            return objects, True
        objects.append(item)
    return objects, False


def cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = one_line(text)
    if len(text) > CELL_CHARS:
        text = text[:CELL_CHARS] + "…"
    return text.replace("|", "\\|")


def _line(values: Sequence[str]) -> str:
    return "| " + " | ".join(values) + " |"


def markdown_table(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], *, max_rows: int, max_bytes: int
) -> tuple[str, int]:
    """(표 글, 실은 행 수). 머리글은 늘 싣고 행은 상한 안에서만 싣는다."""
    header = [_line([cell(name) for name in columns]), _line(["---"] * len(columns))]
    text = "\n".join(header)
    used = len(text.encode("utf-8"))
    shown = 0
    for row in rows[:max_rows]:
        line = _line([cell(value) for value in row])
        size = len(line.encode("utf-8")) + 1
        if used + size > max_bytes:
            break
        text += "\n" + line
        used += size
        shown += 1
    return text, shown


def summary_line(*, row_count: int, shown: int, truncated: bool, max_rows: int) -> str:
    if row_count == 0:
        return "결과 행이 없다."
    if truncated:
        return f"결과가 {max_rows}행을 넘어 잘렸다. 처음 {shown}행만 싣는다."
    if shown < row_count:
        return f"{row_count}행 중 처음 {shown}행만 싣는다."
    return f"{row_count}행."


def result_text(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    row_count: int,
    truncated: bool,
    max_rows: int,
    llm_rows: int,
    llm_bytes: int,
    source_name: str,
) -> str:
    summary = summary_line(row_count=row_count, shown=0, truncated=truncated, max_rows=max_rows)
    if not columns:
        return summary
    # 표식과 요약 줄 몫을 남긴다.
    budget = max(llm_bytes - 512, 256)
    table, shown = markdown_table(columns, rows, max_rows=llm_rows, max_bytes=budget)
    summary = summary_line(row_count=row_count, shown=shown, truncated=truncated, max_rows=max_rows)
    block = document_block(table, index=None, name="질의 결과", source=source_name)
    return f"{block}\n{summary}"
