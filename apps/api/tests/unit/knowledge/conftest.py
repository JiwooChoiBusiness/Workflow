"""지식 라이브러리 시험의 공용 픽스처. --import-mode=importlib라 테스트 모듈이 conftest를
import할 수 없어 도우미는 픽스처로 넘긴다."""

import asyncio
import csv
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.index_db import IndexDb, contentless_supported

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge"


async def open_index_db(path: Path, *, fts_contentless: bool | None = None) -> IndexDb:
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        pytest.skip("이 파이썬의 sqlite3는 확장 로드를 지원하지 않는다")
    db = IndexDb(path, fts_contentless=fts_contentless)
    assert await db.open(), db.unavailable_reason
    return db


@pytest.fixture
async def index_db(tmp_path: Path) -> AsyncIterator[IndexDb]:
    db = await open_index_db(tmp_path / "knowledge" / "index.db")
    yield db
    await db.close()


MakeIndexDb = Callable[..., Awaitable[IndexDb]]


@pytest.fixture
async def make_index_db(tmp_path: Path) -> AsyncIterator[MakeIndexDb]:
    """옵션을 바꿔 여는 IndexDb. expect_open=False면 열기 실패를 그대로 돌려준다.

    시험이 끝나면 연 것을 모두 닫는다."""
    opened: list[IndexDb] = []

    async def make(name: str = "index.db", *, expect_open: bool = True, **options: Any) -> IndexDb:
        db = IndexDb(tmp_path / name, **options)
        opened.append(db)
        available = await db.open()
        if expect_open:
            assert available, db.unavailable_reason
        return db

    yield make
    for db in opened:
        await db.close()


@pytest.fixture(params=[True, False], ids=["contentless", "stored"])
async def fts_db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[IndexDb]:
    # 전문 색인 시험은 contentless(3.43 이상)와 본문 저장(폴백) 두 모드를 모두 돈다(설계 5.3).
    contentless: bool = request.param
    if contentless and not contentless_supported():
        pytest.skip(f"SQLite {sqlite3.sqlite_version}에서는 contentless 전문 색인을 만들 수 없다")
    db = await open_index_db(tmp_path / "index.db", fts_contentless=contentless)
    assert db.fts_contentless is contentless
    yield db
    await db.close()


@dataclass
class FakeTime:
    """가짜 시계. sleep은 시계를 그만큼 옮기고 한 번 양보한다."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list[float])

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@dataclass(frozen=True)
class EvalSet:
    docs: dict[str, str]
    # (질문, 정답 문서 이름, 종류). 종류는 sem(말을 바꾼 질문)·kw(고유 키워드)·nsp(띄어쓰기 없음).
    questions: list[tuple[str, str, str]]


@pytest.fixture(scope="session")
def ko_eval() -> EvalSet:
    with (FIXTURES / "ko_eval_docs.csv").open(encoding="utf-8", newline="") as file:
        docs = {row["name"]: row["text"] for row in csv.DictReader(file)}
    with (FIXTURES / "ko_eval.csv").open(encoding="utf-8", newline="") as file:
        questions = [
            (row["question"], row["expected_document_name"], row["note"])
            for row in csv.DictReader(file)
        ]
    return EvalSet(docs, questions)
