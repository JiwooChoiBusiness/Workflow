"""작업 상태 기계(app/knowledge/ingest/job.py)를 가짜 포트로 본다(3단계 설계 4.4, 6.5)."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.knowledge.chunking import Chunk, GeneralRule
from app.knowledge.ingest.job import (
    ERR_QA_NO_PAIRS,
    SEGMENT_BATCH,
    WARN_EMPTY_TEXT,
    BatchOutcome,
    JobDocument,
    JobFailed,
    SplitRules,
    Stage,
    run_ingest,
)
from app.knowledge.ingest.parse import WARN_NO_TEXT_LAYER, WARN_TEXT_TRUNCATED, SourceText

RULES = SplitRules(doc_form="text_model", general=GeneralRule(max_tokens=40, overlap_tokens=0))


class Cancel:
    def __init__(self, after_batches: int | None = None) -> None:
        self.after_batches = after_batches
        self.flag = False

    def is_set(self) -> bool:
        return self.flag


@dataclass
class FakePort:
    status: str = "waiting"
    split_done: bool = False
    economy: bool = False
    rules: SplitRules = RULES
    source: SourceText | JobFailed = field(
        default_factory=lambda: SourceText(
            text="\n\n".join(f"문단 {i} 내용" * 8 for i in range(12)), kind="text"
        )
    )
    # index_batch가 돌려줄 결과. 다 쓰면 done이다.
    batches: list[BatchOutcome] = field(default_factory=list[BatchOutcome])
    cancel: Cancel | None = None
    # 이 단계로 바꾸려 하면 사용자가 그사이 멈춘 것처럼 거절한다.
    refuse: str | None = None
    events: list[str] = field(default_factory=list[str])
    written: list[int] = field(default_factory=list[int])
    extract: dict[str, Any] | None = None
    failure: JobFailed | None = None

    async def load(self) -> JobDocument | None:
        return JobDocument(self.status, self.split_done, self.economy, self.rules)

    async def advance(
        self,
        stage: Stage,
        *,
        expected: Collection[str],
        extract: dict[str, Any] | None = None,
        split_done: bool = False,
    ) -> bool:
        if stage == self.refuse or self.status not in expected:
            self.events.append(f"refused:{stage}")
            return False
        self.status = stage
        self.split_done = self.split_done or split_done
        if extract is not None:
            self.extract = extract
        self.events.append(stage + ("+split" if split_done else ""))
        return True

    async def clear_units(self) -> None:
        self.events.append("clear")

    async def read_source(self) -> SourceText:
        if isinstance(self.source, JobFailed):
            raise self.source
        return self.source

    async def write_chunks(self, chunks: Sequence[Chunk]) -> None:
        self.written.append(len(chunks))
        self.events.append("write")

    async def index_batch(self) -> BatchOutcome:
        self.events.append("batch")
        if self.cancel is not None and self.cancel.after_batches is not None:
            self.cancel.after_batches -= 1
            if self.cancel.after_batches <= 0:
                self.cancel.flag = True
        return self.batches.pop(0) if self.batches else BatchOutcome(done=True)

    async def fail(self, error: JobFailed) -> None:
        self.failure = error
        self.status = "error"
        self.events.append(f"fail:{error.code}")

    async def finish(self) -> None:
        self.events.append("finish")


async def test_a_new_document_goes_through_every_stage() -> None:
    port = FakePort(batches=[BatchOutcome(), BatchOutcome()])

    result = await run_ingest(port, Cancel())

    assert result == "completed"
    assert port.events == [
        "clear",
        "parsing",
        "cleaning",
        "splitting",
        "write",
        "indexing+split",
        "batch",
        "batch",
        "batch",
        "completed",
        "finish",
    ]
    assert port.status == "completed"
    assert port.extract is not None and port.extract["mode"] == "text"


async def test_segments_are_written_in_batches_of_200() -> None:
    text = "\n\n".join(f"짧은 문단 {index}" for index in range(SEGMENT_BATCH * 2 + 10))
    port = FakePort(
        source=SourceText(text=text, kind="text"),
        rules=SplitRules(
            doc_form="text_model", general=GeneralRule(max_tokens=8, overlap_tokens=0)
        ),
    )

    await run_ingest(port, Cancel())

    assert port.written == [SEGMENT_BATCH, SEGMENT_BATCH, 10]


async def test_a_split_document_resumes_at_indexing_without_parsing() -> None:
    # 재개·재기동·청크 편집 실패: 분할을 마친 문서는 pending 유닛만 잇는다.
    port = FakePort(status="waiting", split_done=True, batches=[BatchOutcome()])

    result = await run_ingest(port, Cancel())

    assert result == "completed"
    assert port.events == ["indexing", "batch", "batch", "completed", "finish"]


async def test_economy_goes_from_splitting_straight_to_completed() -> None:
    port = FakePort(economy=True)

    await run_ingest(port, Cancel())

    assert "indexing" not in port.events and "indexing+split" not in port.events
    assert port.events[-2:] == ["completed+split", "finish"]


async def test_cancel_stops_at_a_batch_boundary_and_leaves_the_status() -> None:
    cancel = Cancel(after_batches=2)
    port = FakePort(split_done=True, batches=[BatchOutcome()] * 5, cancel=cancel)

    result = await run_ingest(port, cancel)

    # 멈춤·종료·삭제 모두 상태를 건드리지 않고 나간다(멈춤은 요청이 이미 paused를 적었다).
    assert result == "stopped"
    assert port.events == ["indexing", "batch", "batch"]
    assert port.status == "indexing"


async def test_a_paused_document_is_not_brought_back() -> None:
    # 사용자가 그사이 멈췄으면 다음 단계로 넘기는 쓰기가 거절되고 작업은 조용히 나간다.
    port = FakePort(refuse="cleaning")

    result = await run_ingest(port, Cancel())

    assert result == "stopped"
    assert port.events[-1] == "refused:cleaning"
    assert port.failure is None


async def test_documents_that_are_not_waiting_are_skipped() -> None:
    for status in ("paused", "completed", "error"):
        port = FakePort(status=status)
        assert await run_ingest(port, Cancel()) == "skipped"
        assert port.events == []


async def test_failures_end_the_document_with_their_code() -> None:
    parse = FakePort(source=JobFailed("extract_timeout", "60초", retryable=True))
    daily = FakePort(
        split_done=True,
        batches=[
            BatchOutcome(),
            BatchOutcome(
                failure=JobFailed("rate_limited_daily", "한도", retryable=True, retry_after=60)
            ),
        ],
    )

    assert await run_ingest(parse, Cancel()) == "failed"
    assert await run_ingest(daily, Cancel()) == "failed"

    assert parse.failure is not None and parse.failure.retryable
    assert parse.events[-1] == "fail:extract_timeout"
    assert daily.failure is not None and daily.failure.retry_after == 60
    assert "completed" not in daily.events


async def test_a_scanned_pdf_completes_with_no_chunks_and_a_warning() -> None:
    port = FakePort(source=SourceText(text=" ", kind="pdf", pages=12))

    result = await run_ingest(port, Cancel())

    assert result == "completed"
    assert "write" not in port.events
    assert port.extract is not None and port.extract["warnings"] == [WARN_NO_TEXT_LAYER]
    assert port.extract["pages"] == 12


async def test_warnings_from_reading_and_splitting_are_kept() -> None:
    port = FakePort(source=SourceText(text="   ", kind="text", truncated=True))

    await run_ingest(port, Cancel())

    assert port.extract is not None
    assert port.extract["warnings"] == [WARN_TEXT_TRUNCATED, WARN_EMPTY_TEXT]


async def test_qa_without_pairs_is_an_error() -> None:
    port = FakePort(
        source=SourceText(text="그냥 글", kind="text"), rules=SplitRules(doc_form="qa_model")
    )

    assert await run_ingest(port, Cancel()) == "failed"
    assert port.failure is not None and port.failure.code == ERR_QA_NO_PAIRS


async def test_a_job_whose_settings_changed_leaves_without_completing() -> None:
    port = FakePort(split_done=True, batches=[BatchOutcome(), BatchOutcome(stopped=True)])

    assert await run_ingest(port, Cancel()) == "stopped"
    assert port.events == ["indexing", "batch", "batch"]
    assert port.status == "indexing" and port.failure is None


async def test_a_cancel_during_the_last_batch_does_not_complete() -> None:
    # 마지막 묶음 도중 멈춤이 오면 completed를 적지 않는다. 그사이 재개된 문서를 덮지 않게 한다.
    cancel = Cancel(after_batches=1)
    port = FakePort(split_done=True, cancel=cancel)

    assert await run_ingest(port, cancel) == "stopped"
    assert "completed" not in port.events
