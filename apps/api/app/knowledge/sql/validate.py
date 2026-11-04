"""SQL 검증(3단계 설계 10.4). sqlglot으로 읽어 SELECT 하나만, 소스의 표와 허용 함수만 쓰게 하고
숨긴 열을 막은 뒤 LIMIT을 넣는다.

순서는 설계 그대로다. (1) 앞뒤 공백과 끝 세미콜론을 덜고 주석은 파서가 떼어 낸다(끝 주석만 남은
"문장"은 세지 않고, 만든 SQL에는 주석을 다시 쓰지 않는다) (2) 읽지 못하면 sql_invalid (3) 문장이
둘 이상이면 multiple_statements (4) 루트가 SELECT·집합 연산이 아니거나 트리 안에 DML·DDL·명령이
있으면 not_select (5) 소스 밖의 표는 table_not_allowed, 표 함수는 함수 허용 목록으로 본다 (6) 허용
목록 밖의 함수는 function_not_allowed (7) 숨긴 열은 column_hidden이고 * 는 보이는 열로 풀어 쓴다
(8) LIMIT은 max_rows + 1까지 (9) 결과 SQL을 돌려준다. 방언은 바꾸지 않는다.

표 참조는 sqlglot 범위(scope)마다 풀어, 그 범위에서 보이는 CTE만 CTE로 인정한다. CTE
이름이 소스의 표·뷰(숨긴 것 포함)나 엔진 카탈로그 이름과 겹치면 cte_name_conflict다. SQLite에서
숨긴 열이 있는 표를 읽으면 USING을 ON으로 바꾸고 모든 열을 표에 이은 트리를 실행하며, 그렇게 풀지
못하면 column_unresolved다. USING·NATURAL 조건은 SQLite authorizer를 거치지 않기
때문이다(17.3-8 실측).

sqlglot은 변환기라 이 검증을 지나도 실행이 안전하다는 뜻은 아니다. 읽기 전용과 숨긴 열은 실행
쪽(runner.py)이 엔진 설정과 숨김을 뺀 사본으로 다시 막는다. sqlglot은 import만으로 RSS가 14MB 늘어
처음 검증할 때 불러온다(13장).
"""

import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from functools import cache
from types import ModuleType
from typing import Any

from app.knowledge.sql.sources import (
    RESERVED_PREFIXES,
    TABLE_FUNCTIONS,
    TYPED_FUNCTIONS,
    TYPED_TABLE_FUNCTIONS,
    SourceView,
    TableView,
    anonymous_functions,
)

