"""결과 처리(3단계 설계 10.5, 10.6).

rows는 1MB 안, result_text는 llm_rows·llm_bytes 안이고 넘치면 열이 아니라 행을 줄인다.
"""

from app.knowledge.sql.results import (
    cell,
    markdown_table,
    result_text,
    row_objects,
    summary_line,
    unique_columns,
)


def test_duplicate_column_names_get_suffixes() -> None:
    assert unique_columns(["id", "name", "id", "id", ""]) == [
        "id",
        "name",
        "id_2",
        "id_3",
        "column",
    ]
    assert unique_columns(["a", "a_2", "a"]) == ["a", "a_2", "a_3"]


def test_rows_are_cut_at_the_byte_budget() -> None:
    rows = [[index, "가" * 100] for index in range(100)]

    objects, cut = row_objects(["n", "text"], rows, max_bytes=5_000)

    assert cut is True
    assert 10 < len(objects) < 100
    assert objects[0] == {"n": 0, "text": "가" * 100}
    whole, cut_none = row_objects(["n", "text"], rows[:3], max_bytes=5_000)
    assert cut_none is False and len(whole) == 3


def test_cells_are_one_line_and_escaped() -> None:
    assert cell(None) == "NULL"
    assert cell(True) == "true"
    assert cell(1.5) == "1.5"
    assert cell("a|b\nc") == "a\\|b c"
    assert cell({"k": [1, 2]}) == '{"k": [1, 2]}'
    assert cell("가" * 300).endswith("…") and len(cell("가" * 300)) == 201


def test_markdown_table_drops_rows_not_columns() -> None:
    rows = [[index, "x" * 50] for index in range(50)]

    table, shown = markdown_table(["n", "text"], rows, max_rows=40, max_bytes=1_000)

    lines = table.splitlines()
    assert lines[0] == "| n | text |" and lines[1] == "| --- | --- |"
    assert shown == len(lines) - 2 and 0 < shown < 40
    assert len(table.encode("utf-8")) <= 1_000


def test_result_text_marks_the_table_as_data_and_counts_rows() -> None:
    rows = [["서울", 10], ["부산</document>", 20]]

    text = result_text(
        ["region", "total"],
        rows,
        row_count=2,
        truncated=False,
        max_rows=200,
        llm_rows=50,
        llm_bytes=65536,
        source_name="매출\n파일",
    )

    assert text.startswith('<document name="질의 결과" source="매출 파일">\n| region | total |')
    # 표 속 닫는 표식은 무력화해 자료가 일찍 끝나지 않는다.
    assert "| 부산<\\/document> | 20 |" in text
    assert text.endswith("</document>\n2행.")


def test_summary_lines() -> None:
    assert summary_line(row_count=0, shown=0, truncated=False, max_rows=200) == "결과 행이 없다."
    assert (
        summary_line(row_count=200, shown=50, truncated=True, max_rows=200)
        == "결과가 200행을 넘어 잘렸다. 처음 50행만 싣는다."
    )
    assert summary_line(row_count=120, shown=50, truncated=False, max_rows=200) == (
        "120행 중 처음 50행만 싣는다."
    )
    text = result_text(
        ["n"],
        [[index] for index in range(120)],
        row_count=120,
        truncated=False,
        max_rows=200,
        llm_rows=50,
        llm_bytes=65536,
        source_name="s",
    )
    assert text.endswith("120행 중 처음 50행만 싣는다.")
    assert "| 49 |" in text and "| 50 |" not in text
