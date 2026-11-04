"""platform knowledge rebuild-index(3단계 설계 5.5, 확장).

서버를 끈 상태에서 전문 색인만 먼저 다시 만든다. 벡터는 서버가 뜰 때 기동 정합 검사가 벡터 테이블이
없는 KB를 찾아 문서를 다시 넣어 채운다(임베딩 호출이 든다). 레인은 묶음마다 전문 색인과 벡터를 같이
쓰므로 그대로 두면 전문 검색도 임베딩 속도(분당 80건)로만 돌아온다. 그래서 전문 색인은 여기서 한
번에 만든다.

새 색인은 옆 파일(index.db.rebuild)에 끝까지 만든 뒤 바꿔 끼운다. 만들다 실패하면(sqlite-vec를
불러오지 못함, 디스크, 중간 오류) 원래 index.db를 그대로 둔다. 옛 파일을 열 수 있으면 오늘 임베딩
사용량(하루 몫, meta)을 새 파일로 옮긴다.
"""

import asyncio
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.engine import Database
from app.db.models import KnowledgeBase
from app.db.models.enums import KbDocForm
from app.db.repositories.knowledge import (
    KbChildChunkRepository,
    KbDocumentRepository,
    KbSegmentRepository,
    KnowledgeBaseRepository,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.fts_index import SqliteFtsIndex
from app.knowledge.index_db import IndexDb, IndexUnavailable, set_meta
from app.knowledge.units import FtsRow
from app.llm.embeddings import DAILY_META_PREFIX
from app.services.knowledge_index_executor import (
    UNIT_PAGE,
    PendingUnit,
    unit_of_child,
    unit_of_segment,
)
from app.services.knowledge_runtime import embedding_config_of, index_path

REBUILD_SUFFIX = ".rebuild"
BUSY_TIMEOUT_SEC = 5.0
# kept: 옛 파일의 오늘 사용량을 옮겼다. reset: 옛 파일을 읽지 못해 그날 한도를 0부터 센다.
# none: 옮길 것이 없었다.
DailyUsage = Literal["kept", "reset", "none"]


class RebuildError(Exception):
    pass


@dataclass(frozen=True)
class RebuildResult:
    kbs: int
    # 전문 색인에 다시 넣은 켜진 유닛.
    units: int
    # 서버가 뜬 뒤 다시 임베딩할 유닛(High Quality KB의 켜진 유닛).
    vector_units: int
    daily_usage: DailyUsage = "none"


def with_sidecars(path: Path) -> list[Path]:
    return [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]


def index_files(settings: Settings) -> list[Path]:
    return with_sidecars(index_path(settings))


async def rebuild_index(settings: Settings) -> RebuildResult:
    """지우지 않은 KB의 켜진 유닛으로 전문 색인을 새로 만들어 index.db와 바꾼다."""
    app_db = settings.data_dir / "app.db"
    if not app_db.is_file():
        raise RebuildError(f"{app_db}가 없다. DATA_DIR이 맞는지 확인한다")
    target = index_path(settings)
    temp = target.with_name(target.name + REBUILD_SUFFIX)
    _remove(with_sidecars(temp), "지난번에 남은 임시 색인 파일을 지우지 못했다")
    carried = await asyncio.to_thread(_settle_old, target)
    db = Database(app_db)
    index_db = IndexDb(temp, cache_kb=settings.kb_index_cache_kb)
    try:
        try:
            result = await _build(db, index_db, carried or {})
        finally:
            await index_db.close()
            await db.close()
    except BaseException as exc:
        _remove_quietly(with_sidecars(temp))
        if isinstance(exc, (OSError, sqlite3.Error, SQLAlchemyError, IndexUnavailable)):
            raise RebuildError(
                f"새 색인을 만들지 못했다: {exc}. 원래 index.db는 그대로 두었다"
            ) from exc
        raise
    # 옛 -wal은 위에서 체크포인트해 비었다. 새 본 파일 옆에 옛 -wal이 남으면 SQLite가 그것을 새
    # 파일에 덮어 쓰므로 바꾸기 전에 지운다.
    try:
        _remove(with_sidecars(target)[1:], "")
        os.replace(temp, target)
    except (OSError, RebuildError) as exc:
        _remove_quietly(with_sidecars(temp))
        raise RebuildError(
            f"index.db를 바꾸지 못했다: {exc}. 다른 프로세스(다른 포트의 서버 등)가 파일을"
            " 쥐고 있는지 본다. 원래 index.db는 그대로 두었다"
        ) from exc
    _remove_quietly(with_sidecars(temp)[1:])
    daily: DailyUsage = "reset" if carried is None else "kept" if carried else "none"
    return RebuildResult(result.kbs, result.units, result.vector_units, daily)


async def _build(db: Database, index_db: IndexDb, carried: dict[str, str]) -> RebuildResult:
    if not await index_db.open():
        raise RebuildError(
            f"새 색인 파일을 열지 못했다: {index_db.unavailable_reason}. 원래 index.db는 그대로"
            " 두었다"
        )
    fts = SqliteFtsIndex(index_db)
    async with db.read() as session:
        workspace_ids = [row.id for row in await WorkspaceRepository(session).list_all()]
    kbs = units = vector_units = 0
    for workspace_id in workspace_ids:
        async with db.read() as session:
            found = await KnowledgeBaseRepository(session, workspace_id).list_active()
        for kb in found:
            written = await _rebuild_kb(db, fts, workspace_id, kb)
            await index_db.refresh_unit_count(kb.id)
            await fts.refresh_stop_bigrams(kb.id)
            kbs += 1
            units += written
            if embedding_config_of(kb) is not None:
                vector_units += written
    for key, value in carried.items():
        await set_meta(index_db, key, value)
    return RebuildResult(kbs=kbs, units=units, vector_units=vector_units)


def _settle_old(path: Path) -> dict[str, str] | None:
    """옛 index.db의 하루 사용량(meta)을 읽고 WAL을 본 파일로 합친다. 읽지 못하면 None.

    sqlite-vec 없이 여는 연결이라 확장을 불러오지 못하는 PC에서도 된다(vec0 테이블은 건드리지
    않는다).
    """
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SEC)
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {
        str(key): str(value)
        for key, value in rows
        if str(key).startswith(DAILY_META_PREFIX) and value is not None
    }


