"""임베딩 설정, 전송 프로토콜, 리미터, 캐시, 클라이언트(3단계 설계 6.6).

채팅 어댑터와 키 풀·오류 분류를 같이 쓰고 전송만 프로토콜로 나눈다. 4-1이 네이티브 전송을 더해도
EmbeddingClient는 그대로다. 이 모듈은 app.knowledge를 import하지 않는다(그쪽이 이 모듈을 쓴다).
"""

import asyncio
import hashlib
import itertools
import json
import math
import struct
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

import aiosqlite
import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.llm.errors import (
    ErrorClass,
    TransportError,
    classify,
    daily_block_seconds,
    seconds_until_daily_reset,
)
from app.llm.key_pool import KeyPool, KeyTier
from app.llm.types import ERR_MODEL_AUTH, ERR_MODEL_REQUEST_INVALID, ERR_MODEL_UNAVAILABLE

log = structlog.get_logger("app.llm")

Priority = Literal["interactive", "bulk"]
EmbeddingPrefix = Literal["none", "gemini2"]

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"
DEFAULT_DIMENSIONS = 768
# gemini-embedding-2는 task_type 대신 접두어를 쓴다. 평가가 title: none 조건이었다(6.6, 17.3-10).
DOCUMENT_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "
# 입력 토큰 상한. 넘는 입력은 API가 오류 없이 잘라 버린다(chunking이 80%에서 자른다).
EMBED_INPUT_LIMITS = {"gemini-embedding-2": 8192, "gemini-embedding-001": 2048}
UNKNOWN_MODEL_INPUT_LIMIT = 2048
# 요청 하나에 101개부터 400이다[실측].
MAX_BATCH_ITEMS = 100
# 무료 등급 분당 100건(배치 항목 하나가 1건). 가짜 어댑터에는 걸지 않는다(K2·memcheck는 6,000).
FREE_EMBED_RPM_CAP = 100

WINDOW_SEC = 60.0
# bulk는 1분을 20초 조각 셋으로 나눠 보낸다. 한 번에 80개를 쏘고 60초를 비우지 않게 한다.
SLICES = 3
SLICE_SEC = WINDOW_SEC / SLICES

QUERY_CACHE_SIZE = 256
RATE_LIMIT_WAIT_SEC = 60.0
# bulk 분당 429가 이만큼 이어지면 unknown처럼 한 번 더 기다리고 그래도 429면 실패한다.
BULK_MINUTE_RETRIES = 3
PAID_RETRIES = 3
PAID_WAIT_SEC = 30.0
PAID_MAX_WAIT_SEC = 60.0
TRANSIENT_RETRY_DELAY_SEC = 2.0
# 5xx·연결 오류: 같은 키로 한 번 더, 그다음 다른 키로 한 번(모두 세 번).
TRANSIENT_ATTEMPTS = 3
# 2026-11-02 실측에서 gemini-embedding-2는 모든 차원에서 노름 1.0이다. 001은 3072가 아니면
# 0.59~0.70이라 정규화한다. 코사인 거리라 결과는 같지만 캐시·양자화를 위해 단위 벡터로 맞춘다.
NORM_TOLERANCE = 1e-4
CACHE_LOOKUP_BATCH = 500

