"""데이터 소스 규칙(3단계 설계 10.2, 10.4, 14.3). 파일 형식, 표 이름, 숨김, 준비 해시, 함수 목록.

검증(validate.py), DDL(ddl.py), 실행 스크립트(runner.py)가 같은 값을 보도록 여기 한 곳에 둔다. 특히
SQLite 함수 목록은 sqlglot 검증과 샌드박스 authorizer가 함께 쓴다.
"""

import hashlib
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.schemas.sql_source import (
    SqlAnnotations,
    SqlDialect,
    SqlFileFormat,
    SqlSchemaSnapshot,
    SqlSourceFile,
    SqlSourceKind,
)

FORMATS: dict[str, SqlFileFormat] = {
    "csv": "csv",
    "tsv": "tsv",
    "tab": "tsv",
    "xlsx": "xlsx",
    "db": "sqlite",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
}
KIND_FORMATS: dict[SqlSourceKind, frozenset[SqlFileFormat]] = {
    "files": frozenset({"csv", "tsv", "xlsx"}),
    "sqlite": frozenset({"sqlite"}),
}
TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# CTE 이름으로 쓸 수 없는 엔진 카탈로그 이름의 앞부분(소문자). 검증과 SQLite authorizer가 같이 쓴다.
RESERVED_PREFIXES = ("sqlite_", "pragma_", "duckdb_", "pg_", "information_schema")
_NOT_NAME = re.compile(r"[^a-z0-9]+")
MAX_TABLE_NAME = 63
SAMPLES_PER_COLUMN = 3
SAMPLE_CHARS = 60


def file_format(filename: str) -> SqlFileFormat | None:
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem:
        return None
    return FORMATS.get(extension.lower())


def dialect_of(kind: str) -> SqlDialect:
    return "sqlite" if kind == "sqlite" else "duckdb"