def _remove(paths: list[Path], message: str) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise RebuildError(f"{message}: {exc}" if message else str(exc)) from exc


def _remove_quietly(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


async def _rebuild_kb(
    db: Database, fts: SqliteFtsIndex, workspace_id: uuid.UUID, kb: KnowledgeBase
) -> int:
    await fts.ensure(kb.id, workspace_id=workspace_id)
    async with db.read() as session:
        documents = await KbDocumentRepository(session, workspace_id).all_live(kb.id)
    written = 0
    for document in documents:
        cursor = 0
        while True:
            page = await _page(db, workspace_id, kb, document.id, cursor)
            if not page:
                break
            cursor = page[-1].unit.unit_no
            rows = [FtsRow(item.unit.unit_no, item.unit.body) for item in page if item.enabled]
            await fts.upsert(kb.id, rows)
            written += len(rows)
    return written


async def _page(
    db: Database, workspace_id: uuid.UUID, kb: KnowledgeBase, document_id: uuid.UUID, after: int
) -> list[PendingUnit]:
    async with db.read() as session:
        if kb.doc_form == KbDocForm.HIERARCHICAL:
            rows = await KbChildChunkRepository(session, workspace_id).units_after(
                document_id, after, UNIT_PAGE
            )
            return [unit_of_child(child, enabled) for child, enabled in rows]
        segments = await KbSegmentRepository(session, workspace_id).units_after(
            document_id, after, UNIT_PAGE
        )
    return [unit for unit in map(unit_of_segment, segments) if unit is not None]
