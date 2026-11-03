"""메타데이터 필터(3단계 설계 7.5). 연산자마다 kb_document_metadata에 대한 EXISTS 조건으로 바뀌고
and·or로 묶인다. 실제 SQLite에서 어떤 문서가 걸리는지 본다."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.engine import Database
from app.db.models import (
    KbDocument,
    KbDocumentMetadata,
    KbMetadataField,
    KnowledgeBase,
    Workspace,
)
from app.db.repositories.knowledge_search import KbSearchDocumentRepository
from app.knowledge.retrieval.filters import (
    FieldInfo,
    FieldType,
    FilterError,
    check_fields,
    clause,
    common_fields,
    plan,
)
from app.schemas.retrieval import MetadataCondition, MetadataFilter

JAN_10 = datetime(2026, 1, 10, tzinfo=UTC)
TYPES: dict[str, FieldType] = {"team": "string", "year": "number", "published": "time"}
# 문서 → 필드 값. 없는 필드는 값 행이 없다. d3의 team은 빈 글이다.
DOCS: dict[str, dict[str, Any]] = {
    "d1": {"team": "Sales", "year": 2024.0, "published": JAN_10},
    "d2": {"team": "sales team", "year": 2025.0, "published": datetime(2026, 3, 1, tzinfo=UTC)},
    "d3": {"team": "", "year": 2023.0},
    "d4": {"published": datetime(2025, 12, 31, 23, tzinfo=UTC)},
    "d5": {"team": "R&D", "year": 2025.5, "published": JAN_10},
}


class Seeded:
    def __init__(self, db: Database, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        self.db = db
        self.workspace_id = workspace_id
        self.kb_id = kb_id
        self.names: dict[uuid.UUID, str] = {}
        self.field_ids: dict[str, uuid.UUID] = {}

    async def match(
        self, *conditions: dict[str, Any], logical: str = "and", completed_only: bool = True
    ) -> set[str]:
        # 색인 중인 문서(busy)도 d1과 같은 값을 갖는다. 기본은 색인이 끝난 문서만 본다.
        built = MetadataFilter.model_validate(
            {"logical_operator": logical, "conditions": list(conditions)}
        )
        async with self.db.read() as session:
            found = await KbSearchDocumentRepository(session, self.workspace_id).searchable(
                self.kb_id,
                completed_only=completed_only,
                plan=plan(built, TYPES),
                field_ids=self.field_ids,
            )
        return {self.names[doc_id] for doc_id in found}


@pytest.fixture
async def seeded(tmp_path: Path) -> AsyncIterator[Seeded]:
    db = Database(tmp_path / "filters.db")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.write() as session:
        workspace = Workspace(name="w", slug="w", settings={})
        session.add(workspace)
        await session.flush()
        kb = KnowledgeBase(
            workspace_id=workspace.id,
            slug="kb",
            name="kb",
            doc_form="text_model",
            indexing_technique="economy",
        )
        session.add(kb)
        await session.flush()
        state = Seeded(db, workspace.id, kb.id)
        for position, (name, kind) in enumerate(TYPES.items(), start=1):
            field = KbMetadataField(
                workspace_id=workspace.id, kb_id=kb.id, name=name, type=kind, position=position
            )
            session.add(field)
            await session.flush()
            state.field_ids[name] = field.id
        extra = {
            "gone": {"deleted_at": JAN_10},
            "shelved": {"archived": True},
            "off": {"enabled": False},
            "busy": {"indexing_status": "indexing"},
        }
        for position, name in enumerate([*DOCS, *extra], start=1):
            columns: dict[str, Any] = {"indexing_status": "completed", **extra.get(name, {})}
            document = KbDocument(
                workspace_id=workspace.id,
                kb_id=kb.id,
                position=position,
                name=name,
                data_source_type="paste_text",
                filename=f"{name}.txt",
                mime="text/plain",
                size_bytes=1,
                sha256="0" * 64,
                storage_key=name,
                batch="b",
                **columns,
            )
            session.add(document)
            await session.flush()
            state.names[document.id] = name
            # 검색 못 하는 문서도 d1과 같은 값을 갖게 해 필터가 아니라 상태로 빠지는지 본다.
            values = DOCS.get(name, DOCS["d1"])
            for field_name, value in values.items():
                column = {"string": "value_string", "number": "value_number"}.get(
                    TYPES[field_name], "value_time"
                )
                session.add(
                    KbDocumentMetadata(
                        workspace_id=workspace.id,
                        kb_id=kb.id,
                        document_id=document.id,
                        field_id=state.field_ids[field_name],
                        **{column: value},
                    )
                )
    yield state
    await db.close()


STRING_CASES = [
    ("is", "Sales", {"d1"}),
    ("is not", "Sales", {"d2", "d3", "d5"}),
    ("contains", "ales", {"d1", "d2"}),
    ("contains", "Sales", {"d1"}),
    ("not contains", "ales", {"d3", "d5"}),
    ("start with", "sales", {"d2"}),
    ("starts with", "S", {"d1"}),
    ("end with", "team", {"d2"}),
    ("ends with", "D", {"d5"}),
    ("empty", None, {"d3", "d4"}),
    ("is not empty", None, {"d1", "d2", "d5"}),
    ("in", ["Sales", "R&D"], {"d1", "d5"}),
    ("in", "Sales, R&D", {"d1", "d5"}),
    ("not in", ["Sales"], {"d2", "d3", "d5"}),
]
NUMBER_CASES = [
    ("=", 2025, {"d2"}),
    ("≠", 2025, {"d1", "d3", "d5"}),
    (">", 2024, {"d2", "d5"}),
    ("<", 2024, {"d3"}),
    (">=", 2024, {"d1", "d2", "d5"}),
    ("≤", "2024", {"d1", "d3"}),
    ("empty", None, {"d4"}),
    ("not empty", None, {"d1", "d2", "d3", "d5"}),
    ("in", [2024, 2023], {"d1", "d3"}),
    ("not in", "2024", {"d2", "d3", "d5"}),
]
TIME_CASES = [
    ("is", "2026-01-10T09:00:00+09:00", {"d1", "d5"}),
    ("is", JAN_10.timestamp(), {"d1", "d5"}),
    ("before", "2026-01-01T00:00:00Z", {"d4"}),
    ("after", "2026-01-10T00:00:00+00:00", {"d2"}),
    ("empty", None, {"d3"}),
    ("not empty", None, {"d1", "d2", "d4", "d5"}),
]


@pytest.mark.parametrize(("operator", "value", "expected"), STRING_CASES)
async def test_string_operators(
    seeded: Seeded, operator: str, value: Any, expected: set[str]
) -> None:
    condition = {"name": "team", "comparison_operator": operator, "value": value}

    assert await seeded.match(condition) == expected


@pytest.mark.parametrize(("operator", "value", "expected"), NUMBER_CASES)
async def test_number_operators(
    seeded: Seeded, operator: str, value: Any, expected: set[str]
) -> None:
    condition = {"name": "year", "comparison_operator": operator, "value": value}

    assert await seeded.match(condition) == expected


@pytest.mark.parametrize(("operator", "value", "expected"), TIME_CASES)
async def test_time_operators(
    seeded: Seeded, operator: str, value: Any, expected: set[str]
) -> None:
    condition = {"name": "published", "comparison_operator": operator, "value": value}

    assert await seeded.match(condition) == expected


async def test_and_or_combine_the_conditions(seeded: Seeded) -> None:
    both = await seeded.match(
        {"name": "team", "comparison_operator": "contains", "value": "ales"},
        {"name": "year", "comparison_operator": "≥", "value": 2025},
    )
    either = await seeded.match(
        {"name": "team", "comparison_operator": "is", "value": "R&D"},
        {"name": "year", "comparison_operator": "<", "value": 2024},
        logical="or",
    )

    assert both == {"d2"}
    assert either == {"d3", "d5"}


async def test_unsearchable_documents_stay_out(seeded: Seeded) -> None:
    condition = {"name": "team", "comparison_operator": "is", "value": "Sales"}

    assert await seeded.match(condition) == {"d1"}
    # search_while_indexing이 켜져 있으면 색인 중인 문서도 검색한다(6.7).
    assert await seeded.match(condition, completed_only=False) == {"d1", "busy"}
    async with seeded.db.read() as session:
        repo = KbSearchDocumentRepository(session, seeded.workspace_id)
        everything = await repo.searchable(seeded.kb_id, completed_only=False)
        done = await repo.searchable(seeded.kb_id, completed_only=True)
        rows = await repo.count_rows(seeded.kb_id)

    assert {seeded.names[doc_id] for doc_id in everything} == {*DOCS, "busy"}
    assert {seeded.names[doc_id] for doc_id in done} == set(DOCS)
    assert rows == len(DOCS) + 4


def condition(name: str, operator: str, value: Any = None) -> MetadataCondition:
    return MetadataCondition.model_validate(
        {"name": name, "comparison_operator": operator, "value": value}
    )


@pytest.mark.parametrize(
    ("field_type", "operator", "value"),
    [
        ("number", "contains", "1"),
        ("time", ">", "2026-01-01T00:00:00Z"),
        ("string", "is", None),
        ("string", "is", ["a", "b"]),
        ("string", "contains", ""),
        ("string", "in", []),
        ("number", "=", "many"),
        ("time", "is", "2026-01-10"),
        ("time", "before", "2026-01-10T09:00:00"),
    ],
)
def test_bad_conditions_are_refused(field_type: FieldType, operator: str, value: Any) -> None:
    with pytest.raises(FilterError) as caught:
        clause(condition("field", operator, value), field_type)

    assert caught.value.code == "metadata_condition_invalid"


def test_operator_aliases_land_on_the_dify_names() -> None:
    names = [
        condition("f", alias).comparison_operator
        for alias in ("starts with", "Ends With", "is empty", "is not empty", "!=", ">=", "<=")
    ]

    assert names == ["start with", "end with", "empty", "not empty", "≠", "≥", "≤"]


def test_fields_must_exist_with_one_type_in_every_kb() -> None:
    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    shared = FieldInfo(uuid.uuid4(), "team", "string")
    per_kb = {
        first: [shared, FieldInfo(uuid.uuid4(), "year", "number")],
        second: [shared, FieldInfo(uuid.uuid4(), "year", "string")],
    }

    with pytest.raises(FilterError) as missing:
        check_fields([condition("owner", "is", "a")], per_kb)
    with pytest.raises(FilterError) as mismatch:
        check_fields([condition("year", "=", 1)], per_kb)

    assert missing.value.code == "metadata_field_not_found"
    assert mismatch.value.code == "metadata_field_mismatch"
    assert check_fields([condition("team", "is", "a")], per_kb) == {"team": "string"}
    assert common_fields(list(per_kb.values())) == {"team": "string"}
