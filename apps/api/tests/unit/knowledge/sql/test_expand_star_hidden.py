"""숨긴 열(3단계 설계 10.4의 7번). 참조는 column_hidden이고 * ·t.* 는 보이는 열로 풀어 쓴다.

검증이 놓쳐도 실행 쪽이 막는다(DuckDB 질의 파일에는 숨긴 열이 없고, SQLite는 authorizer가 NULL로
읽는다). 여기서는 첫 관문만 본다.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing

import pytest

from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import SqlRejected, validate_sql


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        (
            "SELECT * FROM sales",
            "SELECT sales.id AS id, sales.region AS region, sales.amount AS amount, "
            "sales.sold_on AS sold_on FROM sales",
        ),
        ("SELECT s.* FROM sales AS s", "SELECT s.id AS id, s.region AS region"),
        (
            "SELECT * FROM (SELECT * FROM customers) AS c",
            "FROM (SELECT customers.id AS id, customers.name AS name FROM customers) AS c",
        ),
        (
            "WITH x AS (SELECT * FROM customers) SELECT * FROM x",
            "SELECT x.id AS id, x.name AS name",
        ),
        (
            "SELECT * FROM sales JOIN customers USING (id)",
            "sales.sold_on AS sold_on, customers.name AS name FROM sales JOIN customers",
        ),
        (
            "SELECT * EXCLUDE (amount) FROM sales",
            "sales.region AS region, sales.sold_on AS sold_on",
        ),
    ],
)
@pytest.mark.parametrize("dialect", ["duckdb", "sqlite"])
def test_stars_expand_to_visible_columns(
    sql: str, fragment: str, dialect: str, view: Callable[[str], SourceView]
) -> None:
    if dialect == "sqlite" and "EXCLUDE" in sql:
        pytest.skip("EXCLUDE는 DuckDB 문법이다")
    validated = validate_sql(sql, view(dialect), max_rows=10)

    assert fragment in validated.sql
    assert "card" not in validated.sql and "email" not in validated.sql


@pytest.mark.parametrize(
    ("sql", "subject"),
    [
        ("SELECT card FROM sales", "sales.card"),
        ("SELECT s.card AS c FROM sales AS s", "sales.card"),
        ("SELECT CARD FROM SALES", "sales.card"),
        ("SELECT region FROM sales WHERE card LIKE '4%'", "sales.card"),
        ("SELECT COUNT(DISTINCT card) FROM sales", "sales.card"),
        ("SELECT email FROM sales JOIN customers USING (id)", "customers.email"),
        ("SELECT q.card FROM (SELECT * FROM sales) AS q", "sales.card"),
        ("WITH x AS (SELECT card AS c FROM sales) SELECT c FROM x", "sales.card"),
        ("SELECT region FROM sales ORDER BY card", "sales.card"),
        ("SELECT (SELECT max(email) FROM customers) AS e FROM sales", "customers.email"),
    ],
)
def test_hidden_column_references_are_refused(
    sql: str, subject: str, view: Callable[[str], SourceView]
) -> None:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, view("duckdb"), max_rows=10)

    assert caught.value.code == "column_hidden"
    assert caught.value.subject == subject


def test_a_visible_column_with_a_hidden_name_elsewhere_is_fine(
    view: Callable[[str], SourceView],
) -> None:
    # customers.email이 숨겨져 있어도 다른 표의 같은 이름이 아닌 열은 막지 않는다.
    validated = validate_sql("SELECT c.name FROM customers AS c", view("duckdb"), max_rows=10)

    assert validated.sql == "SELECT c.name FROM customers AS c LIMIT 11"


def test_without_hidden_columns_the_sql_is_left_alone(
    plain_view: Callable[[str], SourceView],
) -> None:
    validated = validate_sql(
        "SELECT * FROM sales WHERE amount > 1", plain_view("duckdb"), max_rows=10
    )

    assert validated.sql == "SELECT * FROM sales WHERE amount > 1 LIMIT 11"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT * FROM sales) ORDER BY id",
        "SELECT * FROM (SELECT * FROM sales) "
        "JOIN (SELECT id AS cid, name FROM customers) ON id = cid",
        "SELECT id FROM sales WHERE id IN (SELECT id FROM (SELECT * FROM sales))",
    ],
)
def test_unaliased_derived_tables_get_a_name_before_expanding(
    sql: str, view: Callable[[str], SourceView]
) -> None:
    validated = validate_sql(sql, view("sqlite"), max_rows=10)

    # 별칭이 없으면 풀어 쓴 열의 한정자가 비어(.id) 실행에서 깨진다. 실제 SQLite로 돌려 본다.
    assert " ." not in validated.sql and "(." not in validated.sql
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(
            "CREATE TABLE sales(id, region, amount, card, sold_on);"
            "CREATE TABLE customers(id, name, email);"
            "INSERT INTO sales VALUES (1, '서울', 10, 'x', '2024-01-01');"
            "INSERT INTO customers VALUES (1, '김하나', 'k@example.com');"
        )
        cursor = conn.execute(validated.sql)
        names = [item[0] for item in cursor.description]
    assert "card" not in names and "email" not in names