def default_table_name(filename: str, taken: Collection[str]) -> str:
    """파일 이름을 snake_case 표 이름으로. 영문·숫자가 없으면 table이고 겹치면 _2, _3을 붙인다."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    base = _NOT_NAME.sub("_", stem.lower()).strip("_")
    if not base:
        base = "table"
    elif not base[0].isalpha():
        base = f"t_{base}"
    base = base[:MAX_TABLE_NAME].rstrip("_")
    name, number = base, 2
    while name in taken:
        suffix = f"_{number}"
        name = base[: MAX_TABLE_NAME - len(suffix)] + suffix
        number += 1
    return name


def _lower(name: str) -> str:
    return name.lower()


def hidden_tables(annotations: SqlAnnotations) -> set[str]:
    return {_lower(name) for name, note in annotations.tables.items() if note.hidden}


def hidden_columns(annotations: SqlAnnotations) -> dict[str, set[str]]:
    """표(소문자) → 숨긴 열(소문자). 두 엔진 모두 식별자의 대소문자를 가리지 않는다."""
    found: dict[str, set[str]] = {}
    for table, note in annotations.tables.items():
        names = {_lower(column) for column, item in note.columns.items() if item.hidden}
        if names:
            found.setdefault(_lower(table), set()).update(names)
    return found


def needs_copy(annotations: SqlAnnotations) -> bool:
    """sqlite 소스가 숨김을 뺀 사본으로 질의해야 하는지. 숨긴 표·열이 하나라도 있으면 그렇다."""
    return bool(hidden_tables(annotations) or hidden_columns(annotations))


def source_hash(kind: str, files: Sequence[SqlSourceFile], annotations: SqlAnnotations) -> str:
    """준비 결과를 정하는 입력의 해시. 바뀌면 질의 파일·사본과 스냅샷을 다시 만든다(10.2).

    두 종류 모두 숨김을 넣는다. 숨긴 열은 files 소스의 질의 파일에도, sqlite 소스의 사본에도 없어야
    하므로 숨김이 바뀌면 준비 전까지 질의를 막는다.
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "files": [
            {
                "file_id": str(item.file_id),
                "sha256": item.sha256,
                "format": item.format,
                "table": item.table,
                "sheet": item.sheet,
                "options": item.options.model_dump(mode="json"),
            }
            for item in files
        ],
    }
    payload["hidden_tables"] = sorted(hidden_tables(annotations))
    payload["hidden_columns"] = {
        table: sorted(columns) for table, columns in sorted(hidden_columns(annotations).items())
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ColumnView:
    name: str
    type: str
    description: str = ""
    samples: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TableView:
    """보이는 표 하나. columns는 보이는 열만이고 hidden은 숨긴 열의 이름과 타입이다."""

    name: str
    kind: Literal["table", "view"]
    columns: tuple[ColumnView, ...]
    hidden: tuple[ColumnView, ...] = ()
    description: str = ""
    row_count: int | None = None


@dataclass(frozen=True)
class SourceView:
    """검증·DDL이 보는 데이터 소스. 숨긴 표는 tables에 없고 hidden_tables에 이름만 있다."""

    dialect: SqlDialect
    tables: tuple[TableView, ...]
    hidden_tables: frozenset[str] = field(default_factory=frozenset[str])

    def table(self, name: str) -> TableView | None:
        wanted = _lower(name)
        return next((item for item in self.tables if _lower(item.name) == wanted), None)

    @property
    def has_hidden(self) -> bool:
        return any(item.hidden for item in self.tables)


def source_view(
    kind: str, snapshot: SqlSchemaSnapshot | None, annotations: SqlAnnotations
) -> SourceView:
    dialect = dialect_of(kind)
    if snapshot is None:
        return SourceView(dialect=dialect, tables=())
    notes = {_lower(name): note for name, note in annotations.tables.items()}
    hidden = hidden_tables(annotations)
    tables: list[TableView] = []
    for table in snapshot.tables:
        key = _lower(table.name)
        if key in hidden:
            continue
        if table.error:
            # 깨진 뷰·사본에 없는 뷰는 질의할 수 없으므로 숨긴 표처럼 다룬다.
            hidden.add(key)
            continue
        note = notes.get(key)
        column_notes = (
            {_lower(name): item for name, item in note.columns.items()} if note is not None else {}
        )
        visible: list[ColumnView] = []
        concealed: list[ColumnView] = []
        for column in table.columns:
            column_note = column_notes.get(_lower(column.name))
            if column_note is not None and column_note.hidden:
                concealed.append(ColumnView(name=column.name, type=column.type))
                continue
            visible.append(
                ColumnView(
                    name=column.name,
                    type=column.type,
                    description=column_note.description if column_note is not None else "",
                    samples=tuple(column.samples[:SAMPLES_PER_COLUMN]),
                )
            )
        if not visible:
            # 열을 모두 숨긴 표는 질의 파일에도 없으므로 숨긴 표와 같다.
            hidden.add(key)
            continue
        tables.append(
            TableView(
                name=table.name,
                kind=table.kind,
                columns=tuple(visible),
                hidden=tuple(concealed),
                description=note.description if note is not None else "",
                row_count=table.row_count,
            )
        )
    return SourceView(dialect=dialect, tables=tuple(tables), hidden_tables=frozenset(hidden))


def strip_hidden_samples(snapshot: SqlSchemaSnapshot, annotations: SqlAnnotations) -> None:
    """숨긴 표·열의 샘플값을 지운다. 숨김을 바꾸면 다시 준비하기 전에도 바로 지운다."""
    hidden = hidden_columns(annotations)
    concealed_tables = hidden_tables(annotations)
    for table in snapshot.tables:
        key = _lower(table.name)
        for column in table.columns:
            if key in concealed_tables or _lower(column.name) in hidden.get(key, set()):
                column.samples = []


def sample_value(value: Any) -> Any:
    """스냅샷에 남길 샘플값. 긴 글은 자른다."""
    if isinstance(value, str) and len(value) > SAMPLE_CHARS:
        return value[:SAMPLE_CHARS] + "…"
    return value


# sqlglot이 함수 노드로 만드는 것 중 받아들이는 클래스(10.4의 6번). 이름이 정해진 함수와 함께 AND·
# CASE·CAST·EXISTS처럼 문법이 함수 노드가 되는 것도 들어 있다. 목록 밖(ReadCSV, Columns, 사용자·
# 버전 함수, AI·ML 함수 등)은 function_not_allowed다.
TYPED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # 문법·논리·형 변환
        "And", "Or", "Xor", "Case", "If", "Cast", "TryCast", "Coalesce", "Nullif", "Nvl2",
        "Greatest", "Least", "Exists", "Collate", "Negative", "Extract", "Array", "Struct",
        "List", "IsNan", "IsInf", "Typeof",
        # 집계
        "Count", "CountIf", "Sum", "Avg", "Min", "Max", "Median", "Mode", "Quantile",
        "ApproxQuantile", "PercentileCont", "PercentileDisc", "Stddev", "StddevPop",
        "StddevSamp", "Variance", "VariancePop", "Corr", "CovarPop", "CovarSamp", "ArgMax",
        "ArgMin", "First", "Last", "AnyValue", "ArrayAgg", "GroupConcat", "LogicalAnd",
        "LogicalOr", "ApproxDistinct", "Kurtosis", "Skewness", "RegrSlope", "RegrIntercept",
        "RegrR2", "RegrCount", "RegrAvgx", "RegrAvgy", "Grouping", "BitwiseAndAgg",
        "BitwiseOrAgg", "BitwiseXorAgg",
        # 창 함수
        "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile", "Lag", "Lead",
        "FirstValue", "LastValue", "NthValue",
        # 문자열
        "Length", "BitLength", "ByteLength", "Lower", "Upper", "Trim", "Substring", "Replace",
        "Concat", "ConcatWs", "StrPosition", "SplitPart", "Split", "RegexpLike",
        "RegexpReplace", "RegexpExtract", "RegexpExtractAll", "RegexpSplit", "RegexpCount",
        "RegexpFullMatch", "StartsWith", "EndsWith", "Contains", "Left", "Right", "Pad",
        "Reverse", "Repeat", "Initcap", "Translate", "Chr", "Unicode", "Ascii", "Hex", "Unhex",
        "MD5", "SHA", "SHA2", "Levenshtein", "JarowinklerSimilarity", "Soundex", "Overlay",
        "Space", "Normalize", "ArrayToString", "Format", "StringToArray",
        # 수학
        "Abs", "Round", "Ceil", "Floor", "Pow", "Sqrt", "Cbrt", "Exp", "Ln", "Log", "Sign",
        "Trunc", "Pi", "Sin", "Cos", "Tan", "Cot", "Asin", "Acos", "Atan", "Atan2", "Degrees",
        "Radians", "Factorial", "Rand", "SafeDivide", "WidthBucket",
        # 날짜·시간
        "CurrentDate", "CurrentTime", "CurrentTimestamp", "CurrentDatetime", "Localtime",
        "Localtimestamp", "Date", "Time", "Timestamp", "Datetime", "DateAdd", "DateSub",
        "DateDiff", "DateTrunc", "TimestampTrunc", "DatetimeTrunc", "TimeTrunc",
        "TimestampAdd", "TimestampSub", "TimestampDiff", "DatetimeAdd", "DatetimeSub",
        "DatetimeDiff", "TimeAdd", "TimeSub", "TimeDiff", "Year", "Month", "Day", "Hour",
        "Minute", "Second", "Week", "WeekOfYear", "Quarter", "DayOfWeek", "DayOfWeekIso",
        "DayOfMonth", "DayOfYear", "Dayname", "Monthname", "LastDay", "MonthsBetween",
        "AddMonths", "DateFromParts", "TimeFromParts", "TimestampFromParts", "StrToDate",
        "StrToTime", "StrToUnix", "TimeToStr", "TimeToUnix", "UnixToTime", "UnixToStr",
        "UnixToTimeStr", "TimeStrToTime", "TimeStrToDate", "TimeStrToUnix", "DateStrToDate",
        "DateToDateStr", "TsOrDsToDate", "TsOrDsToTimestamp", "TsOrDsToDatetime",
        "TsOrDsToTime", "TsOrDsAdd", "TsOrDsDiff", "TsOrDsToDateStr", "MakeInterval",
        "UnixDate", "UnixSeconds", "UnixMillis", "UnixMicros", "DateBin", "ToChar",
        "NumberToStr", "ToNumber", "ToDouble",
        # JSON
        "JSONExtract", "JSONExtractScalar", "JSONBExtract", "JSONBExtractScalar", "JSONType",
        "JSONFormat", "JSONKeys", "ParseJSON", "CheckJson", "JSONExists", "JSONArray",
        "JSONObject", "JSONArrayAgg", "JSONObjectAgg",
        # 목록(DuckDB). GenerateSeries·Unnest는 FROM의 표 함수로도 쓴다.
        "ArraySize", "ArrayContains", "ArrayDistinct", "ArraySlice", "ArraySort", "ArrayConcat",
        "ArrayFilter", "Transform", "ArrayFirst", "ArrayLast", "ArrayPosition", "ArrayReverse",
        "ArraySum", "ArrayMin", "ArrayMax", "ArrayAppend", "ArrayPrepend", "StructExtract",
        "GenerateSeries", "ExplodingGenerateSeries", "Unnest",
    }
)  # fmt: skip

