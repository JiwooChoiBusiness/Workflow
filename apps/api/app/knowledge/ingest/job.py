"""문서 하나의 색인 작업 상태 기계(3단계 설계 4.4, 6.1, 6.5). DB·색인·샌드박스는 IngestPort로 본다.

waiting → parsing → cleaning → splitting → indexing → completed. Economical은 splitting 뒤 바로
completed다. 분할을 마친 적이 없는 문서(splitting_completed_at 없음)는 남은 부분 결과를 지우고
처음부터, 마친 문서는 indexing부터 pending 유닛만 이어서 한다(재기동·재개·청크 편집 실패가 같은
길이다).

단계마다, 그리고 세그먼트 묶음·임베딩 묶음 경계마다 취소를 본다. 취소 사유(pause·shutdown·delete)와
관계없이 작업은 상태를 건드리지 않고 나간다. pause는 요청이 이미 paused를 적었고, shutdown은 다음
기동의 복구가, delete는 삭제 작업이 뒤를 맡는다. 상태 전이는 모두 "기대한 상태일 때만" 쓰므로 그사이
사용자가 멈추거나 지운 문서를 작업이 되살리지 않는다.
"""

import asyncio
import itertools
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.knowledge.chunking import (
    Chunk,
    ChunkResult,
    DocForm,
    GeneralRule,
    ParentChildRule,
    QaPair,
    parse_qa_csv,
    qa_chunks,
    split_general,
    split_parent_child,
)
from app.knowledge.cleaning import CleaningRules, clean_text, normalize
from app.knowledge.ingest.parse import WARN_NO_TEXT_LAYER, WARN_TEXT_TRUNCATED, SourceText

Stage = Literal["parsing", "cleaning", "splitting", "indexing", "completed"]
JobResult = Literal["completed", "failed", "stopped", "skipped"]

WAITING = "waiting"
RUNNING: tuple[str, ...] = ("parsing", "cleaning", "splitting", "indexing")
# 분할 결과를 app.db에 쓰는 묶음(세그먼트 기준). 쓰기 잠금을 짧게 잡으려는 크기다(13장).
SEGMENT_BATCH = 200
WARN_EMPTY_TEXT = "empty_text"
ERR_QA_NO_PAIRS = "qa_no_pairs"


@dataclass(frozen=True)
class SplitRules:
    doc_form: DocForm
    cleaning: CleaningRules = field(default_factory=CleaningRules)
    general: GeneralRule = field(default_factory=GeneralRule)
    parent_child: ParentChildRule = field(default_factory=ParentChildRule)
    # 임베딩 모델의 입력 상한(토큰). 색인만 하는 Economical이면 None.
    input_limit: int | None = None


@dataclass(frozen=True)
class JobDocument:
    status: str
    split_done: bool
    economy: bool
    rules: SplitRules


