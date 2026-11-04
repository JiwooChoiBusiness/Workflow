"""SELECT 하나만(3단계 설계 10.4의 1~4번). 주석과 끝 세미콜론, 트리 안의 DML·DDL·명령."""

from collections.abc import Callable

import pytest

from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import SqlRejected, extract_sql, validate_sql


def rejected(sql: str, view: SourceView) -> SqlRejected:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, view, max_rows=10)
    return caught.value


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", "sql_empty"),
        ("  ;  ", "sql_empty"),
        ("-- 주석뿐", "sql_empty"),
        ("SELECT region FROM sales WHERE (", "sql_invalid"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("UPDATE sales SET amount = 0", "not_select"),
        ("DELETE FROM sales", "not_select"),
        ("CREATE TABLE t AS SELECT 1", "not_select"),
        ("SET memory_limit = '4GB'", "not_select"),
        ("INSTALL httpfs", "not_select"),
        ("LOAD httpfs", "not_select"),
        ("DESCRIBE sales", "not_select"),
        ("SELECT * FROM (DESCRIBE sales)", "not_select"),
        ("SELECT region INTO copy FROM sales", "not_select"),
        ("EXPLAIN SELECT 1", "not_select"),
    ],
)
def test_only_one_select(sql: str, code: str, view: Callable[[str], SourceView]) -> None:
    assert rejected(sql, view("duckdb")).code == code


def test_trailing_comments_and_semicolons_are_not_statements(
    plain_view: Callable[[str], SourceView],
) -> None:
    validated = validate_sql(
        "SELECT region -- 지역\nFROM sales /* 판매 */ ;;  -- 끝", plain_view("duckdb"), max_rows=5
    )

    # 주석은 다시 쓰지 않는다. 줄 주석에 */ 를 넣어 블록 주석을 닫는 장난이 통하지 않는다.
    assert validated.sql == "SELECT region FROM sales LIMIT 6"
    sneaky = validate_sql("SELECT 1 -- */ ; ATTACH 'x' AS y", plain_view("duckdb"), max_rows=5)
    assert sneaky.sql == "SELECT 1 LIMIT 6"


def test_parenthesised_select_is_unwrapped(plain_view: Callable[[str], SourceView]) -> None:
    validated = validate_sql("(SELECT region FROM sales)", plain_view("duckdb"), max_rows=5)

    assert validated.sql == "SELECT region FROM sales LIMIT 6"


def test_message_names_the_statement(view: Callable[[str], SourceView]) -> None:
    error = rejected("WITH x AS (DELETE FROM sales RETURNING id) SELECT * FROM x", view("duckdb"))

    assert error.code == "not_select"
    assert error.message == "SELECT 문만 실행한다(쿼리 안의 DELETE도 쓸 수 없다)"
    assert error.subject == "Delete"


def test_sql_is_taken_from_the_first_sql_fence() -> None:
    answer = "다음 SQL을 쓰면 된다.\n```SQL\nSELECT 1\n```\n그리고\n```sql\nSELECT 2\n```"

    assert extract_sql(answer) == "SELECT 1"
    assert extract_sql("```\nSELECT 3\n```") == "SELECT 3"
    assert extract_sql("```sql\nSELECT 4") == "SELECT 4"
    assert extract_sql("  SELECT 5  ") == "SELECT 5"


def test_too_long_sql_is_rejected(plain_view: Callable[[str], SourceView]) -> None:
    sql = "SELECT " + ", ".join(["region"] * 6000) + " FROM sales"  # noqa: S608

    assert rejected(sql, plain_view("duckdb")).code == "sql_too_long"