# sqlglot이 모르는 이름(Anonymous)으로 나오는 함수. 방언마다 실제로 그 엔진에 있는 것만 둔다.
DUCKDB_FUNCTIONS: frozenset[str] = frozenset(
    {
        "date_part", "datepart", "now", "today", "epoch_ms", "epoch_us", "epoch_ns", "epoch",
        "make_timestamp", "make_time", "make_date", "age", "printf", "format", "list_value",
        "list_aggregate", "list_sort", "list_reverse_sort", "list_contains", "list_extract",
        "list_slice", "list_distinct", "list_unique", "list_has", "list_position", "list_sum",
        "list_min", "list_max", "list_avg", "list_count", "list_transform", "list_filter",
        "list_reduce", "array_length", "array_transform", "array_filter", "len", "string_split",
        "string_split_regex", "string_to_array", "regexp_split_to_array", "suffix", "prefix",
        "position", "instr", "bit_count", "gcd", "lcm", "isnan", "isinf", "isfinite", "even",
        "round_even", "fsum", "favg", "sumkahan", "bool_and", "bool_or", "arbitrary", "product",
        "mad", "entropy", "histogram", "weighted_avg", "min_by", "max_by", "approx_quantile",
        "reservoir_quantile", "quantile", "quantile_cont", "quantile_disc", "yearweek",
        "isodow", "isoyear", "millisecond", "microsecond", "decade", "century", "era",
        "julian", "to_timestamp", "struct_pack", "row", "map", "cardinality", "unnest",
        "generate_series", "range", "strip_accents", "nfc_normalize", "md5", "sha256", "hash",
        "levenshtein", "damerau_levenshtein", "jaccard", "jaro_similarity",
        "jaro_winkler_similarity", "hamming", "mismatches", "editdist3", "lpad", "rpad",
        "ltrim", "rtrim", "trim", "left", "right", "repeat", "replace", "reverse",
        "split_part", "starts_with", "ends_with", "contains", "regexp_matches",
        "regexp_full_match", "regexp_extract", "regexp_extract_all", "regexp_replace",
        "format_bytes", "formatreadablesize", "formatreadabledecimalsize", "to_base", "bin",
        "time_bucket", "try_strptime", "strptime", "strftime", "dayname", "monthname",
        "last_day", "weekday", "weekofyear", "dayofmonth", "dayofweek", "dayofyear",
        "json_extract", "json_extract_string", "json_extract_path", "json_extract_path_text",
        "json_value", "json_array_length", "json_keys", "json_type", "json_valid",
        "json_object", "json_array", "to_json", "json_structure", "json_contains",
        "json_group_array", "json_group_object", "json_merge_patch", "list", "array_agg",
        "string_agg", "group_concat", "listagg", "count_star", "first", "last", "any_value",
        "kurtosis", "kurtosis_pop", "skewness", "var_samp", "var_pop", "stddev_samp",
        "stddev_pop", "corr", "covar_pop", "covar_samp", "regr_slope", "regr_intercept",
        "regr_r2", "regr_count", "regr_avgx", "regr_avgy", "regr_sxx", "regr_syy", "regr_sxy",
        "ascii", "chr", "unicode", "ord", "lower", "upper", "lcase", "ucase", "length",
        "strlen", "char_length", "bit_length", "octet_length", "concat", "concat_ws",
        "array_to_string", "list_aggr", "ifnull", "nullif", "coalesce", "greatest", "least",
        "typeof", "abs", "sign", "round", "ceil", "ceiling", "floor", "trunc", "sqrt", "cbrt",
        "pow", "power", "exp", "ln", "log", "log2", "log10", "pi", "degrees", "radians",
        "sin", "cos", "tan", "cot", "asin", "acos", "atan", "atan2", "factorial", "mod",
        "divide", "add", "subtract", "multiply", "date_diff", "datediff", "date_sub",
        "date_add", "date_trunc", "datetrunc", "year", "month", "day", "hour", "minute",
        "second", "week", "quarter", "to_days", "to_hours", "to_minutes",
        "to_seconds", "to_months", "to_years", "to_weeks", "to_milliseconds", "if", "iif",
        "array_slice", "array_contains", "array_position", "array_sort", "array_reverse_sort",
        "array_distinct", "array_unique", "array_has", "array_sum", "array_min", "array_max",
        "array_concat", "list_concat", "array_value", "struct_extract", "map_keys",
        "map_values", "map_extract", "element_at",
    }
)  # fmt: skip