class JobFailed(Exception):
    """문서를 error로 끝낼 실패. retry_after는 다시 시도해도 되는 때까지 남은 초(일일 한도)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class BatchOutcome:
    # 더 볼 유닛이 없으면 done. failure면 문서를 error로 끝낸다(앞서 쓴 벡터는 남는다).
    done: bool = False
    failure: JobFailed | None = None
    # 작업을 시작한 뒤 KB 색인 설정이 바뀌었다. 상태를 그대로 두고 나가 새 작업이 새 설정으로 잇게
    # 한다(옛 설정의 벡터를 새 테이블에 쓰지 않게).
    stopped: bool = False


class IngestPort(Protocol):
    async def load(self) -> JobDocument | None: ...

    async def advance(
        self,
        stage: Stage,
        *,
        expected: Collection[str],
        extract: dict[str, Any] | None = None,
        split_done: bool = False,
    ) -> bool: ...

    async def clear_units(self) -> None: ...

    async def read_source(self) -> SourceText: ...

    async def write_chunks(self, chunks: Sequence[Chunk]) -> None: ...

    async def index_batch(self) -> BatchOutcome: ...

    async def fail(self, error: JobFailed) -> None: ...

    async def finish(self) -> None: ...


class CancelLike(Protocol):
    def is_set(self) -> bool: ...


def split_text(text: str, rules: SplitRules) -> ChunkResult:
    """정리한 글을 청크 모드대로 나눈다. Q&A는 CSV의 (질문, 답) 쌍이다."""
    if rules.doc_form == "qa_model":
        return qa_chunks(parse_qa_csv(text))
    if rules.doc_form == "hierarchical_model":
        return split_parent_child(text, rules.parent_child, input_limit=rules.input_limit)
    return ChunkResult(split_general(text, rules.general, input_limit=rules.input_limit))


def prepare_chunks(text: str, rules: SplitRules) -> tuple[ChunkResult, list[str]]:
    """정리와 분할. 미리보기와 작업이 같이 쓴다(이벤트 루프 밖에서 부른다)."""
    warnings: list[str] = []
    if rules.doc_form == "qa_model":
        # CSV는 통째로 정리하면 칸 안 줄바꿈이 흐트러진다. 항상 하는 정규화만 하고 칸마다 정리한다.
        cleaning = rules.cleaning
        pairs = [
            QaPair(clean_text(pair.question, cleaning), clean_text(pair.answer, cleaning))
            for pair in parse_qa_csv(normalize(text))
        ]
        result = qa_chunks(pairs)
    else:
        cleaned_text = clean_text(text, rules.cleaning)
        if not cleaned_text:
            warnings.append(WARN_EMPTY_TEXT)
        result = split_text(cleaned_text, rules)
    warnings.extend(warning for warning in result.warnings if warning not in warnings)
    return result, warnings


def _stopped(cancel: CancelLike) -> bool:
    return cancel.is_set()


async def run_ingest(port: IngestPort, cancel: CancelLike) -> JobResult:
    document = await port.load()
    if document is None or document.status not in (WAITING, *RUNNING):
        # 그사이 멈췄거나(paused) 끝났거나 지웠다. 큐에 남아 있던 옛 항목이다.
        return "skipped"
    try:
        if not document.split_done:
            outcome = await _parse_and_split(port, document, cancel)
            if outcome is not None:
                return outcome
        elif not document.economy and not await port.advance(
            "indexing", expected=(WAITING, *RUNNING)
        ):
            return "stopped"
        while True:
            if _stopped(cancel):
                return "stopped"
            batch = await port.index_batch()
            if batch.stopped:
                return "stopped"
            if batch.failure is not None:
                await port.fail(batch.failure)
                return "failed"
            if batch.done:
                break
        # 마지막 묶음 도중 멈춤·삭제 요청이 왔으면 completed를 적지 않는다. 그사이 다시 시작된
        # 문서(재개, 원본 바꾸기)를 옛 작업이 끝난 것으로 덮지 않게 한다.
        if _stopped(cancel):
            return "stopped"
        if not await port.advance("completed", expected=(WAITING, "splitting", "indexing")):
            return "stopped"
        await port.finish()
        return "completed"
    except JobFailed as exc:
        await port.fail(exc)
        return "failed"


async def _parse_and_split(
    port: IngestPort, document: JobDocument, cancel: CancelLike
) -> JobResult | None:
    """분할까지 한다. 작업을 여기서 끝내야 하면 그 결과를, 이어서 색인하면 None을 돌려준다."""
    await port.clear_units()
    if not await port.advance("parsing", expected=(WAITING, *RUNNING)):
        return "stopped"
    source = await port.read_source()
    if _stopped(cancel):
        return "stopped"
    warnings = [WARN_TEXT_TRUNCATED] if source.truncated else []
    if source.no_text_layer:
        # 스캔 PDF는 오류가 아니라 청크 0개로 끝내고 "모델로 읽기"를 안내한다(6.2).
        warnings.append(WARN_NO_TEXT_LAYER)
        if not await port.advance(
            "completed",
            expected=("parsing",),
            extract=source.extract_info(warnings),
            split_done=True,
        ):
            return "stopped"
        await port.finish()
        return "completed"
    info = source.extract_info(warnings)
    if not await port.advance("cleaning", expected=("parsing",), extract=info):
        return "stopped"
    if _stopped(cancel):
        return "stopped"
    # 정리·분할은 8MB 글에서 수 초가 걸려 이벤트 루프 밖에서 한다(6.3).
    result, split_warnings = await asyncio.to_thread(prepare_chunks, source.text, document.rules)
    del source
    if document.rules.doc_form == "qa_model" and not result.chunks:
        raise JobFailed(
            ERR_QA_NO_PAIRS,
            "Q&A 문서에서 question·answer 쌍을 찾지 못했다. 머리글이 있는 두 열 CSV로 올린다",
        )
    if not await port.advance("splitting", expected=("cleaning",)):
        return "stopped"
    for batch in itertools.batched(result.chunks, SEGMENT_BATCH):
        if _stopped(cancel):
            return "stopped"
        await port.write_chunks(batch)
    known: list[str] = list(info["warnings"])
    info["warnings"] = [*known, *(item for item in split_warnings if item not in known)]
    stage: Stage = "completed" if document.economy else "indexing"
    if stage == "completed":
        # Economical은 분할하며 전문 색인까지 썼다. 남은 pending(없어야 한다)만 보고 끝낸다.
        while not (batch_outcome := await port.index_batch()).done:
            if batch_outcome.stopped or _stopped(cancel):
                return "stopped"
            if batch_outcome.failure is not None:
                raise batch_outcome.failure
    if not await port.advance(stage, expected=("splitting",), extract=info, split_done=True):
        return "stopped"
    if stage == "completed":
        await port.finish()
        return "completed"
    return None
