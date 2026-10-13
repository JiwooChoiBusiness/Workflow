import pytest

from app.security.rate_limit import API_REQUESTS_PER_MINUTE, SlidingWindowLimiter, new_api_limiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_blocks_after_limit_until_window_passes(clock: Clock) -> None:
    limiter = SlidingWindowLimiter(5, 60, clock=clock)

    for _ in range(5):
        assert limiter.hit("login").allowed
        clock.now += 1
    blocked = limiter.hit("login")

    assert not blocked.allowed
    # 첫 요청(1000초)이 창에서 빠지는 1060초까지 기다려야 한다. 지금은 1005초.
    assert blocked.retry_after_sec == 55
    clock.now = 1060.0
    assert limiter.hit("login").allowed


def test_window_slides_one_request_at_a_time(clock: Clock) -> None:
    limiter = SlidingWindowLimiter(2, 10, clock=clock)
    limiter.hit("k")
    clock.now += 5
    limiter.hit("k")

    clock.now += 5
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed


def test_rejected_hits_do_not_extend_the_block(clock: Clock) -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.hit("k")

    for _ in range(10):
        clock.now += 5
        assert not limiter.hit("k").allowed

    clock.now = 1060.0
    assert limiter.hit("k").allowed


def test_keys_are_independent_and_resettable(clock: Clock) -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    assert limiter.hit("a").allowed
    assert limiter.hit("b").allowed
    assert not limiter.hit("a").allowed

    limiter.reset("a")
    assert limiter.hit("a").allowed
    assert not limiter.hit("b").allowed

    limiter.reset()
    assert limiter.hit("b").allowed


def test_retry_after_is_at_least_one_second(clock: Clock) -> None:
    limiter = SlidingWindowLimiter(1, 1, clock=clock)
    limiter.hit("k")
    clock.now += 0.999

    assert limiter.hit("k").retry_after_sec == 1


def test_service_api_allows_sixty_per_minute_per_key(clock: Clock) -> None:
    # 설계 6.4: 키당 60 req/분. 61번째는 막히고 첫 요청이 창에서 빠질 때까지 기다린다.
    limiter = new_api_limiter(clock=clock)

    allowed = [limiter.hit("key-a").allowed for _ in range(API_REQUESTS_PER_MINUTE)]
    clock.now += 30
    blocked = limiter.hit("key-a")

    assert API_REQUESTS_PER_MINUTE == 60 and all(allowed)
    assert not blocked.allowed and blocked.retry_after_sec == 30
    assert limiter.hit("key-b").allowed
    clock.now += 30
    assert limiter.hit("key-a").allowed


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (5, 0)])
def test_rejects_nonsense_settings(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit, window)
