import math
import uuid
from typing import Any

import pytest

from app.knowledge import index_db as index_db_module
from app.knowledge.index_db import IndexDb
from app.knowledge.units import VecRow, vec_table_name
from app.knowledge.vector_index import MAX_K, SqliteVecIndex
from app.llm.embeddings import EmbeddingConfig, pack_vector, unpack_vector
from app.llm.fake_adapter import fake_embedding

WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000003")
CONFIG = EmbeddingConfig(dimensions=4)


def unit(*values: float) -> bytes:
    norm = math.sqrt(sum(value * value for value in values))
    return pack_vector([value / norm for value in values])


async def ready(db: IndexDb, config: EmbeddingConfig = CONFIG) -> tuple[SqliteVecIndex, uuid.UUID]:
    index = SqliteVecIndex(db)
    kb_id = uuid.uuid4()
    assert await index.ensure(kb_id, config, workspace_id=WORKSPACE)
    return index, kb_id


async def test_ensure_records_the_config_and_is_idempotent(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)

    meta = await index.describe(kb_id)
    assert meta is not None
    assert meta.vec_table == vec_table_name(kb_id)
    assert (meta.dims, meta.config_key, meta.embedding_version) == (4, CONFIG.key(), 1)
    assert not await index.ensure(kb_id, CONFIG, workspace_id=WORKSPACE)


@pytest.mark.parametrize(
    "changed",
    [
        EmbeddingConfig(dimensions=4, version=2),
        EmbeddingConfig(dimensions=8),
        EmbeddingConfig(dimensions=4, model="gemini-embedding-001"),
        EmbeddingConfig(dimensions=4, prefix="none"),
    ],
    ids=["version", "dimensions", "model", "prefix"],
)
async def test_changed_config_recreates_an_empty_table(
    index_db: IndexDb, changed: EmbeddingConfig
) -> None:
    # 같은 차원에 모델만 바뀌어도 vec0는 오류 없이 엉뚱한 결과를 내므로 설정으로 가른다(5.4).
    index, kb_id = await ready(index_db)
    await index.upsert(kb_id, [VecRow(1, unit(1, 0, 0, 0), "doc")])

    assert await index.ensure(kb_id, changed, workspace_id=WORKSPACE)

    assert await index.count(kb_id) == 0
    meta = await index.describe(kb_id)
    assert meta is not None and meta.config_key == changed.key()
    assert meta.embedding_version == changed.version and meta.dims == changed.dimensions


