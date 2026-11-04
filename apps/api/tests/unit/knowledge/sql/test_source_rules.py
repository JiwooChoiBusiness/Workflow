"""데이터 소스 규칙(3단계 설계 10.2). 형식, 기본 표 이름, 준비 해시."""

import uuid

from app.knowledge.sql.sources import default_table_name, file_format, needs_copy, source_hash
from app.schemas.sql_source import (
    ColumnNote,
    SqlAnnotations,
    SqlFileOptions,
    SqlSourceFile,
    TableNote,
)


def test_file_formats() -> None:
    assert file_format("매출.CSV") == "csv"
    assert file_format("a.tsv") == "tsv"
    assert file_format("book.xlsx") == "xlsx"
    assert file_format("shop.sqlite3") == "sqlite"
    assert file_format("old.xls") is None
    assert file_format(".csv") is None
    assert file_format("noext") is None


def test_default_table_names() -> None:
    assert default_table_name("2024 매출 (최종).csv", ()) == "t_2024"
    assert default_table_name("Sales Q1.csv", ()) == "sales_q1"
    assert default_table_name("매출.csv", ()) == "table"
    assert default_table_name("sales.csv", {"sales"}) == "sales_2"
    assert default_table_name("sales.csv", {"sales", "sales_2"}) == "sales_3"
    assert len(default_table_name("a" * 200 + ".csv", ())) == 63


def file(**options: object) -> SqlSourceFile:
    return SqlSourceFile(
        file_id=uuid.UUID(int=1),
        storage_key="sql-sources/w/1",
        filename="a.csv",
        mime="text/csv",
        size_bytes=1,
        sha256="x",
        format="csv",
        table="a",
        options=SqlFileOptions.model_validate(options),
    )


def test_the_prepare_hash_follows_files_options_and_hidden_columns() -> None:
    plain = SqlAnnotations()
    hidden = SqlAnnotations(tables={"a": TableNote(columns={"b": ColumnNote(hidden=True)})})
    described = SqlAnnotations(tables={"a": TableNote(description="설명만 바뀜")})

    base = source_hash("files", [file()], plain)

    assert source_hash("files", [file()], described) == base
    assert source_hash("files", [file(encoding="cp949")], plain) != base
    assert source_hash("files", [file()], hidden) != base
    # sqlite 소스도 숨김이 바뀌면 숨김을 뺀 사본을 다시 만들어야 하므로 해시에 넣는다.
    assert source_hash("sqlite", [file()], hidden) != source_hash("sqlite", [file()], plain)
    assert source_hash("sqlite", [file()], described) == source_hash("sqlite", [file()], plain)
    assert needs_copy(hidden) and not needs_copy(described)
