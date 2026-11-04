"""검토 보강(M27): CTE는 범위마다 풀고 카탈로그·표 이름과 겹치면 거부, SQLite 숨긴 열 표의 엄격한
풀이(USING→ON, 이름 없는 식의 별칭), 괄호 밖 절, sqlglot 내부 예외, 숨긴 것을 읽는 뷰 찾기.
"""

from collections.abc import Callable

import pytest

from app.knowledge.sql import validate as validate_module
from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import (
    SqlRejected,
    exposed_views,
    is_view_definition,
    validate_sql,
)


def shadowing(name: str) -> str:
    return "WITH <n> AS (SELECT 1 AS a) SELECT a FROM <n>".replace("<n>", name)


def rejected(sql: str, view: SourceView) -> SqlRejected:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(sql, view, max_rows=10)
    return caught.value


@pytest.mark.parametrize(
    "name",
    ["sqlite_schema", "pragma_table_info", "duckdb_tables", "pg_class", "information_schema"],
)
@pytest.mark.parametrize("dialect", ["duckdb", "sqlite"])
def test_cte_names_cannot_take_catalog_names(
    name: str, dialect: str, view: Callable[[str], SourceView]
) -> None:
    caught = rejected(shadowing(name), view(dialect))

    assert (caught.code, caught.subject) == ("cte_name_conflict", name)


def test_cte_names_cannot_shadow_hidden_or_visible_tables(
    view: Callable[[str], SourceView],
) -> None:
    for name in ("secret", "SALES", "customers"):
        caught = rejected(shadowing(name), view("sqlite"))
        assert caught.code == "cte_name_conflict", name


def test_a_cte_only_covers_its_own_scope(view: Callable[[str], SourceView]) -> None:
    # 안쪽 서브쿼리의 CTE foo는 바깥 FROM foo를 가리지 않는다. 바깥 foo는 실제 표라 없는 표다.
    caught = rejected(
        "SELECT * FROM foo WHERE 1 IN (WITH foo AS (SELECT 1 AS a) SELECT a FROM foo)",
        view("sqlite"),
    )
    validated = validate_sql(
        "WITH t AS (SELECT id FROM sales) SELECT COUNT(*) AS n FROM t", view("sqlite"), max_rows=10
    )

    assert (caught.code, caught.subject) == ("table_not_allowed", "foo")
    assert validated.ctes == ("t",) and validated.tables == ("sales",)


def test_sqlite_hidden_tables_are_queried_as_qualified_trees(
    view: Callable[[str], SourceView],
) -> None:
    joined = validate_sql(
        "SELECT name FROM customers JOIN sales USING (id)", view("sqlite"), max_rows=10
    )
    named = validate_sql("SELECT upper(name), 1 FROM customers", view("sqlite"), max_rows=10)
    natural = rejected("SELECT name FROM customers NATURAL JOIN sales", view("sqlite"))

    # USING 조건은 authorizer를 거치지 않으므로 ON으로 바꾼 트리를 실행한다.
    assert "USING" not in joined.sql and "ON customers.id = sales.id" in joined.sql
    # 이름 없는 식은 _col_N이 아니라 식 글을 별칭으로 둔다.
    assert '"UPPER(name)"' in named.sql and "_col_" not in named.sql
    assert natural.code == "column_unresolved"


def test_duckdb_keeps_using_because_the_query_file_has_no_hidden_columns(
    view: Callable[[str], SourceView],
) -> None:
    validated = validate_sql(
        "SELECT name FROM customers JOIN sales USING (id)", view("duckdb"), max_rows=10
    )

    assert validated.sql == "SELECT name FROM customers JOIN sales USING (id) LIMIT 11"


@pytest.mark.parametrize("dialect", ["duckdb", "sqlite"])
def test_outer_clauses_of_a_parenthesized_query_move_inside(
    dialect: str, plain_view: Callable[[str], SourceView]
) -> None:
    ordered = validate_sql(
        "(SELECT region FROM sales) ORDER BY region", plain_view(dialect), max_rows=10
    )
    limited = validate_sql("(SELECT region FROM sales) LIMIT 5", plain_view(dialect), max_rows=10)
    clash = rejected("(SELECT region FROM sales LIMIT 3) LIMIT 5", plain_view(dialect))

    assert ordered.sql == "SELECT region FROM sales ORDER BY region LIMIT 11"
    assert limited.sql == "SELECT region FROM sales LIMIT 5" and limited.limit == 5
    assert clash.code == "sql_invalid"


def test_unexpected_sqlglot_errors_become_rejections(
    view: Callable[[str], SourceView], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise AssertionError("sqlglot 안의 단언")

    monkeypatch.setattr(validate_module, "_check_functions", broken)

    caught = rejected("SELECT region FROM sales", view("duckdb"))

    assert caught.code == "sql_invalid"
    assert caught.message == "SQL을 검사하지 못했다: sqlglot 안의 단언"


def test_views_that_read_hidden_things_are_found() -> None:
    views = {
        "v_ok": "CREATE VIEW v_ok AS SELECT id, name FROM customers",
        "v_star": "CREATE VIEW v_star AS SELECT * FROM customers",
        "v_secret": "CREATE VIEW v_secret AS SELECT code FROM secret",
        "v_over": "CREATE VIEW v_over AS SELECT * FROM v_secret",
        "v_alias": "CREATE VIEW v_alias AS SELECT email AS contact FROM customers",
        "v_using": "CREATE VIEW v_using AS SELECT name FROM customers JOIN g USING (email)",
        "v_cte": "CREATE VIEW v_cte AS WITH secret AS (SELECT 1 AS code) SELECT code FROM secret",
        "v_other": "CREATE VIEW v_other AS SELECT * FROM orders",
        "v_odd": "CREATE VIEW v_odd AS SELECT ??? email FROM customers",
    }

    found = exposed_views(views, hidden_tables={"secret"}, hidden_columns={"customers": {"email"}})

    # v_cte의 secret은 CTE라 숨긴 표가 아니다. 정의를 읽지 못한 v_odd는 숨긴 이름이 낱말로 있다.
    assert found == ["v_star", "v_secret", "v_over", "v_alias", "v_using", "v_odd"]


def test_only_single_create_view_statements_are_rebuilt() -> None:
    assert is_view_definition("CREATE VIEW v AS SELECT id FROM t")
    assert is_view_definition('CREATE VIEW "big orders" AS SELECT id FROM t WHERE x > 1')
    assert not is_view_definition("CREATE TABLE t (id INTEGER)")
    assert not is_view_definition("CREATE VIEW v AS SELECT 1; DROP TABLE t")
    assert not is_view_definition("ATTACH 'x.db' AS x")
    assert not is_view_definition("CREATE VIEW v AS SELECT ??? FROM")