async def test_knn_returns_cosine_similarity_in_order(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    await index.upsert(
        kb_id,
        [
            VecRow(1, unit(1, 0, 0, 0), "a"),
            VecRow(2, unit(1, 1, 0, 0), "a"),
            VecRow(3, unit(0, 1, 0, 0), "b"),
            VecRow(4, unit(-1, 0, 0, 0), "b"),
        ],
    )

    hits = await index.search(kb_id, unit(1, 0, 0, 0), 3)

    assert [hit.unit_no for hit in hits] == [1, 2, 3]
    assert hits[0].cosine == pytest.approx(1.0, abs=1e-6)
    assert hits[1].cosine == pytest.approx(math.sqrt(0.5), abs=1e-6)
    assert hits[2].cosine == pytest.approx(0.0, abs=1e-6)


async def test_upsert_replaces_the_row_in_one_transaction(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    await index.upsert(kb_id, [VecRow(7, unit(1, 0, 0, 0), "old", "segment")])

    await index.upsert(kb_id, [VecRow(7, unit(0, 0, 1, 0), "new", "child")])

    assert await index.count(kb_id) == 1
    assert await index.get(kb_id, 7) == unit(0, 0, 1, 0)
    hits = await index.search(kb_id, unit(0, 0, 1, 0), 1, doc_ids=["new"])
    assert [hit.unit_no for hit in hits] == [7]
    assert await index.search(kb_id, unit(0, 0, 1, 0), 1, doc_ids=["old"]) == []


async def test_document_filter_only_sees_those_documents(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    rows = [
        VecRow(unit_no, unit(1, unit_no, 0, 0), f"doc-{unit_no % 3}") for unit_no in range(1, 31)
    ]
    await index.upsert(kb_id, rows)

    hits = await index.search(kb_id, unit(1, 0, 0, 0), 5, doc_ids=["doc-1", "doc-2"])

    assert len(hits) == 5
    assert all(hit.unit_no % 3 in (1, 2) for hit in hits)
    assert await index.search(kb_id, unit(1, 0, 0, 0), 5, doc_ids=[]) == []


async def test_rowid_path_for_old_sqlite_gives_the_same_hits(
    make_index_db: Any,
) -> None:
    # SQLite 3.38 미만에서는 vec0의 doc_id IN이 안 되어 rowid 점 조회로 찾는다(M21 검토 2-7).
    # 여기 SQLite는 새것이라 같은 데이터에서 두 경로의 결과를 맞대어 본다.
    rows = [
        VecRow(unit_no, unit(1, unit_no * 0.1, unit_no % 4, 0), f"doc-{unit_no % 5}")
        for unit_no in range(1, 61)
    ]
    query = unit(1, 0.2, 1, 0)
    found: list[list[tuple[int, float]]] = []
    for doc_filter_in in (True, False):
        db = await make_index_db(f"index-{doc_filter_in}.db", doc_filter_in=doc_filter_in)
        assert db.doc_filter_in is doc_filter_in
        index, kb_id = await ready(db)
        await index.upsert(kb_id, rows)
        hits = await index.search(kb_id, query, 7, doc_ids=["doc-1", "doc-3"])
        found.append([(hit.unit_no, round(hit.cosine, 5)) for hit in hits])
        assert all(hit.unit_no % 5 in (1, 3) for hit in hits)
        assert await index.search(kb_id, query, 7, doc_ids=["none"]) == []

    assert found[0] == found[1]
    assert len(found[0]) == 7


def test_old_sqlite_versions_use_the_rowid_path() -> None:
    assert index_db_module.doc_filter_in_supported((3, 38, 0))
    assert not index_db_module.doc_filter_in_supported((3, 37, 2))


async def test_large_document_filters_are_split_and_merged(
    index_db: IndexDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.knowledge import vector_index

    monkeypatch.setattr(vector_index, "DOC_FILTER_BATCH", 4)
    index, kb_id = await ready(index_db)
    await index.upsert(
        kb_id,
        [VecRow(unit_no, unit(1, unit_no, 0, 0), f"doc-{unit_no}") for unit_no in range(1, 21)],
    )
    wanted = [f"doc-{unit_no}" for unit_no in (2, 4, 6, 8, 10, 12, 14, 16, 18, 20)]

    hits = await index.search(kb_id, unit(1, 0, 0, 0), 3, doc_ids=wanted)

    assert [hit.unit_no for hit in hits] == [2, 4, 6]


async def test_delete_by_units_and_by_documents(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    await index.upsert(
        kb_id,
        [VecRow(unit_no, unit(1, unit_no, 0, 0), f"doc-{unit_no % 2}") for unit_no in range(1, 9)],
    )

    await index.delete(kb_id, [1, 3])
    await index.delete_docs(kb_id, ["doc-0"])

    assert [unit_no async for unit_no in index.list_unit_nos(kb_id)] == [5, 7]
    assert await index.get(kb_id, 1) is None


async def test_get_many_returns_the_stored_blobs(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    blobs = {unit_no: unit(1, unit_no, unit_no, 0) for unit_no in range(1, 6)}
    await index.upsert(kb_id, [VecRow(unit_no, blob, "doc") for unit_no, blob in blobs.items()])

    found = await index.get_many(kb_id, [2, 4, 99])

    assert found == {2: blobs[2], 4: blobs[4]}
    assert unpack_vector(found[2]) == unpack_vector(blobs[2])


async def test_unit_numbers_are_listed_in_pages(
    index_db: IndexDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_db_module, "LIST_PAGE", 3)
    index, kb_id = await ready(index_db)
    await index.upsert(
        kb_id, [VecRow(unit_no, unit(1, 0, 0, 0), "doc") for unit_no in range(10, 0, -1)]
    )

    assert [unit_no async for unit_no in index.list_unit_nos(kb_id)] == list(range(1, 11))


async def test_wrong_inputs_are_rejected(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)

    with pytest.raises(ValueError):
        await index.upsert(kb_id, [VecRow(1, pack_vector([1.0, 0.0]), "doc")])
    with pytest.raises(ValueError):
        await index.search(kb_id, pack_vector([1.0, 0.0]), 1)
    with pytest.raises(ValueError):
        await index.search(kb_id, unit(1, 0, 0, 0), MAX_K + 1)
    with pytest.raises(LookupError):
        await index.upsert(uuid.uuid4(), [VecRow(1, unit(1, 0, 0, 0), "doc")])


async def test_drop_leaves_the_kb_without_vectors(index_db: IndexDb) -> None:
    index, kb_id = await ready(index_db)
    await index.upsert(kb_id, [VecRow(1, unit(1, 0, 0, 0), "doc")])

    await index.drop(kb_id)

    assert await index.search(kb_id, unit(1, 0, 0, 0), 1) == []
    assert await index.count(kb_id) == 0
    meta = await index.describe(kb_id)
    assert meta is not None and meta.vec_table is None and meta.dims is None


async def test_fake_embedder_finds_the_synthetic_answers(index_db: IndexDb, ko_eval: Any) -> None:
    # 설계 17.3-11: 가짜 임베더가 합성 평가셋에서 R@3 0.5 이상이면 e2e·memcheck가 검색 시험까지
    # 돈다. 지금 값은 R@3 0.906이다.
    config = EmbeddingConfig()
    index, kb_id = await ready(index_db, config)
    names = list(ko_eval.docs)
    await index.upsert(
        kb_id,
        [
            VecRow(position + 1, pack_vector(fake_embedding(config.document_input(text))), name)
            for position, (name, text) in enumerate(ko_eval.docs.items())
        ],
    )

    top3 = 0
    for question, expected, _ in ko_eval.questions:
        query = pack_vector(fake_embedding(config.query_input(question)))
        hits = await index.search(kb_id, query, 3)
        top3 += expected in [names[hit.unit_no - 1] for hit in hits]

    assert top3 / len(ko_eval.questions) >= 0.5
