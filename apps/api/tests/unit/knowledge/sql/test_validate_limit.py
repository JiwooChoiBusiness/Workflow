"""LIMIT 주입(3단계 설계 10.4의 8번). 없으면 max_rows + 1, 있으면 min(기존, max_rows + 1)."""

from collections.abc import Callable

import pytest

from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import SqlRejected, validate_sql


@pytest.mark.parametrize(
    ("sql", "expected", "limit"),
    [
        ("SELECT region FROM sales", "SELECT region FROM sales LIMIT 51", 51),
        ("SELECT region FROM sales LIMIT 10", "SELECT region FROM sales LIMIT 10", 10),
        ("SELECT region FROM sales LIMIT 999", "SELECT region FROM sales LIMIT 51", 51),
        ("SELECT region FROM sales LIMIT 0", "SELECT region FROM sales LIMIT 0", 0),
        (
            "SELECT region FROM sales LIMIT 5 OFFSET 20",
            "SELECT region FROM sales LIMIT 5 OFFSET 20",
            5,
        ),
        (
            "SELECT region FROM sales FETCH FIRST 70 ROWS ONLY",
            "SELECT region FROM sales LIMIT 51",
            51,
        ),
        (
            "SELECT region FROM sales UNION ALL SELECT name FROM customers",
            "SELECT region FROM sales UNION ALL SELECT name FROM customers LIMIT 51",
            51,
        ),
        (
            "SELECT region FROM (SELECT region FROM sales LIMIT 3000) AS s",
            "SELECT region FROM (SELECT region FROM sales LIMIT 3000) AS s LIMIT 51",
            51,
        ),
    ],
)
def test_limit_is_injected_or_lowered(
    sql: str, expected: str, limit: int, plain_view: Callable[[str], SourceView]
) -> None:
    validated = validate_sql(sql, plain_view("duckdb"), max_rows=50)

    assert validated.sql == expected
    assert validated.limit == limit


def test_sqlite_offset_comma_form(plain_view: Callable[[str], SourceView]) -> None:
    validated = validate_sql(
        "SELECT region FROM sales LIMIT 5, 500", plain_view("sqlite"), max_rows=50
    )

    assert validated.sql == "SELECT region FROM sales LIMIT 51 OFFSET 5"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM sales LIMIT 1 + 1",
        "SELECT region FROM sales LIMIT (SELECT 3)",
        "SELECT region FROM sales FETCH FIRST 10 PERCENT ROWS ONLY",
    ],
)
def test_limits_that_are_not_plain_numbers_are_refused(
    sql: str, plain_view: Callable[[str], SourceView]
) -> None:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, plain_view("duckdb"), max_rows=50)

    assert caught.value.code == "limit_invalid"
