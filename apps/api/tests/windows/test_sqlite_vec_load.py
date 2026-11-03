"""실제 sqlite-vec 휠(Windows x64, 0.1.9)로 index.db를 만들고 KNN·upsert를 돈다.

3단계 설계 5.2, M21b 검증. Windows 휠은 SIMD 없는 스칼라 빌드이고, 사용자 폴더처럼 한글이 든
경로에서 DLL을 불러야 한다. 기본 실행에서 빠지고 `uv run pytest -m windows`로 돈다.
"""

import math
import random
import shutil
import sqlite3
import sys
import uuid
from importlib.metadata import version
from pathlib import Path

import pytest

from app.knowledge.index_db import PAGE_SIZE, IndexDb, default_extension_path
from app.knowledge.units import VecRow
from app.knowledge.vector_index import SqliteVecIndex
from app.llm.embeddings import EmbeddingConfig, pack_vector

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows x64 휠"),
]

WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000005")


def unit_vector(seed: int, dimensions: int = 768) -> list[float]:
    rng = random.Random(seed)  # noqa: S311  시험용 벡터라 암호 강도가 필요 없다.
    values = [rng.gauss(0.0, 1.0) for _ in range(dimensions)]
    norm = math.sqrt(math.fsum(value * value for value in values))
    return [value / norm for value in values]


def dll_path() -> Path:
    return Path(default_extension_path()).with_suffix(".dll")


def test_the_pinned_wheel_ships_the_windows_dll() -> None:
    assert version("sqlite-vec") == "0.1.9"
    assert dll_path().is_file()


def test_plain_sqlite3_loads_the_extension(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "plain.db")
    try:
        conn.enable_load_extension(True)
        conn.load_extension(default_extension_path())
        conn.enable_load_extension(False)
        assert conn.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9"
    finally:
        conn.close()


async def test_index_db_runs_knn_and_upsert_with_the_real_wheel(tmp_path: Path) -> None:
    # DLL을 한글이 든 폴더로 옮겨 불러 본다(사용자 폴더 이름이 한글인 PC).
    folder = tmp_path / "한글 경로"
    folder.mkdir()
    shutil.copy2(dll_path(), folder / "vec0.dll")
    path = tmp_path / "knowledge" / "index.db"
    db = IndexDb(path, extension_path=lambda: str(folder / "vec0.dll"))
    kb_id = uuid.uuid4()
    try:
        assert await db.open(), db.unavailable_reason
        index = SqliteVecIndex(db)
        assert await index.ensure(kb_id, EmbeddingConfig(), workspace_id=WORKSPACE)
        rows = [
            VecRow(unit_no, pack_vector(unit_vector(unit_no)), f"doc-{unit_no % 5}")
            for unit_no in range(1, 501)
        ]
        await index.upsert(kb_id, rows)
        # 같은 rowid를 다시 넣으면 한 트랜잭션의 DELETE+INSERT로 바뀐다.
        await index.upsert(kb_id, [VecRow(1, pack_vector(unit_vector(9999)), "doc-new")])

        hits = await index.search(kb_id, pack_vector(unit_vector(9999)), 3)
        filtered = await index.search(kb_id, pack_vector(unit_vector(42)), 5, doc_ids=["doc-2"])

        assert hits[0].unit_no == 1 and hits[0].cosine == pytest.approx(1.0, abs=1e-5)
        assert await index.count(kb_id) == 500
        assert filtered[0].unit_no == 42
        assert all(hit.unit_no % 5 == 2 for hit in filtered)
    finally:
        await db.close()

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA page_size").fetchone()[0] == PAGE_SIZE
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # 확장을 불러오지 않은 연결도 파일을 연다. vec0 테이블 조회만 실패한다(설계 5.1).
        assert raw.execute("SELECT count(*) FROM kb_index").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="no such module"):
            raw.execute(f"SELECT count(*) FROM vec_{kb_id.hex}")  # noqa: S608
    finally:
        raw.close()
