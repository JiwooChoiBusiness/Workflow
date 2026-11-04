"""함수 허용 목록(3단계 설계 10.4의 6번). 집계·문자열·날짜·수학·창 함수만 받는다."""

from collections.abc import Callable

import pytest

from app.knowledge.sql.sources import SQLITE_FUNCTIONS, SourceView
from app.knowledge.sql.validate import SqlRejected, validate_sql

DUCKDB_OK = [
    "count(*)",
    "count(DISTINCT region)",
    "sum(amount)",
    "avg(amount)",
    "min(sold_on)",
    "max(amount)",
    "median(amount)",
    "string_agg(region, ',')",
    "list(region)",
    "lower(region)",
    "upper(region)",
    "trim(region)",
    "substr(region, 1, 2)",
    "replace(region, '서', '동')",
    "concat(region, '시')",
    "region || '시'",
    "length(region)",
    "regexp_matches(region, '서')",
    "abs(amount)",
    "round(amount, 1)",
    "ceil(amount)",
    "floor(amount)",
    "power(amount, 2)",
    "sqrt(amount)",
    "amount % 2",
    "greatest(amount, 1)",
    "date_trunc('month', sold_on)",
    "date_part('year', sold_on)",
    "extract(year FROM sold_on)",
    "strftime(sold_on, '%Y')",
    "year(sold_on)",
    "current_date",
    "date_diff('day', sold_on, current_date)",
    "sold_on + INTERVAL 1 DAY",
    "CAST(amount AS INTEGER)",
    "amount::INTEGER",
    "TRY_CAST(region AS DATE)",
    "coalesce(region, '-')",
    "nullif(amount, 0)",
    "if(amount > 1, 1, 0)",
    "count_if(amount > 1)",
    "arg_max(region, amount)",
    "quantile_cont(amount, 0.9)",
    "stddev(amount)",
]


@pytest.mark.parametrize("expression", DUCKDB_OK)
def test_duckdb_functions_that_are_allowed(
    expression: str, plain_view: Callable[[str], SourceView]
) -> None:
    sql = f"SELECT {expression} AS v FROM sales"  # noqa: S608
    validate_sql(sql, plain_view("duckdb"), max_rows=10)


@pytest.mark.parametrize(
    "expression",
    [
        "getenv('PATH')",
        "current_setting('allowed_paths')",
        "read_text('C:/x')",
        "COLUMNS('c.*')",
        "query('SELECT 1')",
        "duckdb_settings()",
        "setseed(0.5)",
        "uuid()",
    ],
)
def test_duckdb_functions_that_are_refused(
    expression: str, plain_view: Callable[[str], SourceView]
) -> None:
    sql = f"SELECT {expression} FROM sales"  # noqa: S608
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, plain_view("duckdb"), max_rows=10)

    assert caught.value.code == "function_not_allowed"


@pytest.mark.parametrize(
    "expression",
    [
        "total(amount)",
        "group_concat(region, ',')",
        "printf('%.1f', amount)",
        "julianday(sold_on)",
        "datetime(sold_on)",
        "date(sold_on, '+1 day')",
        "strftime('%Y', sold_on)",
        "json_extract('{\"a\": 1}', '$.a')",
        "ifnull(region, '-')",
        "instr(region, '서')",
        "typeof(amount)",
        "min(amount, 1)",
    ],
)
def test_sqlite_functions_that_are_allowed(
    expression: str, plain_view: Callable[[str], SourceView]
) -> None:
    sql = f"SELECT {expression} AS v FROM sales"  # noqa: S608
    validate_sql(sql, plain_view("sqlite"), max_rows=10)


@pytest.mark.parametrize(
    "expression",
    [
        "load_extension('x')",
        "readfile('C:/x')",
        "writefile('C:/x', 'y')",
        "fts3_tokenizer('simple')",
        "zeroblob(1000000000)",
        "randomblob(1000000000)",
        "sqlite_version()",
    ],
)
def test_sqlite_functions_that_are_refused(
    expression: str, plain_view: Callable[[str], SourceView]
) -> None:
    sql = f"SELECT {expression} FROM sales"  # noqa: S608
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, plain_view("sqlite"), max_rows=10)

    assert caught.value.code == "function_not_allowed"


def test_the_sqlite_authorizer_list_leaves_out_dangerous_functions() -> None:
    # 같은 목록이 샌드박스 authorizer의 SQLITE_FUNCTION 허용 목록이다(runner.py).
    assert {"load_extension", "readfile", "writefile", "fts3_tokenizer"}.isdisjoint(
        SQLITE_FUNCTIONS
    )
    assert {"like", "glob", "count", "strftime", "json_each"} <= SQLITE_FUNCTIONS
