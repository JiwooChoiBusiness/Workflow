"""표 제한과 경로 스캔(3단계 설계 10.4의 5번). 소스 밖의 표, 파일 경로, 파일을 읽는 표 함수."""

from collections.abc import Callable

import pytest

from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import SqlRejected, validate_sql


@pytest.mark.parametrize(
    ("dialect", "sql", "code"),
    [
        ("duckdb", "SELECT * FROM 'C:/wf-data/storage/sql-sources/a.csv'", "table_not_allowed"),
        ("duckdb", 'SELECT * FROM "C:/wf-data/app.db"', "table_not_allowed"),
        ("duckdb", "SELECT * FROM read_csv('C:/x.csv')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM read_csv_auto('C:/x.csv')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM read_parquet('C:/x.parquet')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM read_json('C:/x.json')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM read_text('C:/x.txt')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM glob('C:/*')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM query_table('sales')", "function_not_allowed"),
        ("duckdb", "SELECT * FROM 'https://example.com/x.csv'", "table_not_allowed"),
        ("duckdb", "SELECT * FROM other.main.sales", "table_not_allowed"),
        ("duckdb", "SELECT * FROM information_schema.tables", "table_not_allowed"),
        ("duckdb", "SELECT * FROM duckdb_tables()", "function_not_allowed"),
        ("duckdb", "SELECT * FROM orders", "table_not_allowed"),
        ("sqlite", "SELECT * FROM sqlite_schema", "table_not_allowed"),
        ("sqlite", "SELECT * FROM temp.sales", "table_not_allowed"),
        ("sqlite", "SELECT * FROM pragma_module_list", "table_not_allowed"),
        ("sqlite", "SELECT * FROM pragma_table_info('sales')", "function_not_allowed"),
    ],
)
def test_outside_tables_and_file_scans_are_refused(
    dialect: str, sql: str, code: str, plain_view: Callable[[str], SourceView]
) -> None:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, plain_view(dialect), max_rows=10)

    assert caught.value.code == code


def test_table_names_ignore_case_and_ctes_are_local(
    plain_view: Callable[[str], SourceView],
) -> None:
    validated = validate_sql(
        "WITH totals AS (SELECT region, SUM(amount) AS s FROM SALES GROUP BY region) "
        "SELECT * FROM totals JOIN Customers ON TRUE",
        plain_view("duckdb"),
        max_rows=10,
    )

    assert sorted(validated.tables) == ["customers", "sales"]
    assert validated.ctes == ("totals",)


def test_the_message_lists_the_tables_that_exist(view: Callable[[str], SourceView]) -> None:
    with pytest.raises(SqlRejected) as caught:
        validate_sql("SELECT * FROM orders", view("duckdb"), max_rows=10)

    # 숨긴 표(secret)는 목록에 없다.
    assert (
        caught.value.message
        == "표 orders은(는) 이 데이터 소스에 없다. 쓸 수 있는 표: sales, customers"
    )
    assert caught.value.subject == "orders"