ERR_RATE_LIMITED = "rate_limited"
ERR_RATE_LIMITED_DAILY = "rate_limited_daily"
ERR_PAID_KEY_MISSING = "paid_key_missing"
# 일일 한도는 태평양 자정에 풀린다(서머타임에 따라 한국 16시 또는 17시).
DAILY_LIMIT_MESSAGE = (
    "오늘의 임베딩 일일 한도를 다 썼다. 태평양 자정(한국 16~17시) 뒤에 다시 시도한다"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EmbeddingConfig(BaseModel):
    """KB의 색인 설정. 바꾸면 전량 재임베딩한다(version을 올린다)."""

    model_config = ConfigDict(frozen=True)

    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int = Field(default=DEFAULT_DIMENSIONS, ge=1, le=8192)
    prefix: EmbeddingPrefix = "gemini2"
    # gemini-embedding-001 네이티브 전송 전용(4-1 뒤). 1차에서는 항상 None이다.
    task_type: str | None = None
    version: int = Field(default=1, ge=1)

    def key(self) -> str:
        # embed_cache와 kb_index의 config_key. version은 넣지 않아 같은 설정의 KB끼리 캐시를 나눈다.
        return f"{self.model}:{self.dimensions}:{self.prefix}:{self.task_type or 'none'}"

    def document_input(self, text: str) -> str:
        return DOCUMENT_PREFIX + text if self.prefix == "gemini2" else text

    def query_input(self, text: str) -> str:
        return QUERY_PREFIX + text if self.prefix == "gemini2" else text

    @property
    def input_token_limit(self) -> int:
        return EMBED_INPUT_LIMITS.get(self.model, UNKNOWN_MODEL_INPUT_LIMIT)


def content_hash(text: str) -> str:
    # 임베딩 입력 원문(접두어 붙이기 전) 기준. 접두어 규칙은 config_key가 가른다.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_vector(values: Sequence[float]) -> bytes:
    # numpy 없이 f32 리틀 엔디언 BLOB으로 만든다. sqlite-vec float[N] 열의 형식이다.
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> list[float]:
    if len(blob) % 4:
        raise ValueError(f"f32 벡터의 바이트 수가 4의 배수가 아니다: {len(blob)}")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def normalized(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm == 0.0 or abs(norm - 1.0) <= NORM_TOLERANCE:
        return list(values)
    return [value / norm for value in values]


class EmbeddingTransport(Protocol):
    async def embed(
        self, *, api_key: str, model: str, inputs: Sequence[str], dimensions: int | None
    ) -> list[list[float]]: ...


class EmbeddingError(Exception):
    """임베딩 호출 실패. message에 키 값이 없다. retry_after는 다시 시도해도 되는 때까지 남은 초."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


@dataclass(frozen=True)
class TierLimits:
    rpm: int = 90
    # 0이면 토큰 버킷을 쓰지 않는다. 1,004토큰 청크 95개를 한 요청으로 보내 200이었다[실측].
    tpm: int = 0
    # 사람이 기다리는 요청(interactive) 전용 분당 몫.
    reserve: int = 10
    # 태평양 자정부터 센 하루 항목 수. 0이면 세지 않는다. 채팅 429가 키 커서를 옮겨 임베딩이 여러
    # 키로 흩어져도 하루 총량이 프로젝트 하나 몫(무료 1,000건[실측])을 넘지 않게 하는 장치다.
    rpd: int = 0

    def __post_init__(self) -> None:
        if self.rpm < 1 or self.tpm < 0 or self.rpd < 0:
            raise ValueError("rpm은 1 이상, tpm·rpd는 0 이상이다")
        if not 0 <= self.reserve < self.rpm:
            raise ValueError("reserve는 0 이상 rpm 미만이다")

    @property
    def daily_reserve(self) -> int:
        # 분당 예약분과 같은 비율을 하루치에서도 interactive 몫으로 남긴다. 큰 문서가 그날 몫을 다
        # 써도 질의 임베딩과 청크 편집은 된다.
        if not self.rpd:
            return 0
        return min(self.rpd - 1, math.ceil(self.rpd * self.reserve / self.rpm))


class DailyLimitReached(Exception):
    """리미터의 하루 몫을 다 썼다. retry_after는 다음 태평양 자정까지 남은 초."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"하루 몫을 다 썼다({retry_after:.0f}초 뒤 풀린다)")
        self.retry_after = retry_after


@dataclass(frozen=True)
class DailyUsage:
    limit: int
    bulk: int
    interactive: int
    reset_at: datetime

    @property
    def used(self) -> int:
        return self.bulk + self.interactive


class DailyStore(Protocol):
    # 하루 사용량을 재기동 뒤에도 잇는 저장소(index.db meta). 값은 이 모듈이 만든 JSON 글이다.
    async def load(self, key: str) -> str | None: ...
    async def save(self, key: str, value: str) -> None: ...


@dataclass
class _Daily:
    reset_at: datetime
    bulk: int = 0
    interactive: int = 0


@dataclass(frozen=True)
class _Grant:
    at: float
    # at + 60을 한 번만 계산해 둔다. 기다릴 시각도 이 값이라 경계 비교가 부동소수 오차로
    # 엇갈리지 않는다.
    expires: float
    items: int
    tokens: int
    priority: Priority


@dataclass(frozen=True)
class _Room:
    items: int
    tokens: int | None
    item_cap: int
    token_cap: int | None


class EmbedLimiter:
    """임베딩 호출 총량을 프로젝트 하나의 문서화된 한도 안에 묶는 전역 리미터(Z2, 6.6).

    등급(free/paid)마다 최근 60초의 허가 기록을 둔다. interactive는 먼저 예약분, 모자라면 공용분을
    바로 쓰고, bulk는 공용분(rpm - reserve)만 쓰되 시계에 맞춘 20초 조각마다 그 3분의 1까지 쓴다.
    조각을 미끄러지는 창으로 두면 깨어나는 시각의 오차로 60초 만료가 조각 경계보다 조금 앞서
    1개짜리 허가가 끼므로 경계를 고정한다. 토큰 버킷(tpm)을 켜면 같은 규칙을 토큰에도 건다
    (예약 토큰은 tpm × reserve / rpm).
    """

    def __init__(
        self,
        *,
        free: TierLimits | None = None,
        paid: TierLimits | None = None,
        free_rpm_cap: int | None = FREE_EMBED_RPM_CAP,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
        store: DailyStore | None = None,
    ) -> None:
        free = free or TierLimits()
        if free_rpm_cap is not None and free.rpm > free_rpm_cap:
            reserve = min(free.reserve, free_rpm_cap - 1)
            free = TierLimits(rpm=free_rpm_cap, tpm=free.tpm, reserve=reserve, rpd=free.rpd)
        self._limits: dict[KeyTier, TierLimits] = {"free": free, "paid": paid or TierLimits()}
        self._grants: dict[KeyTier, deque[_Grant]] = {"free": deque(), "paid": deque()}
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._store = store
        self._daily: dict[KeyTier, _Daily] = {}
        self._saving: asyncio.Task[None] | None = None
        self._dirty = False

    def limits(self, tier: KeyTier) -> TierLimits:
        return self._limits[tier]

    def batch_items(self, tier: KeyTier, priority: Priority) -> int:
        # 한 번에 보낼 수 있는 최대 항목 수(bulk는 조각 몫). 인제스트가 묶음 크기를 정할 때 쓴다.
        limits = self._limits[tier]
        if priority == "interactive":
            return min(MAX_BATCH_ITEMS, limits.rpm)
        return min(MAX_BATCH_ITEMS, math.ceil((limits.rpm - limits.reserve) / SLICES))

    def usage(self, tier: KeyTier) -> tuple[int, int]:
        """최근 60초에 허가한 (항목 수, 토큰 수)."""
        now = self._clock()
        self._prune(tier, now)
        grants = self._grants[tier]
        return sum(grant.items for grant in grants), sum(grant.tokens for grant in grants)

    def daily_usage(self, tier: KeyTier) -> DailyUsage | None:
        """오늘(태평양 날짜) 허가한 항목 수. 하루 몫을 세지 않는 등급이면 None."""
        limit = self._limits[tier].rpd
        if not limit:
            return None
        state = self._daily_state(tier)
        return DailyUsage(limit, state.bulk, state.interactive, state.reset_at)

    def daily_remaining(self, tier: KeyTier, priority: Priority) -> int | None:
        """그 우선순위가 오늘 더 받을 수 있는 항목 수. 세지 않으면 None."""
        limits = self._limits[tier]
        if not limits.rpd:
            return None
        state = self._daily_state(tier)
        if priority == "interactive":
            return max(0, limits.rpd - state.bulk - state.interactive)
        reserve = limits.daily_reserve
        overflow = max(0, state.interactive - reserve)
        return max(0, limits.rpd - reserve - state.bulk - overflow)

    def seconds_until_daily_reset(self, tier: KeyTier) -> float:
        return max((self._daily_state(tier).reset_at - self._now()).total_seconds(), 0.0)

    async def acquire(
        self, tier: KeyTier, priority: Priority, tokens: Sequence[int], *, partial: bool = True
    ) -> int:
        """tokens는 보낼 항목들의 추정 토큰이다. 허가된 앞쪽 항목 수를 돌려준다.

        partial이면 지금 들어가는 만큼(1개 이상)만 허가하고, 아니면 모두 들어갈 때까지 기다린다.
        다시 보내는 요청(400 분할, 429 뒤 재시도)은 partial=False로 같은 묶음을 통째로 받는다.
        하루 몫이 모자라면 자정까지 기다리지 않고 DailyLimitReached를 낸다.
        """
        if not tokens:
            return 0
        while True:
            daily = self.daily_remaining(tier, priority)
            if daily is not None and (daily < 1 or (not partial and daily < len(tokens))):
                raise DailyLimitReached(self.seconds_until_daily_reset(tier))
            wanted = tokens if daily is None else tokens[:daily]
            now = self._clock()
            self._prune(tier, now)
            granted = self._grantable(tier, priority, wanted, now, partial)
            if granted:
                spent = sum(wanted[:granted])
                grant = _Grant(now, now + WINDOW_SEC, granted, spent, priority)
                self._grants[tier].append(grant)
                if daily is not None:
                    self._count_daily(tier, priority, granted)
                return granted
            await self._sleep(self._wait(tier, priority, wanted, now, partial))

    async def load(self) -> None:
        """저장해 둔 오늘 사용량을 읽는다. 태평양 날짜가 지났으면 버린다(기동 때 한 번)."""
        if self._store is None:
            return
        for tier in self._limits:
            state = _parse_daily(await self._store.load(_daily_key(tier)))
            if state is not None and state.reset_at > self._now():
                self._daily[tier] = state

    async def flush(self) -> None:
        """밀린 저장을 마친다(종료 직전과 시험)."""
        while self._saving is not None:
            await asyncio.shield(self._saving)

    def _daily_state(self, tier: KeyTier) -> _Daily:
        now = self._now()
        state = self._daily.get(tier)
        if state is None or now >= state.reset_at:
            reset_at = now + timedelta(seconds=seconds_until_daily_reset(now))
            state = _Daily(reset_at=reset_at)
            self._daily[tier] = state
        return state

    def _count_daily(self, tier: KeyTier, priority: Priority, items: int) -> None:
        state = self._daily_state(tier)
        if priority == "interactive":
            state.interactive += items
        else:
            state.bulk += items
        if self._store is None:
            return
        # 허가 경로(질의 임베딩)가 index.db 쓰기 잠금을 기다리지 않게 저장은 뒤에서 몰아 한다.
        self._dirty = True
        if self._saving is None:
            self._saving = asyncio.get_running_loop().create_task(
                self._save_loop(), name="embed-daily-save"
            )

    async def _save_loop(self) -> None:
        store = self._store
        try:
            while store is not None and self._dirty:
                self._dirty = False
                for tier, state in list(self._daily.items()):
                    if not self._limits[tier].rpd:
                        continue
                    try:
                        await store.save(_daily_key(tier), _dump_daily(state))
                    except Exception as exc:
                        # 재기동 뒤 이어 세려고 남기는 것이다. 실패해도 세기는 메모리에서 계속한다.
                        log.warning("llm.embed_daily_save_failed", tier=tier, error=str(exc))
        finally:
            self._saving = None

    def _prune(self, tier: KeyTier, now: float) -> None:
        grants = self._grants[tier]
        while grants and grants[0].expires <= now:
            grants.popleft()

    def _room(self, tier: KeyTier, priority: Priority, at: float) -> _Room:
        limits = self._limits[tier]
        items: dict[Priority, int] = {"interactive": 0, "bulk": 0}
        used: dict[Priority, int] = {"interactive": 0, "bulk": 0}
        slice_items = slice_tokens = 0
        current = _slice_of(at)
        for grant in self._grants[tier]:
            if grant.expires <= at:
                continue
            items[grant.priority] += grant.items
            used[grant.priority] += grant.tokens
            if grant.priority == "bulk" and _slice_of(grant.at) == current:
                slice_items += grant.items
                slice_tokens += grant.tokens
        if priority == "interactive":
            return _Room(
                items=limits.rpm - items["interactive"] - items["bulk"],
                tokens=limits.tpm - used["interactive"] - used["bulk"] if limits.tpm else None,
                item_cap=limits.rpm,
                token_cap=limits.tpm or None,
            )
        shared = limits.rpm - limits.reserve
        item_cap = math.ceil(shared / SLICES)
        overflow = max(0, items["interactive"] - limits.reserve)
        room_items = min(shared - items["bulk"] - overflow, item_cap - slice_items)
        if not limits.tpm:
            return _Room(items=room_items, tokens=None, item_cap=item_cap, token_cap=None)
        reserve_tokens = math.ceil(limits.tpm * limits.reserve / limits.rpm)
        shared_tokens = limits.tpm - reserve_tokens
        token_cap = math.ceil(shared_tokens / SLICES)
        token_overflow = max(0, used["interactive"] - reserve_tokens)
        room_tokens = min(shared_tokens - used["bulk"] - token_overflow, token_cap - slice_tokens)
        return _Room(items=room_items, tokens=room_tokens, item_cap=item_cap, token_cap=token_cap)

    def _grantable(
        self, tier: KeyTier, priority: Priority, tokens: Sequence[int], at: float, partial: bool
    ) -> int:
        room = self._room(tier, priority, at)
        # 상한보다 큰 항목 하나·묶음 하나는 창이 비었을 때 통째로 내준다(영영 기다리지 않게).
        clamped = [min(value, room.token_cap) for value in tokens] if room.token_cap else tokens
        oversized = len(clamped) > room.item_cap or (
            room.token_cap is not None and sum(clamped) > room.token_cap
        )
        if oversized and not partial:
            empty = room.items >= room.item_cap and (
                room.tokens is None or room.token_cap is None or room.tokens >= room.token_cap
            )
            return len(clamped) if empty else 0
        count = spent = 0
        for value in clamped:
            if count >= room.items or (room.tokens is not None and spent + value > room.tokens):
                break
            count += 1
            spent += value
        if partial:
            return count
        return count if count == len(clamped) else 0

    def _wait(
        self, tier: KeyTier, priority: Priority, tokens: Sequence[int], now: float, partial: bool
    ) -> float:
        # 허가 기록이 60초 창을 벗어나는 시각과 다음 조각 경계 중 처음으로 들어가는 때까지 기다린다.
        moments = {grant.expires for grant in self._grants[tier]}
        if priority == "bulk":
            moments.add((_slice_of(now) + 1) * SLICE_SEC)
        for moment in sorted(moments):
            if moment > now and self._grantable(tier, priority, tokens, moment, partial):
                # 부동소수 오차로 경계 직전에 깨어 다시 자는 일을 막는다.
                return moment - now + 1e-6
        return WINDOW_SEC


def _slice_of(at: float) -> int:
    return math.floor(at / SLICE_SEC)


# 하루 사용량을 남기는 index.db meta 키의 앞부분. 색인을 다시 만들 때 새 파일로 옮긴다.
DAILY_META_PREFIX = "embed_daily_"


def _daily_key(tier: KeyTier) -> str:
    return f"{DAILY_META_PREFIX}{tier}"


def _dump_daily(state: _Daily) -> str:
    return json.dumps(
        {
            "reset_at": _format_time(state.reset_at),
            "bulk": state.bulk,
            "interactive": state.interactive,
        }
    )


def _parse_daily(raw: str | None) -> _Daily | None:
    if not raw:
        return None
    try:
        data: object = json.loads(raw)
        if not isinstance(data, dict):
            return None
        fields = cast(dict[str, object], data)
        reset_at = datetime.strptime(str(fields["reset_at"]), "%Y-%m-%dT%H:%M:%S.%fZ")
        bulk, interactive = fields.get("bulk", 0), fields.get("interactive", 0)
    except (KeyError, ValueError):
        return None
    if not isinstance(bulk, int) or not isinstance(interactive, int):
        return None
    return _Daily(reset_at=reset_at.replace(tzinfo=UTC), bulk=bulk, interactive=interactive)


class SqlStore(Protocol):
    # app.knowledge.index_db.IndexDb가 이 모양이다(이 모듈은 app.knowledge를 import하지 않는다).
    def read(self) -> AbstractAsyncContextManager[aiosqlite.Connection]: ...
    def write(self) -> AbstractAsyncContextManager[aiosqlite.Connection]: ...


# (config_key, 내용 해시들, 요청한 KB의 워크스페이스) → 그 워크스페이스에서 살아 있는 유닛의 벡터.
# app.db를 봐야 해서 app.services가 넣는다.
LiveVectorLookup = Callable[[str, Sequence[str], uuid.UUID], Awaitable[Mapping[str, bytes]]]


class EmbedCache:
    """같은 입력은 항상 같은 벡터라[실측] 내용 해시로 재사용한다(6.6).

    조회 순서는 (1) live: 같은 워크스페이스에서 같은 config_key인 KB의 살아 있는 유닛 벡터(vec_*에서
    복사, scope가 있을 때만), (2) index.db embed_cache(지웠거나 끈 유닛의 벡터, 30일·5만 행)다. API
    결과는 vec_*에 들어가므로 여기에 다시 쓰지 않고, 유닛을 지우거나 끌 때 store()로 옮겨 둔다.
    """

    def __init__(
        self,
        store: SqlStore,
        *,
        live: LiveVectorLookup | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._live = live
        self._now = now

    async def lookup(
        self, config_key: str, hashes: Collection[str], *, scope: uuid.UUID | None = None
    ) -> dict[str, bytes]:
        wanted = list(dict.fromkeys(hashes))
        found: dict[str, bytes] = {}
        if self._live is not None and scope is not None and wanted:
            found.update(await self._live(config_key, wanted, scope))
        missing = [value for value in wanted if value not in found]
        for batch in itertools.batched(missing, CACHE_LOOKUP_BATCH):
            marks = ", ".join("?" for _ in batch)
            async with self._store.read() as conn:
                rows = await conn.execute_fetchall(
                    "SELECT content_hash, vector FROM embed_cache "  # noqa: S608
                    f"WHERE config_key = ? AND content_hash IN ({marks})",
                    (config_key, *batch),
                )
            found.update((str(row[0]), bytes(row[1])) for row in rows)
        return found

    async def store(self, config_key: str, vectors: Mapping[str, bytes]) -> None:
        if not vectors:
            return
        stamp = _format_time(self._now())
        async with self._store.write() as conn:
            await conn.executemany(
                "INSERT INTO embed_cache (config_key, content_hash, vector, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (config_key, content_hash) "
                "DO UPDATE SET vector = excluded.vector, created_at = excluded.created_at",
                [(config_key, digest, vector, stamp) for digest, vector in vectors.items()],
            )

    async def purge(self, *, max_age_days: int, max_rows: int) -> int:
        cutoff = _format_time(self._now() - timedelta(days=max_age_days))
        async with self._store.write() as conn:
            async with conn.execute(
                "DELETE FROM embed_cache WHERE created_at < ?", (cutoff,)
            ) as cursor:
                deleted = max(cursor.rowcount, 0)
            rows = await conn.execute_fetchall("SELECT count(*) FROM embed_cache")
            extra = int(next(iter(rows))[0]) - max_rows
            if extra > 0:
                async with conn.execute(
                    "DELETE FROM embed_cache WHERE rowid IN "
                    "(SELECT rowid FROM embed_cache ORDER BY created_at LIMIT ?)",
                    (extra,),
                ) as cursor:
                    deleted += max(cursor.rowcount, 0)
        return deleted


def _format_time(value: datetime) -> str:
    # app.db·index.db와 같은 고정 폭 UTC 꼴(app/db/types.py TIMESTAMP_FORMAT).
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class EmbeddedDocuments:
    # 입력 순서. 400으로 빠진 항목은 None이고 errors에 입력 위치 → 사유가 있다.
    vectors: list[bytes | None]
    errors: dict[int, str] = field(default_factory=dict[int, str])
    # 캐시에서 채워 API를 부르지 않은 항목 수.
    cached: int = 0
    # 도중에 멈춘 사유(일일 한도, 429 소진, 인증 등). 그 앞까지 받은 벡터는 vectors에 그대로 있고
    # 보내지 못한 항목은 None이면서 errors에도 없다. 이미 한도에서 센 호출을 버리지 않으려는 것이다.
    failure: "EmbeddingError | None" = None
    # API로 받은 항목의 추정 토큰 합(접두어 포함). 응답에 usage가 없어 KB 통계에 이 값을 더한다.
    api_tokens: int = 0


class _InvalidInput(Exception):
    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class _Attempts:
    # 요청 하나(같은 묶음)를 보내는 동안의 재시도 기록.
    tried: set[int] = field(default_factory=set[int])
    minute_429: int = 0
    unknown_waited: bool = False
    paid_waits: int = 0
    transient: int = 0
    blocked_waits: int = 0
    # interactive에서 429를 준 키 → retryDelay. 모두 막혔을 때 가장 짧은 쪽으로 한 번만 더 보낸다.
    retry_after: dict[int, float] = field(default_factory=dict[int, float])
    last_try: bool = False


class EmbeddingClient:
    """임베딩 호출의 키 선택, 429·5xx 처리, 400 분할, 캐시(6.6, Z2 (b)).

    - bulk(인제스트)의 분당 429는 리미터 안의 계수 드리프트라 KeyPool.hold로 같은 키를 막아 두고
      retry_after만큼 기다려 같은 키로 다시 보낸다. 3번 이어지면 unknown처럼 한 번 더 기다린다.
    - interactive(질의, 청크 단건 편집, 평가)의 분당 429는 다음 키로 간다.
    - 일일 429는 두 우선순위 모두 exhaust 뒤 순환하지 않고 rate_limited_daily로 멈춘다. 그 등급의
      키 하나라도 일일 한도면 다른 키로도 보내지 않는다(하루 총량이 키 수만큼 늘어나므로).
    - paid는 키가 하나라 429·5xx에서 hold로 기다려 같은 키로 3번까지 다시 보낸다. 무료로 넘어가지
      않는다.
    """

    def __init__(
        self,
        transport: EmbeddingTransport,
        key_pool: KeyPool,
        limiter: EmbedLimiter,
        cache: EmbedCache | None,
        *,
        estimate_tokens: Callable[[str], int],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
        query_cache_size: int = QUERY_CACHE_SIZE,
        transient_delay_sec: float = TRANSIENT_RETRY_DELAY_SEC,
    ) -> None:
        # estimate_tokens는 토큰 버킷에 쓸 추정(app.knowledge.tokens.estimate_tokens). 이 모듈이
        # app.knowledge를 import하지 않도록 부르는 쪽이 넣는다. 시계는 KeyPool·리미터가 각자 갖는다.
        self._transport = transport
        self._pool = key_pool
        self._limiter = limiter
        self._cache = cache
        self._estimate = estimate_tokens
        self._sleep = sleep
        self._now = now
        self._queries: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._query_cache_size = query_cache_size
        self._transient_delay_sec = transient_delay_sec
        # (모델, 등급) → 마지막으로 성공한 키. 채팅의 429가 공용 커서를 옮겨도 임베딩은 한 키에
        # 머물러 키마다 하루 한도를 따로 쓰지 않게 한다(Z2).
        self._last_key: dict[tuple[str, KeyTier], int] = {}

    @property
    def limiter(self) -> EmbedLimiter:
        return self._limiter

    def daily_exhausted(self, model: str, tier: KeyTier = "free") -> float | None:
        """이 모델의 일일 한도가 그 등급에서 막혀 있으면 풀리기까지 남은 초(레인이 큐를 멈출 때)."""
        if not self._pool.any_exhausted(model, tier):
            return None
        remaining = [
            status.exhausted_remaining.get(model, 0.0)
            for status in self._pool.status()
            if status.tier == tier
        ]
        return max(remaining, default=0.0)

    def blocked_for_today(
        self, model: str, tier: KeyTier = "free", priority: Priority = "bulk"
    ) -> float | None:
        """오늘 더 보낼 수 없으면(일일 429나 리미터의 하루 몫) 풀리기까지 남은 초."""
        exhausted = self.daily_exhausted(model, tier)
        if exhausted is not None:
            return exhausted
        if self._limiter.daily_remaining(tier, priority) == 0:
            return self._limiter.seconds_until_daily_reset(tier)
        return None

    async def embed_documents(
        self,
        texts: Sequence[str],
        config: EmbeddingConfig,
        *,
        tier: KeyTier = "free",
        priority: Priority = "bulk",
        scope: uuid.UUID | None = None,
    ) -> EmbeddedDocuments:
        """실패해도 예외 대신 failure를 채워 돌려준다. 그때까지 받은 벡터는 그대로 있다.

        scope는 색인하는 KB의 워크스페이스다. 주면 그 워크스페이스의 다른 KB에 같은 글이 있을 때
        벡터를 복사한다(캐시 첫 단계).
        """
        if not texts:
            return EmbeddedDocuments(vectors=[])
        hashes = [content_hash(text) for text in texts]
        found: dict[str, bytes] = {}
        if self._cache is not None:
            width = config.dimensions * 4
            cached = await self._cache.lookup(config.key(), hashes, scope=scope)
            found = {digest: blob for digest, blob in cached.items() if len(blob) == width}
        cached_count = sum(1 for digest in hashes if digest in found)
        # 같은 글은 한 번만 보낸다.
        first_at: dict[str, int] = {}
        for position, digest in enumerate(hashes):
            if digest not in found:
                first_at.setdefault(digest, position)
        pending = list(first_at.values())
        errors_by_hash: dict[str, str] = {}
        failure: EmbeddingError | None = None
        api_tokens = 0
        while pending and failure is None:
            inputs = [
                config.document_input(texts[position]) for position in pending[:MAX_BATCH_ITEMS]
            ]
            batch_tokens = [self._estimate(text) for text in inputs]
            try:
                self._raise_if_daily_exhausted(config.model, tier)
                # 리미터가 지금 보낼 수 있는 만큼(bulk는 조각 몫)이 이번 묶음이다.
                granted = await self._acquire(tier, priority, batch_tokens)
                results, failure = await self._embed_batch(
                    inputs[:granted], batch_tokens[:granted], config, tier, priority, top=True
                )
            except EmbeddingError as exc:
                failure = exc
                break
            for position, result, tokens in zip(
                pending[:granted], results, batch_tokens[:granted], strict=True
            ):
                digest = hashes[position]
                if isinstance(result, str):
                    errors_by_hash[digest] = result
                elif result is not None:
                    found[digest] = pack_vector(result)
                    api_tokens += tokens
            pending = pending[granted:]
        vectors: list[bytes | None] = [found.get(digest) for digest in hashes]
        errors = {
            position: errors_by_hash[digest]
            for position, digest in enumerate(hashes)
            if digest in errors_by_hash
        }
        return EmbeddedDocuments(
            vectors=vectors,
            errors=errors,
            cached=cached_count,
            failure=failure,
            api_tokens=api_tokens,
        )

    async def embed_query(
        self, text: str, config: EmbeddingConfig, *, tier: KeyTier = "free"
    ) -> bytes:
        # 검색 시험에서 같은 질의를 되풀이할 때 호출이 없게 메모리 LRU를 둔다(리미터도 쓰지 않는다).
        key = (config.key(), text)
        cached = self._queries.get(key)
        if cached is not None:
            self._queries.move_to_end(key)
            return cached
        query = config.query_input(text)
        tokens = [self._estimate(query)]
        self._raise_if_daily_exhausted(config.model, tier)
        await self._acquire(tier, "interactive", tokens)
        results, failure = await self._embed_batch(
            [query], tokens, config, tier, "interactive", top=True
        )
        if failure is not None:
            raise failure
        result = results[0]
        if isinstance(result, str):
            raise EmbeddingError(ERR_MODEL_REQUEST_INVALID, result, retryable=False)
        if result is None:
            raise EmbeddingError(ERR_MODEL_UNAVAILABLE, "임베딩 결과가 없다", retryable=True)
        vector = pack_vector(result)
        self._queries[key] = vector
        while len(self._queries) > self._query_cache_size:
            self._queries.popitem(last=False)
        return vector

    async def embed_single(
        self,
        text: str,
        config: EmbeddingConfig,
        *,
        tier: KeyTier = "free",
        scope: uuid.UUID | None = None,
    ) -> bytes:
        # 청크 단건 편집. 사람이 기다리므로 interactive로 보낸다.
        result = await self.embed_documents(
            [text], config, tier=tier, priority="interactive", scope=scope
        )
        if result.failure is not None:
            raise result.failure
        vector = result.vectors[0]
        if vector is None:
            raise EmbeddingError(ERR_MODEL_REQUEST_INVALID, result.errors[0], retryable=False)
        return vector

    async def _acquire(
        self, tier: KeyTier, priority: Priority, tokens: Sequence[int], *, partial: bool = True
    ) -> int:
        try:
            return await self._limiter.acquire(tier, priority, tokens, partial=partial)
        except DailyLimitReached as exc:
            raise EmbeddingError(
                ERR_RATE_LIMITED_DAILY,
                DAILY_LIMIT_MESSAGE,
                retryable=True,
                retry_after=exc.retry_after,
            ) from None

    async def _embed_batch(
        self,
        inputs: list[str],
        tokens: list[int],
        config: EmbeddingConfig,
        tier: KeyTier,
        priority: Priority,
        *,
        top: bool = False,
    ) -> tuple[list[list[float] | str | None], EmbeddingError | None]:
        """(항목별 결과, 도중에 멈춘 사유). 결과는 벡터, 400 사유, 보내지 못했으면 None이다.

        400은 입력 문제다. 묶음 하나 때문에 전체가 실패하지 않게 반으로 나눠 범인만 가린다. 맨 위
        묶음은 부르는 쪽이 리미터 허가를 이미 받았고, 나눈 묶음은 보낼 때마다 다시 받는다. 나눈
        뒤쪽이 실패하면 앞쪽에서 받은 벡터는 살린다.
        """
        try:
            vectors = await self._request(inputs, tokens, config, tier, priority, acquired=top)
            return list[list[float] | str | None](vectors), None
        except EmbeddingError as exc:
            if top:
                raise
            return [None] * len(inputs), exc
        except _InvalidInput as exc:
            if len(inputs) == 1:
                return [exc.message], None
            middle = len(inputs) // 2
            left, failure = await self._embed_batch(
                inputs[:middle], tokens[:middle], config, tier, priority
            )
            if failure is not None:
                return [*left, *([None] * (len(inputs) - middle))], failure
            right, failure = await self._embed_batch(
                inputs[middle:], tokens[middle:], config, tier, priority
            )
            results = [*left, *right]
            if top and failure is None and all(isinstance(result, str) for result in results):
                # 하나씩 보내도 모두 400이면 입력이 아니라 요청 자체(모델·차원)의 문제다.
                # 다음 묶음도 같을 것이라 항목마다 오류로 두지 않고 멈춘다.
                raise EmbeddingError(
                    ERR_MODEL_REQUEST_INVALID, exc.message, retryable=False, status=exc.status
                ) from exc
            return results, failure

    async def _request(
        self,
        inputs: list[str],
        tokens: list[int],
        config: EmbeddingConfig,
        tier: KeyTier,
        priority: Priority,
        *,
        acquired: bool,
    ) -> list[list[float]]:
        model = config.model
        state = _Attempts()
        index: int | None = None
        while True:
            if index is None:
                index = await self._pick(model, tier, priority, state)
            # 기다리는 동안 다른 요청이 일일 한도를 만났을 수 있다. 다른 키로는 보내지 않는다.
            self._raise_if_daily_exhausted(model, tier)
            if not acquired:
                await self._acquire(tier, priority, tokens, partial=False)
            acquired = False
            try:
                raw = await self._transport.embed(
                    api_key=self._pool.secret(index),
                    model=model,
                    inputs=inputs,
                    dimensions=config.dimensions,
                )
            except TransportError as err:
                verdict = classify(err)
                message = self._pool.redact(err.message)
                log.warning(
                    "llm.embed_attempt_failed",
                    model=model,
                    key_index=index,
                    tier=tier,
                    priority=priority,
                    items=len(inputs),
                    status=err.status,
                    kind=verdict.kind,
                    quota_scope=verdict.scope,
                    retry_after=verdict.retry_after,
                    error=message[:300],
                )
                index = await self._after_failure(
                    err, verdict, message, index, model, tier, priority, state
                )
                continue
            self._pool.mark_success(index)
            self._last_key[(model, tier)] = index
            return [self._checked(vector, config) for vector in raw]

    async def _pick(self, model: str, tier: KeyTier, priority: Priority, state: _Attempts) -> int:
        while True:
            self._raise_if_daily_exhausted(model, tier)
            index = self._pool.pick(
                model, exclude=state.tried, tier=tier, prefer=self._last_key.get((model, tier))
            )
            if index is not None:
                return index
            if self._pool.count(tier) == 0:
                if tier == "paid":
                    raise EmbeddingError(
                        ERR_PAID_KEY_MISSING, "유료 키(GEMINI_PAID_API_KEY)가 없다", retryable=False
                    )
                raise EmbeddingError(ERR_MODEL_AUTH, "GEMINI_API_KEYS가 비어 있다", retryable=False)
            if self._pool.all_disabled(tier):
                raise EmbeddingError(
                    ERR_MODEL_AUTH, "키가 모두 401·403을 받아 빠졌다", retryable=False
                )
            wait = self._shortest_cooldown(model, tier)
            if priority == "interactive":
                if state.last_try:
                    raise EmbeddingError(
                        ERR_RATE_LIMITED,
                        "모든 키가 이 모델의 429 대기 중이다",
                        retryable=True,
                        retry_after=wait,
                    )
                # 모두 막혀 있으면 가장 짧은 retryDelay만큼 기다린 뒤 그 키로 한 번만 더 보낸다.
                state.last_try = True
                if state.retry_after:
                    retry_index, retry_wait = min(
                        state.retry_after.items(), key=lambda item: item[1]
                    )
                    await self._sleep(retry_wait)
                    return retry_index
            else:
                if state.blocked_waits >= BULK_MINUTE_RETRIES:
                    raise EmbeddingError(
                        ERR_RATE_LIMITED,
                        "모든 키가 이 모델의 429 대기 중이다",
                        retryable=True,
                        retry_after=wait,
                    )
                state.blocked_waits += 1
            state.tried.clear()
            await self._sleep(wait)

    async def _after_failure(
        self,
        err: TransportError,
        verdict: ErrorClass,
        message: str,
        index: int,
        model: str,
        tier: KeyTier,
        priority: Priority,
        state: _Attempts,
    ) -> int | None:
        """실패 하나를 키 풀에 적고 다음에 쓸 키를 돌려준다(None이면 새로 고른다). 끝이면 예외."""
        if verdict.kind == "invalid":
            if err.status in (400, 413):
                raise _InvalidInput(message, err.status)
            self._pool.mark_failure(index)
            raise EmbeddingError(
                ERR_MODEL_REQUEST_INVALID, message, retryable=False, status=err.status
            )
        if verdict.kind == "auth":
            self._pool.mark_auth_failed(index)
            log.warning("llm.key_disabled", key_index=index, status=err.status)
            state.tried.add(index)
            return None
        if verdict.kind == "rate_limited" and verdict.scope == "day":
            seconds = daily_block_seconds(verdict.retry_after, self._now())
            if verdict.retry_after is not None and verdict.retry_after < seconds:
                log.info(
                    "llm.daily_limit_until_reset",
                    model=model,
                    retry_after=verdict.retry_after,
                    blocked_sec=round(seconds),
                )
            self._pool.exhaust(index, model, seconds)
            raise EmbeddingError(
                ERR_RATE_LIMITED_DAILY,
                DAILY_LIMIT_MESSAGE,
                retryable=True,
                retry_after=seconds,
                status=err.status,
            )
        if tier == "paid":
            # 유료 키는 하나라 순환이 없다. 429·5xx 모두 같은 키로 기다렸다 다시 보낸다(6.6).
            if state.paid_waits >= PAID_RETRIES:
                self._pool.mark_failure(index)
                raise self._exhausted_error(err, verdict, message)
            state.paid_waits += 1
            wait = min(
                max(verdict.retry_after if verdict.retry_after is not None else PAID_WAIT_SEC, 0.0),
                PAID_MAX_WAIT_SEC,
            )
            self._pool.mark_failure(index)
            self._pool.hold(index, model, wait)
            await self._sleep(wait)
            return index
        if verdict.kind == "rate_limited":
            wait = verdict.retry_after if verdict.retry_after is not None else RATE_LIMIT_WAIT_SEC
            if priority == "interactive":
                # 사람이 기다리므로 다음 키로 간다. unknown도 같게 다룬다.
                self._pool.mark_rate_limited(index, model)
                state.tried.add(index)
                state.retry_after[index] = wait
                return None
            if verdict.scope == "minute" and state.minute_429 + 1 < BULK_MINUTE_RETRIES:
                state.minute_429 += 1
            elif not state.unknown_waited:
                state.minute_429 += 1
                state.unknown_waited = True
            else:
                self._pool.mark_failure(index)
                raise EmbeddingError(
                    ERR_RATE_LIMITED, message, retryable=True, retry_after=wait, status=err.status
                )
            # 리미터 안에서 난 분당 429다. 커서를 옮기지 않고 같은 키로 기다린다.
            self._pool.mark_failure(index)
            self._pool.hold(index, model, wait)
            await self._sleep(wait)
            return index
        # transient: 같은 키로 한 번 더, 그다음 다른 키로 한 번. 다른 키가 없으면 거기서 끝낸다.
        self._pool.mark_failure(index)
        state.transient += 1
        if state.transient >= TRANSIENT_ATTEMPTS:
            raise self._exhausted_error(err, verdict, message)
        if state.transient == 1:
            await self._sleep(self._transient_delay_sec)
            return index
        state.tried.add(index)
        other = self._pool.pick(model, exclude=state.tried, tier=tier)
        if other is None:
            raise self._exhausted_error(err, verdict, message)
        return other

    def _exhausted_error(
        self, err: TransportError, verdict: ErrorClass, message: str
    ) -> EmbeddingError:
        if verdict.kind == "rate_limited":
            return EmbeddingError(
                ERR_RATE_LIMITED,
                message,
                retryable=True,
                retry_after=verdict.retry_after,
                status=err.status,
            )
        return EmbeddingError(ERR_MODEL_UNAVAILABLE, message, retryable=True, status=err.status)

    def _raise_if_daily_exhausted(self, model: str, tier: KeyTier) -> None:
        remaining = self.daily_exhausted(model, tier)
        if remaining is not None:
            raise EmbeddingError(
                ERR_RATE_LIMITED_DAILY, DAILY_LIMIT_MESSAGE, retryable=True, retry_after=remaining
            )

    def _shortest_cooldown(self, model: str, tier: KeyTier) -> float:
        waits = [
            status.cooldown_remaining[model]
            for status in self._pool.status()
            if status.tier == tier and not status.disabled and model in status.cooldown_remaining
        ]
        return max(min(waits, default=RATE_LIMIT_WAIT_SEC), 0.0) + 0.1

    def _checked(self, vector: Sequence[float], config: EmbeddingConfig) -> list[float]:
        if len(vector) != config.dimensions:
            raise EmbeddingError(
                ERR_MODEL_REQUEST_INVALID,
                f"임베딩 차원({len(vector)})이 설정({config.dimensions})과 다르다",
                retryable=False,
            )
        return normalized(vector)