MAX_SQL_BYTES = 32 * 1024
_SQL_FENCE = re.compile(r"```[ \t]*sql\b[^\n]*\n(.*?)(?:```|\Z)", re.IGNORECASE | re.DOTALL)
_BARE_FENCE = re.compile(r"```[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)
# 트리 어디에 있어도 거부하는 문장 노드. CTE 본문의 INSERT … RETURNING, 서브쿼리 안의 PRAGMA
# 같은 것.
_STATEMENT_NODES = (
    "DML", "DDL", "Command", "Pragma", "Attach", "Detach", "Copy", "Install", "Set", "Use",
    "Describe", "Summarize", "Transaction", "Commit", "Rollback", "LoadData", "Analyze",
    "TruncateTable", "Grant", "Revoke", "Show", "Refresh", "Cache", "Uncache", "Export",
    "Create", "Drop", "Alter", "Insert", "Update", "Delete", "Merge", "Kill", "Execute",
    "Declare", "Into",
)  # fmt: skip
# 괄호로 감싼 질의 바깥에서 안쪽으로 옮길 수 있는 절.
_MOVABLE = ("order", "limit", "offset")


class SqlRejected(Exception):
    """검증 거부. code는 sql_query_log.reject_code와 노드·도구 오류 코드가 된다."""

    def __init__(self, code: str, message: str, *, subject: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # 문제가 된 표·함수·열 이름(화면 문구 재료).
        self.subject = subject


@dataclass(frozen=True)
class ValidatedSql:
    # 실행할 SQL(sql_executed). LIMIT을 넣고 * 를 풀어 쓴 뒤의 글이다.
    sql: str
    # 쓴 소스 표(스냅샷 이름 그대로).
    tables: tuple[str, ...]
    # 쿼리가 정의한 CTE 이름(소문자). 소스의 표·카탈로그 이름과 겹치지 않는다. SQLite authorizer가
    # 재귀 CTE를 열 없이 읽을 때(COUNT(*)) 허용한다.
    ctes: tuple[str, ...]
    # 넣거나 줄인 LIMIT 값(max_rows + 1 이하).
    limit: int


def extract_sql(text: str) -> str:
    """입력에서 첫 ```sql 펜스 안을 꺼낸다. 없으면 언어 표시 없는 첫 펜스, 그것도 없으면
    전체다(10.3)."""
    match = _SQL_FENCE.search(text) or _BARE_FENCE.search(text)
    return (match.group(1) if match else text).strip()


@cache
def _modules() -> tuple[ModuleType, ModuleType]:
    import sqlglot
    from sqlglot import exp

    # 모르는 구문을 Command로 읽을 때마다 경고를 남긴다. 그런 문장은 not_select로 거부한다.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    return sqlglot, exp


def _trim(sql: str) -> str:
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return lines[0][:300] if lines else type(exc).__name__


def _classes(exp: ModuleType, names: tuple[str, ...]) -> tuple[type, ...]:
    return tuple(found for name in names if isinstance(found := getattr(exp, name, None), type))


def validate_sql(sql: str, view: SourceView, *, max_rows: int) -> ValidatedSql:
    """SqlRejected를 던지거나 실행할 SQL을 돌려준다. 다른 예외는 내지 않는다."""
    text = _trim(sql)
    if not text:
        raise SqlRejected("sql_empty", "SQL이 비어 있다")
    if len(text.encode("utf-8")) > MAX_SQL_BYTES:
        raise SqlRejected("sql_too_long", f"SQL은 {MAX_SQL_BYTES // 1024}KB까지다")
    sqlglot, exp = _modules()
    try:
        return _validate(text, view, max_rows, sqlglot, exp)
    except SqlRejected:
        raise
    except RecursionError:
        raise SqlRejected("sql_invalid", "SQL이 너무 깊게 중첩돼 읽지 못했다") from None
    except Exception as exc:
        # sqlglot 안의 예기치 않은 예외(PIVOT의 AssertionError 등)도 거부로 바꾼다. 검사하지 못한
        # SQL은 실행하지 않는다.
        raise SqlRejected("sql_invalid", f"SQL을 검사하지 못했다: {_first_line(exc)}") from None


def _validate(
    text: str, view: SourceView, max_rows: int, sqlglot: ModuleType, exp: ModuleType
) -> ValidatedSql:
    dialect = view.dialect
    try:
        parsed: list[Any] = sqlglot.parse(text, read=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        raise SqlRejected("sql_invalid", f"SQL을 읽지 못했다: {_first_line(exc)}") from None
    statements = [
        item for item in parsed if item is not None and not isinstance(item, exp.Semicolon)
    ]
    if not statements:
        raise SqlRejected("sql_empty", "SQL이 비어 있다(주석뿐이다)")
    if len(statements) > 1:
        raise SqlRejected(
            "multiple_statements",
            "문장은 하나만 쓴다. 세미콜론으로 이은 여러 문장은 실행하지 않는다",
        )
    root = _unwrap(statements[0], exp)
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise SqlRejected(
            "not_select",
            f"SELECT 문만 실행한다({type(root).__name__.upper()} 문은 쓸 수 없다)",
            subject=type(root).__name__,
        )
    _reject_statements(root, exp)
    ctes = _check_cte_names(root, view, exp)
    tables = _check_tables(root, view, exp)
    _check_functions(root, view, exp)
    referenced = [found for name in tables if (found := view.table(name)) is not None]
    if any(table.hidden for table in referenced):
        if dialect == "sqlite":
            root = _qualify_strict(root, referenced, dialect, exp)
        else:
            expanded = _expand_stars(root, referenced, dialect, exp)
            if expanded is None:
                # * 를 풀지 못했다. 바깥에서 서브쿼리의 숨긴 열을 이름으로 부르면 그 이유로, 그
                # 밖(PIVOT 등)은 검사하지 못한 SQL로 거부한다.
                _reject_hidden_names(root, referenced, exp)
                raise SqlRejected(
                    "sql_invalid",
                    "* 를 보이는 열로 풀지 못했다(PIVOT 등). * 대신 열 이름을 직접 쓴다",
                )
            root = expanded
            _check_hidden(root, referenced, dialect, exp)
    limit = _apply_limit(root, max_rows, exp)
    return ValidatedSql(
        sql=root.sql(dialect=dialect, comments=False),
        tables=tuple(tables),
        ctes=tuple(sorted(ctes)),
        limit=limit,
    )


def _unwrap(root: Any, exp: ModuleType) -> Any:
    """괄호로 감싼 질의를 벗긴다. 바깥의 ORDER BY·LIMIT·OFFSET은 안쪽에 같은 절이 없을 때만 안으로
    옮기고, 그 밖의 바깥 절은 버리지 않고 거부한다."""
    while isinstance(root, exp.Subquery) and isinstance(root.this, exp.Query):
        inner = root.this
        for key, value in list(root.args.items()):
            if key == "this" or value is None or value == []:
                continue
            if key not in _MOVABLE or inner.args.get(key) is not None:
                raise SqlRejected(
                    "sql_invalid",
                    "괄호로 감싼 질의 바깥에는 안쪽에 없는 ORDER BY·LIMIT·OFFSET만 쓴다. 괄호 안에 "
                    "합쳐 쓴다",
                )
            inner.set(key, value)
        root = inner
    return root


def _reject_statements(root: Any, exp: ModuleType) -> None:
    denied = _classes(exp, _STATEMENT_NODES)
    for node in root.walk():
        if isinstance(node, denied):
            name = type(node).__name__
            raise SqlRejected(
                "not_select",
                f"SELECT 문만 실행한다(쿼리 안의 {name.upper()}도 쓸 수 없다)",
                subject=name,
            )


def _function_name(func: Any, exp: ModuleType) -> str:
    if isinstance(func, (exp.Anonymous, exp.AnonymousAggFunc)):
        return str(func.name).lower()
    try:
        return str(func.sql_name()).lower()
    except Exception:
        return type(func).__name__.lower()


def _check_cte_names(root: Any, view: SourceView, exp: ModuleType) -> set[str]:
    """CTE 이름(소문자). 소스의 표·뷰(숨긴 것 포함)나 카탈로그 이름과 겹치면 거부한다. 겹치면 범위
    밖의 실제 표 참조가 CTE로 읽혀 검증과 authorizer를 건너뛸 수 있다."""
    known = {table.name.lower() for table in view.tables} | set(view.hidden_tables)
    names: set[str] = set()
    for cte in root.find_all(exp.CTE):
        alias = str(cte.alias or "")
        key = alias.lower()
        if not key:
            continue
        if key in known or key.startswith(RESERVED_PREFIXES):
            raise SqlRejected(
                "cte_name_conflict",
                f"CTE 이름 {alias}이(가) 데이터 소스의 표 이름이나 예약된 이름(sqlite_·pragma_·"
                "duckdb_·pg_·information_schema)과 겹친다. 다른 이름을 쓴다",
                subject=alias,
            )
        names.add(key)
    return names


def _cte_references(root: Any) -> set[int]:
    """그 범위에서 보이는 CTE를 가리키는 표 노드(id). 범위 밖의 같은 이름은 실제 표다."""
    from sqlglot.optimizer.scope import Scope, traverse_scope

    found: set[int] = set()
    for scope in traverse_scope(root):
        for table in scope.tables:
            if isinstance(scope.sources.get(table.alias_or_name), Scope):
                found.add(id(table))
    return found


def _check_tables(root: Any, view: SourceView, exp: ModuleType) -> list[str]:
    allowed = {table.name.lower(): table.name for table in view.tables}
    dialect = view.dialect
    ctes = _cte_references(root)
    used: list[str] = []
    for table in root.find_all(exp.Table):
        inner = table.this
        if isinstance(inner, exp.Func):
            name = _function_name(inner, exp)
            anonymous = isinstance(inner, (exp.Anonymous, exp.AnonymousAggFunc))
            permitted = (
                name in TABLE_FUNCTIONS[dialect]
                if anonymous
                else type(inner).__name__ in TYPED_TABLE_FUNCTIONS[dialect]
            )
            if not permitted:
                raise SqlRejected(
                    "function_not_allowed",
                    f"표 함수 {name}은(는) 쓸 수 없다. 파일·경로를 읽는 함수는 막혀 있다",
                    subject=name,
                )
            continue
        name = str(table.name)
        key = name.lower()
        catalog = table.args.get("catalog")
        db = table.args.get("db")
        if catalog is not None or (db is not None and str(table.db).lower() != "main"):
            raise SqlRejected(
                "table_not_allowed",
                f"표 {table.sql(dialect=dialect)}은(는) 쓸 수 없다. 이 데이터 소스의 표만 쓴다",
                subject=name,
            )
        if db is None and id(table) in ctes:
            continue
        if key in view.hidden_tables:
            raise SqlRejected(
                "table_not_allowed", f"표 {name}은(는) 숨긴 표라 쓸 수 없다", subject=name
            )
        if key not in allowed:
            available = ", ".join(table.name for table in view.tables) or "(없음)"
            raise SqlRejected(
                "table_not_allowed",
                f"표 {name}은(는) 이 데이터 소스에 없다. 쓸 수 있는 표: {available}",
                subject=name,
            )
        if allowed[key] not in used:
            used.append(allowed[key])
    return used


def _check_functions(root: Any, view: SourceView, exp: ModuleType) -> None:
    anonymous = anonymous_functions(view.dialect)
    for func in root.find_all(exp.Func):
        if isinstance(func, (exp.Anonymous, exp.AnonymousAggFunc)):
            allowed = _function_name(func, exp) in anonymous
        else:
            allowed = type(func).__name__ in TYPED_FUNCTIONS
        if not allowed:
            name = _function_name(func, exp)
            raise SqlRejected(
                "function_not_allowed",
                f"함수 {name}은(는) 쓸 수 없다. 집계·문자열·날짜·수학·창 함수만 쓴다",
                subject=name,
            )


def _schema(tables: list[TableView], *, hidden: bool) -> dict[str, object]:
    # 타입은 열 이름을 푸는 데만 쓰므로 모두 TEXT로 둔다. 복합 타입 글자를 sqlglot이 못 읽을 수
    # 있다.
    schema: dict[str, object] = {}
    for table in tables:
        columns = [*table.columns, *table.hidden] if hidden else list(table.columns)
        schema[table.name] = {column.name: "TEXT" for column in columns}
    return schema


def _has_star(root: Any, exp: ModuleType) -> bool:
    for select in root.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            ):
                return True
    return False


def _expand_stars(root: Any, tables: list[TableView], dialect: str, exp: ModuleType) -> Any:
    """* 와 t.* 를 보이는 열 목록으로 풀어 쓴 트리. * 가 없으면 그대로, 풀지 못하면 None이다."""
    if not _has_star(root, exp):
        return root
    from sqlglot.optimizer.qualify_columns import qualify_columns

    tree = root.copy()
    _alias_derived_tables(tree, exp)
    _name_outputs(tree, dialect, exp)
    try:
        return qualify_columns(
            tree,
            schema=_schema(tables, hidden=False),
            expand_alias_refs=False,
            expand_stars=True,
            dialect=dialect,
        )
    except Exception:
        return None


def _alias_derived_tables(root: Any, exp: ModuleType) -> None:
    # 별칭 없는 파생 표(FROM (SELECT ...))는 * 를 풀면 한정자가 빈 열(.id)이 되어 실행이 깨진다.
    taken = {
        str(node.alias_or_name).lower()
        for node in root.find_all(exp.Table, exp.Subquery, exp.CTE)
        if node.alias_or_name
    }
    index = 0
    for subquery in list(root.find_all(exp.Subquery)):
        if subquery.alias or not isinstance(subquery.parent, exp.From | exp.Join):
            continue
        while f"_q{index}" in taken:
            index += 1
        taken.add(f"_q{index}")
        subquery.set("alias", exp.TableAlias(this=exp.to_identifier(f"_q{index}")))


def _unresolved(message: str | None = None) -> SqlRejected:
    return SqlRejected(
        "column_unresolved",
        message
        or "숨긴 열이 있는 표에서 열이 어느 표의 것인지 정하지 못했다. 표에 별칭을 붙이고 열은 "
        "별칭.열로, 조인은 USING 대신 ON 조건으로 다시 쓴다",
    )


def _qualify_strict(root: Any, tables: list[TableView], dialect: str, exp: ModuleType) -> Any:
    """SQLite에서 숨긴 열이 있는 표를 읽을 때 실행할 트리. 보이는 열만 든 스키마로 열을 모두 표에
    잇고(숨김을 뺀 사본이 이름을 푸는 방식과 같다) USING을 ON으로, * 를 보이는 열로 바꾼다. 하나라도
    하지 못하면 column_unresolved로 거부한다(통과시키지 않는다).
    """
    for join in root.find_all(exp.Join):
        if join.text("method").upper() == "NATURAL":
            raise _unresolved(
                "숨긴 열이 있는 표에는 NATURAL JOIN을 쓸 수 없다. 표에 별칭을 붙이고 ON 조건으로 "
                "다시 쓴다"
            )
    concealed = {
        hidden.name.lower(): (table.name, hidden.name)
        for table in tables
        for hidden in table.hidden
    }
    for name in _using_names(root, exp):
        if name.lower() in concealed:
            raise _hidden_rejection(*concealed[name.lower()])
    from sqlglot.optimizer.qualify_columns import qualify_columns

    tree = root.copy()
    _alias_derived_tables(tree, exp)
    _name_outputs(tree, dialect, exp)
    try:
        tree = qualify_columns(
            tree,
            schema=_schema(tables, hidden=False),
            expand_alias_refs=False,
            expand_stars=True,
            dialect=dialect,
        )
        scopes = _scopes(tree)
    except Exception:
        # 보이는 열로 풀리지 않는 이름이 숨긴 열이면 그 이유로 거부한다.
        _reject_hidden_names(root, tables, exp)
        raise _unresolved() from None
    if any(join.args.get("using") for join in tree.find_all(exp.Join)) or _has_star(tree, exp):
        # 남은 USING 조건과 풀리지 않은 * 는 숨긴 열까지 읽을 수 있다.
        raise _unresolved()
    _hidden_in_scopes(scopes, tables, exp)
    return tree


def _name_outputs(root: Any, dialect: str, exp: ModuleType) -> None:
    # 열을 표에 이으면 이름 없는 식(total(amount) 등)이 _col_N이 된다. 지금 글을 별칭으로 붙여 결과
    # 열 이름을 엔진이 붙였을 이름에 가깝게 둔다.
    for select in list(root.find_all(exp.Select)):
        for projection in list(select.expressions):
            if isinstance(projection, exp.Star) or projection.output_name:
                continue
            projection.replace(
                exp.alias_(projection.copy(), projection.sql(dialect=dialect), quoted=True)
            )


def _scopes(tree: Any) -> list[Any]:
    from sqlglot.optimizer.scope import traverse_scope

    return list(traverse_scope(tree))


def _reject_hidden_names(root: Any, tables: list[TableView], exp: ModuleType) -> None:
    names = {_lower(column) for column in _using_names(root, exp)}
    names.update(str(column.name).lower() for column in root.find_all(exp.Column))
    for table in tables:
        for hidden in table.hidden:
            if hidden.name.lower() in names:
                raise _hidden_rejection(table.name, hidden.name)


def _lower(value: Any) -> str:
    return str(value).lower()


def _using_names(root: Any, exp: ModuleType) -> list[str]:
    found: list[str] = []
    for join in root.find_all(exp.Join):
        for item in join.args.get("using") or []:
            found.append(str(getattr(item, "name", item)))
    return found


def _hidden_rejection(table: str, column: str) -> SqlRejected:
    return SqlRejected(
        "column_hidden",
        f"열 {table}.{column}은(는) 숨긴 열이라 쓸 수 없다",
        subject=f"{table}.{column}",
    )


def _check_hidden(root: Any, tables: list[TableView], dialect: str, exp: ModuleType) -> None:
    from sqlglot.optimizer.qualify_columns import qualify_columns

    try:
        qualified = qualify_columns(
            root.copy(), schema=_schema(tables, hidden=True), expand_stars=False, dialect=dialect
        )
        scopes = _scopes(qualified)
    except Exception:
        # 열을 표에 잇지 못했다. 숨긴 열과 이름이 같은 참조는 모두 거부한다.
        _reject_hidden_names(root, tables, exp)
        return
    _hidden_in_scopes(scopes, tables, exp)


def _hidden_in_scopes(scopes: list[Any], tables: list[TableView], exp: ModuleType) -> None:
    from sqlglot.expressions import Table

    hidden: Mapping[str, Mapping[str, str]] = {
        table.name.lower(): {column.name.lower(): column.name for column in table.hidden}
        for table in tables
        if table.hidden
    }
    names = {table.name.lower(): table.name for table in tables}
    for scope in scopes:
        bases = {
            alias: source.name.lower()
            for alias, source in scope.sources.items()
            if isinstance(source, Table)
        }
        for column in scope.columns:
            key = str(column.name).lower()
            if not key or key == "*":
                continue
            if column.table:
                base = bases.get(column.table)
                if base is not None and key in hidden.get(base, {}):
                    raise _hidden_rejection(names[base], hidden[base][key])
                continue
            # 표를 정하지 못한 열이 범위 안 어느 표의 숨긴 열과 이름이 같으면 거부한다.
            _reject_unbound(key, bases.values(), hidden, names)


def _reject_unbound(
    key: str,
    bases: Collection[str],
    hidden: Mapping[str, Mapping[str, str]],
    names: Mapping[str, str],
) -> None:
    for base in bases:
        if key in hidden.get(base, {}):
            raise _hidden_rejection(names[base], hidden[base][key])


def _int_literal(node: Any, exp: ModuleType) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            value = int(str(node.this))
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def _apply_limit(root: Any, max_rows: int, exp: ModuleType) -> int:
    cap = max(1, max_rows) + 1
    current = root.args.get("limit")
    if current is None:
        root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
        return cap
    if isinstance(current, exp.Fetch):
        options = current.args.get("limit_options")
        if options is not None and options.args.get("percent"):
            raise SqlRejected("limit_invalid", "LIMIT·FETCH에는 행 수(정수)만 쓴다")
        count = current.args.get("count")
    else:
        count = current.expression
    value = _int_literal(count, exp)
    if value is None or current.args.get("percent"):
        raise SqlRejected("limit_invalid", "LIMIT에는 0 이상의 정수만 쓴다")
    value = min(value, cap)
    root.set("limit", exp.Limit(expression=exp.Literal.number(value)))
    return value


def exposed_views(
    views: Mapping[str, str],
    *,
    hidden_tables: Collection[str],
    hidden_columns: Mapping[str, Collection[str]],
) -> list[str]:
    """숨긴 표·열(또는 그런 뷰)을 읽는 SQLite 뷰 이름(정의 순서). views는 이름 → CREATE VIEW 글이다.

    뷰가 숨긴 표나 이미 드러난 뷰를 읽거나, 숨긴 열이 있는 표를 * ·NATURAL·숨긴 열 이름으로
    읽으면 드러낸다고 본다. 정의를 읽지 못하면 숨긴 이름이 낱말로 들어 있는지만 본다. 판단이
    빗나가도 질의는 숨김을 뺀 사본에서 돌므로 값은 새지 않는다. 이 목록은 DDL·샘플에서 뷰를 치우는
    데 쓴다.
    """
    sqlglot, exp = _modules()
    concealed = {name.lower() for name in hidden_tables}
    columns = {
        table.lower(): {name.lower() for name in names} for table, names in hidden_columns.items()
    }
    found: list[str] = []
    for name, definition in views.items():
        try:
            exposes = _view_exposes(sqlglot, exp, definition, concealed, columns)
        except Exception:
            exposes = _mentions(definition, concealed, columns)
        if exposes:
            found.append(name)
            concealed.add(name.lower())
    return found


def _view_exposes(
    sqlglot: ModuleType,
    exp: ModuleType,
    definition: str,
    concealed: set[str],
    columns: Mapping[str, set[str]],
) -> bool:
    tree = sqlglot.parse_one(definition, read="sqlite")
    body = tree.expression if isinstance(tree, exp.Create) else tree
    ctes = {str(cte.alias).lower() for cte in body.find_all(exp.CTE) if cte.alias}
    referenced = {
        str(table.name).lower()
        for table in body.find_all(exp.Table)
        if not isinstance(table.this, exp.Func) and str(table.name).lower() not in ctes
    }
    if referenced & concealed:
        return True
    partial = {table: columns[table] for table in referenced if columns.get(table)}
    if not partial:
        return False
    if _has_star(body, exp) or any(
        join.text("method").upper() == "NATURAL" for join in body.find_all(exp.Join)
    ):
        return True
    used = {str(column.name).lower() for column in body.find_all(exp.Column)}
    used.update(name.lower() for name in _using_names(body, exp))
    return any(used & names for names in partial.values())


def _mentions(definition: str, concealed: set[str], columns: Mapping[str, set[str]]) -> bool:
    words = {word.lower() for word in re.findall(r"[^\W\d][\w$]*", definition)}
    if words & concealed:
        return True
    return any(words & names for names in columns.values())


def is_view_definition(definition: str) -> bool:
    """CREATE VIEW … AS SELECT 하나인지. 숨김을 뺀 사본에 뷰를 다시 만들기 전에 본다."""
    sqlglot, exp = _modules()
    try:
        parsed = [
            item
            for item in sqlglot.parse(definition, read="sqlite")
            if item is not None and not isinstance(item, exp.Semicolon)
        ]
    except Exception:
        return False
    if len(parsed) != 1:
        return False
    tree = parsed[0]
    if not isinstance(tree, exp.Create) or str(tree.args.get("kind") or "").upper() != "VIEW":
        return False
    body = tree.expression
    if not isinstance(body, (exp.Select, exp.SetOperation, exp.Subquery)):
        return False
    try:
        _reject_statements(body, exp)
    except SqlRejected:
        return False
    return True
