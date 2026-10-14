"""디스크 한도 메시지의 크기 표기."""

import pytest

from app.sandbox.disk import format_size


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1KB"),
        (5000, "4.9KB"),
        (5 * 1024**2, "5MB"),
        (1536 * 1024**2, "1.5GB"),
        (2 * 1024**3, "2GB"),
        (3000 * 1024**3, "3000GB"),
    ],
)
def test_size_uses_a_unit_that_does_not_round_to_zero(size: int, text: str) -> None:
    assert format_size(size) == text
