"""platform doctor의 지식 색인 항목(3단계 설계 5.4, 5.5).

app.db와 index.db를 표준 sqlite3로 읽기만 한다(backup.py와 같다). 서버가 떠 있어도 돌릴 수 있다.
고아와 설정 불일치는 알리기만 하고 고치지 않는다. 서버의 기동 정합 검사와 매시 정리가 고친다.
"""

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.knowledge.index_db import CONTENTLESS_MIN_SQLITE, SCHEMA_VERSION, contentless_supported
from app.llm.embeddings import FREE_EMBED_RPM_CAP, EmbeddingConfig
from app.sandbox import disk
from app.sysinfo import Check

INDEX_PARTS = ("knowledge", "index.db")
KB_WORK = "kb-work"
BUSY_TIMEOUT_SEC = 5.0
MB = 1024 * 1024


@dataclass(frozen=True)
class _Kb:
    id: str
    name: str
    hierarchical: bool
    config: EmbeddingConfig | None


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SEC)
    try:
        conn.execute("PRAGMA query_only=1")
        yield conn
    finally:
        conn.close()


def _config(raw: str | None, technique: str) -> EmbeddingConfig | None:
    if technique != "high_quality" or not raw:
        return None
    try:
        return EmbeddingConfig.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None


def _app_kbs(app_db: Path) -> list[_Kb]:
    with _connect(app_db) as conn:
        rows = conn.execute(
            "SELECT id, name, doc_form, indexing_technique, embedding FROM knowledge_base "
            "WHERE deleted_at IS NULL ORDER BY created_at"
        ).fetchall()
    return [
        _Kb(str(row[0]), str(row[1]), row[2] == "hierarchical_model", _config(row[4], row[3]))
        for row in rows
    ]


@dataclass(frozen=True)
class _Units:
    # 지우지 않은 문서의 켜진 유닛. 다시 색인할 양과 설정 불일치는 이것으로 센다.
    alive: set[int]
    # 지우는 중인 문서까지 센 켜진 유닛. 그 문서의 색인 행은 삭제 작업이 지우므로 고아가 아니다
    # (서버의 매시 고아 정리와 같은 기준).
    known: set[int]


def _enabled_units(conn: sqlite3.Connection, kb: _Kb) -> _Units:
    # 끈 청크(자식은 부모가 꺼진 것)는 색인 행이 없어야 해서 빠진다.
    if kb.hierarchical:
        sql = (
            "SELECT c.unit_no, d.deleted_at IS NULL FROM kb_child_chunk c "
            "JOIN kb_segment s ON s.id = c.segment_id JOIN kb_document d ON d.id = c.document_id "
            "WHERE c.kb_id = ? AND s.enabled = 1"
        )
    else:
        sql = (
            "SELECT s.unit_no, d.deleted_at IS NULL FROM kb_segment s "
            "JOIN kb_document d ON d.id = s.document_id "
            "WHERE s.kb_id = ? AND s.unit_no IS NOT NULL AND s.enabled = 1"
        )
    alive: set[int] = set()
    known: set[int] = set()
    for unit_no, live in conn.execute(sql, (kb.id,)):
        known.add(int(unit_no))
        if live:
            alive.add(int(unit_no))
    return _Units(alive=alive, known=known)


def _rowids(conn: sqlite3.Connection, table: str | None) -> set[int] | None:
    if not table:
        return None
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return {int(row[0]) for row in conn.execute(f"SELECT rowid FROM {table}")}  # noqa: S608


def _hex(kb_id: str) -> str:
    return kb_id.replace("-", "").lower()


def _rebuild_estimate(settings: Settings, units: int) -> str:
    # 미리보기의 예상 소요와 같은 식(분당 rpm − reserve, 17.4 M22)에 무료 키 하루 한도를 더한다.
    per_minute = max(1, min(settings.kb_embed_rpm, FREE_EMBED_RPM_CAP) - settings.kb_embed_reserve)
    text = f"유닛 {units:,}개, 분당 {per_minute}건이면 약 {math.ceil(units / per_minute):,}분"
    if settings.kb_embed_rpd and units > settings.kb_embed_rpd:
        days = math.ceil(units / settings.kb_embed_rpd)
        text += f", 무료 키 하루 {settings.kb_embed_rpd:,}건 한도로는 {days}일"
    return text


def knowledge_checks(settings: Settings) -> list[Check]:
    app_db = settings.data_dir / "app.db"
    index = settings.data_dir.joinpath(*INDEX_PARTS)
    checks = [_work_dir_check(settings.data_dir / KB_WORK)]
    if not app_db.is_file():
        return [Check("지식 색인", True, "app.db가 없어 보지 않았다"), *checks]
    try:
        kbs = _app_kbs(app_db)
    except sqlite3.Error as exc:
        # 0007 전 스키마면 서버가 뜰 때 올린다.
        return [Check("지식 색인", True, f"지식 베이스 표를 읽지 못했다: {exc}"), *checks]
    try:
        with _connect(app_db) as conn:
            units = {kb.id: _enabled_units(conn, kb) for kb in kbs}
    except sqlite3.Error as exc:
        return [Check("지식 색인", False, f"app.db의 청크를 읽지 못했다: {exc}"), *checks]
    total = sum(len(found.alive) for found in units.values())
    if not index.is_file():
        if total:
            detail = (
                f"{index}가 없다. 서버가 뜨면 모든 문서를 다시 색인한다"
                f"({_rebuild_estimate(settings, total)})"
            )
            return [Check("지식 색인", False, detail), *checks]
        return [Check("지식 색인", True, f"{index} 아직 없음. 서버가 처음 뜰 때 만든다"), *checks]
    return [*_index_checks(settings, index, kbs, units), *checks]


