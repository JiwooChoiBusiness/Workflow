"""스캔 PDF 모델 읽기의 조각 나누기(3단계 설계 6.2). 조각은 20쪽이나 12MB 중 먼저 닿는 쪽까지이고,
응답이 max_tokens로 끝나면 반으로 나눠 다시 보낸다(1쪽까지)."""

from collections.abc import Sequence

import pytest

from app.knowledge.ingest.scanned_pdf import (
    PIECE_MAX_BYTES,
    PIECE_MAX_PAGES,
    WARN_MODEL_READ_TRUNCATED,
    PieceFile,
    ScannedPdfError,
    collect_text,
    initial_ranges,
    model_piece_reader,
    read_with_model,
)
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.llm.types import ErrorEvent, ImageBlock

PAGE_BYTES = 100_000


class Store:
    """쪽마다 크기를 정해 둔 가짜 조각 저장소. 조각 데이터는 b"s-e"(쪽 범위)다."""

    def __init__(self, page_bytes: Sequence[int]) -> None:
        self.page_bytes = list(page_bytes)
        self.writes: list[list[tuple[int, int]]] = []
        self.reads: list[tuple[int, int]] = []

    async def write(self, ranges: Sequence[tuple[int, int]]) -> list[PieceFile]:
        self.writes.append(list(ranges))
        return [
            PieceFile(start, end, sum(self.page_bytes[start:end]), f"{start}-{end}")
            for start, end in ranges
        ]

    async def read(self, piece: PieceFile) -> bytes:
        self.reads.append((piece.start, piece.end))
        return piece.name.encode()


def reader_cutting_above(pages: int) -> tuple[list[str], object]:
    seen: list[str] = []

    async def read(data: bytes) -> tuple[str, bool]:
        start, end = (int(part) for part in data.decode().split("-"))
        seen.append(data.decode())
        return " ".join(f"{page + 1}쪽" for page in range(start, end)), end - start > pages

    return seen, read


def test_pieces_start_at_twenty_pages() -> None:
    assert initial_ranges(45) == [(0, 20), (20, 40), (40, 45)]
    assert initial_ranges(0) == []
    assert PIECE_MAX_PAGES == 20 and PIECE_MAX_BYTES == 12 * 1024 * 1024


async def test_pages_are_read_in_order_one_piece_at_a_time() -> None:
    store = Store([PAGE_BYTES] * 45)
    seen, reader = reader_cutting_above(100)

    result = await read_with_model(45, store, reader)  # type: ignore[arg-type]

    assert seen == ["0-20", "20-40", "40-45"]
    assert store.reads == [(0, 20), (20, 40), (40, 45)]
    assert result.text.split("\n\n")[0].startswith("1쪽 2쪽")
    assert result.text.endswith("45쪽")
    assert result.pieces == 3 and result.warnings == []


async def test_max_tokens_halves_the_piece_down_to_single_pages() -> None:
    store = Store([PAGE_BYTES] * 20)
    # 6쪽보다 큰 조각은 출력 상한에서 잘린다.
    seen, reader = reader_cutting_above(6)

    result = await read_with_model(20, store, reader)  # type: ignore[arg-type]

    assert seen[0] == "0-20"
    # 20 → 10 → 5쪽으로 나눠 다시 보낸다.
    assert seen[1:] == ["0-10", "0-5", "5-10", "10-20", "10-15", "15-20"]
    words = result.text.split()
    assert words == [f"{page}쪽" for page in range(1, 21)]
    assert result.pieces == 4


async def test_a_piece_over_12mb_is_split_before_sending() -> None:
    sizes = [PAGE_BYTES] * 20
    sizes[3] = 7 * 1024 * 1024
    sizes[4] = 6 * 1024 * 1024
    store = Store(sizes)
    seen, reader = reader_cutting_above(100)

    await read_with_model(20, store, reader)  # type: ignore[arg-type]

    # 4·5쪽(7MB·6MB)이 함께 든 조각은 계속 반으로 나뉘어 두 쪽이 따로 간다.
    assert seen == ["0-2", "2-3", "3-4", "4-5", "5-10", "10-20"]
    assert all(
        sum(sizes[int(a) : int(b)]) <= PIECE_MAX_BYTES
        for a, b in (piece.split("-") for piece in seen)
    )


async def test_a_single_page_over_12mb_cannot_be_sent() -> None:
    store = Store([PIECE_MAX_BYTES + 1])
    _, reader = reader_cutting_above(100)

    with pytest.raises(ScannedPdfError) as raised:
        await read_with_model(1, store, reader)  # type: ignore[arg-type]

    assert raised.value.code == "page_too_large"


async def test_a_single_page_that_still_hits_max_tokens_keeps_what_came_back() -> None:
    store = Store([PAGE_BYTES] * 2)
    _, reader = reader_cutting_above(0)

    result = await read_with_model(2, store, reader)  # type: ignore[arg-type]

    assert result.text.split() == ["1쪽", "2쪽"]
    assert result.warnings == [WARN_MODEL_READ_TRUNCATED]


async def test_the_model_reader_sends_the_pdf_as_an_image_block() -> None:
    adapter = FakeAdapter([[*fake_text("1쪽의 글"), fake_end("max_tokens")]])
    read = model_piece_reader(adapter, tier="paid", metadata={"document_id": "d"})

    text, cut = await read(b"%PDF-1.4 piece")

    assert (text, cut) == ("1쪽의 글", True)
    request = adapter.requests[0]
    assert request.tier == "paid"
    assert request.params.max_output_tokens == 32_000
    block = request.messages[0].blocks[0]
    assert isinstance(block, ImageBlock) and block.media_type == "application/pdf"


async def test_model_errors_become_scanned_pdf_errors() -> None:
    async def events() -> object:
        yield ErrorEvent(code="model_unavailable", message="과부하", retryable=True)

    with pytest.raises(ScannedPdfError) as raised:
        await collect_text(events())  # type: ignore[arg-type]

    assert (raised.value.code, raised.value.retryable) == ("model_unavailable", True)