# SQLite가 부르는 함수 이름(소문자). sqlglot 검증의 Anonymous 허용 목록이자 샌드박스 authorizer의
# SQLITE_FUNCTION 허용 목록이다. like·glob은 LIKE·GLOB 연산자, ->·->>는 JSON 연산자가 부르는
# 이름이다. load_extension, readfile, writefile, fts3_tokenizer, zeroblob, randomblob, sqlite_* 는
# 넣지 않는다.
SQLITE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count", "sum", "avg", "min", "max", "total", "group_concat", "string_agg", "abs",
        "round", "length", "octet_length", "lower", "upper", "trim", "ltrim", "rtrim", "substr",
        "substring", "replace", "instr", "printf", "format", "char", "unicode", "hex", "unhex",
        "quote", "coalesce", "ifnull", "iif", "if", "nullif", "likely", "unlikely",
        "likelihood", "typeof", "date", "time", "datetime", "julianday", "unixepoch",
        "strftime", "timediff", "json", "jsonb", "json_extract", "jsonb_extract", "json_array",
        "json_object", "json_type", "json_valid", "json_array_length", "json_group_array",
        "json_group_object", "json_quote", "json_each", "json_tree", "->", "->>", "like",
        "glob", "random", "sign", "ceil", "ceiling", "floor", "trunc", "ln", "log", "log2",
        "log10", "exp", "pow", "power", "sqrt", "mod", "pi", "sin", "cos", "tan", "asin",
        "acos", "atan", "atan2", "degrees", "radians", "row_number", "rank", "dense_rank",
        "percent_rank", "cume_dist", "ntile", "lag", "lead", "first_value", "last_value",
        "nth_value", "concat", "concat_ws", "match", "soundex", "median",
    }
)  # fmt: skip

# FROM 자리의 표 함수. 나머지 표 함수(read_csv, glob, pragma_table_info 등)는
# function_not_allowed다.
TABLE_FUNCTIONS: dict[SqlDialect, frozenset[str]] = {
    "duckdb": frozenset({"unnest", "generate_series", "range"}),
    "sqlite": frozenset({"json_each", "json_tree"}),
}
TYPED_TABLE_FUNCTIONS: dict[SqlDialect, frozenset[str]] = {
    "duckdb": frozenset({"GenerateSeries", "ExplodingGenerateSeries", "Unnest"}),
    "sqlite": frozenset(),
}


def anonymous_functions(dialect: SqlDialect) -> frozenset[str]:
    return SQLITE_FUNCTIONS if dialect == "sqlite" else DUCKDB_FUNCTIONS