def _index_checks(
    settings: Settings, index: Path, kbs: list[_Kb], units: dict[str, _Units]
) -> list[Check]:
    size = index.stat().st_size
    wal = index.with_name(index.name + "-wal")
    if wal.is_file():
        size += wal.stat().st_size
    try:
        import sqlite_vec

        extension = sqlite_vec.loadable_path()
    except (ImportError, AttributeError) as exc:
        return [Check("지식 색인", False, f"sqlite-vec를 찾지 못했다: {exc}. 지식 기능만 503이다")]
    try:
        conn = sqlite3.connect(index, timeout=BUSY_TIMEOUT_SEC)
    except sqlite3.Error as exc:
        return [Check("지식 색인", False, f"{index}를 열지 못했다: {exc}")]
    try:
        try:
            conn.enable_load_extension(True)
            conn.load_extension(extension)
            conn.enable_load_extension(False)
            vec_version = str(conn.execute("SELECT vec_version()").fetchone()[0])
        except (sqlite3.Error, AttributeError) as exc:
            return [
                Check(
                    "지식 색인", False, f"sqlite-vec를 불러오지 못했다: {exc}. 지식 기능만 503이다"
                )
            ]
        conn.execute("PRAGMA query_only=1")
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        stored = int(meta.get("schema_version") or 0)
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
        opened = Check(
            "지식 색인",
            stored <= SCHEMA_VERSION,
            f"{index} {size / MB:.1f}MB, 스키마 {stored}, 페이지 {page // 1024}KB,"
            f" sqlite-vec {vec_version}"
            + (
                ""
                if stored <= SCHEMA_VERSION
                else f". 코드의 스키마 {SCHEMA_VERSION}보다 높아 지식 기능을 끈다"
            ),
        )
        rows = conn.execute(
            "SELECT kb_id, vec_table, fts_table, dims, config_key, embedding_version FROM kb_index"
        ).fetchall()
        mismatch, orphans = _compare(conn, settings, kbs, units, {str(row[0]): row for row in rows})
    except sqlite3.Error as exc:
        return [Check("지식 색인", False, f"{index}를 읽지 못했다: {exc}")]
    finally:
        conn.close()
    return [opened, _sqlite_check(meta), mismatch, orphans]


def _sqlite_check(meta: dict[str, Any]) -> Check:
    version = sqlite3.sqlite_version
    mode = "contentless" if contentless_supported() else "본문 저장(3.43 미만 폴백)"
    detail = f"SQLite {version}, 전문 색인 {mode}"
    if meta.get("fts_contentless") == "1" and not contentless_supported():
        need = ".".join(str(part) for part in CONTENTLESS_MIN_SQLITE)
        return Check(
            "SQLite·전문 색인",
            False,
            f"{detail}. 이 index.db의 전문 색인은 SQLite {need} 이상에서 만든 contentless라"
            " 고치거나 지울 수 없다. platform knowledge rebuild-index로 다시 만든다",
        )
    return Check("SQLite·전문 색인", True, detail)


def _compare(
    conn: sqlite3.Connection,
    settings: Settings,
    kbs: list[_Kb],
    units: dict[str, _Units],
    indexed: dict[str, Any],
) -> tuple[Check, Check]:
    known = {kb.id for kb in kbs}
    mismatched: list[str] = []
    missing: list[str] = []
    stale_units = 0
    vec_orphans = fts_orphans = 0
    for kb in kbs:
        wanted = units[kb.id].alive
        everything = units[kb.id].known
        row = indexed.get(kb.id)
        if row is None:
            if wanted:
                missing.append(kb.name)
                stale_units += len(wanted)
            continue
        fts = _rowids(conn, row[2])
        if fts is None and wanted:
            missing.append(kb.name)
            stale_units += len(wanted)
        elif fts is not None:
            fts_orphans += len(fts - everything)
        vec = _rowids(conn, row[1])
        config = kb.config
        if config is not None and wanted:
            same = (
                row[1] == f"vec_{_hex(kb.id)}"
                and vec is not None
                and row[4] == config.key()
                and row[5] == config.version
                and row[3] == config.dimensions
            )
            if not same:
                mismatched.append(kb.name)
                stale_units += len(wanted)
        if vec is not None:
            vec_orphans += len(vec - everything)
    foreign = [kb_id for kb_id in indexed if kb_id not in known]
    problems = mismatched + missing
    if problems:
        names = ", ".join(problems[:5]) + (" 등" if len(problems) > 5 else "")
        setting = Check(
            "색인 설정",
            False,
            f"지식 베이스 {len(kbs)}개 중 설정 불일치 {len(mismatched)}개,"
            f" 색인 없음 {len(missing)}개({names}). 서버가 뜨면 다시 만든다"
            f"({_rebuild_estimate(settings, stale_units)})",
        )
    else:
        setting = Check("색인 설정", True, f"지식 베이스 {len(kbs)}개, app.db와 index.db 설정 같음")
    orphan_detail = f"벡터 {vec_orphans:,}행, 전문 {fts_orphans:,}행, 모르는 KB {len(foreign)}개"
    if vec_orphans or fts_orphans or foreign:
        orphans = Check("색인 고아", False, f"{orphan_detail}. 매시 정리가 지운다")
    else:
        orphans = Check("색인 고아", True, "없음")
    return setting, orphans


def _work_dir_check(root: Path) -> Check:
    folders = [path for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    usage = disk.measure(root)
    detail = f"{root} 폴더 {len(folders)}개, {disk.format_size(usage.total_bytes)}"
    # 작업이 끝나면 바로 지우고 남은 것은 매시 정리가 1시간 뒤 지운다(4.5).
    return Check("지식 작업 폴더", True, detail)
