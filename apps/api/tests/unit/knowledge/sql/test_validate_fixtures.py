"""검증 픽스처 51개(허용 21·거부 30, 3단계 설계 10.4, M27).

FROM '<경로>'와 read_csv, CTE 이름으로 실제 표 가리기, 숨긴 열의 USING·NATURAL, PIVOT, 괄호 밖
LIMIT을 포함한다.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.sql.sources import SourceView
from app.knowledge.sql.validate import SqlRejected, validate_sql

CASES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).resolve().parents[3] / "fixtures" / "sql" / "validation_cases.json").read_text(
        encoding="utf-8"
    )
)


def test_the_fixture_covers_the_reviewed_cases() -> None:
    assert len(CASES["allowed"]) == 21 and len(CASES["rejected"]) == 30
    rejected = " ".join(case["sql"] for case in CASES["rejected"])
    assert "read_csv(" in rejected and "FROM 'C:/" in rejected
    codes = [case["expect"] for case in CASES["rejected"]]
    assert codes.count("cte_name_conflict") == 3 and "column_unresolved" in codes
    assert "PIVOT" in rejected and ") LIMIT 100000" in rejected


@pytest.mark.parametrize("case", CASES["allowed"], ids=lambda case: case["sql"][:40])
def test_allowed(case: dict[str, Any], view: Callable[[str], SourceView]) -> None:
    validated = validate_sql(case["sql"], view(case["dialect"]), max_rows=200)

    assert case["contains"] in validated.sql
    assert validated.limit <= 201
    # 숨긴 열은 * 를 풀어 쓴 결과에도 나오지 않는다.
    assert "card" not in validated.sql and "email" not in validated.sql


@pytest.mark.parametrize("case", CASES["rejected"], ids=lambda case: case["sql"][:40])
def test_rejected(case: dict[str, Any], view: Callable[[str], SourceView]) -> None:
    with pytest.raises(SqlRejected) as caught:
        validate_sql(case["sql"], view(case["dialect"]), max_rows=200)

    assert caught.value.code == case["expect"], caught.value.message
    assert caught.value.message
