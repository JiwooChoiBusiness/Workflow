import re
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import SecretStr

MASK = "***"

KeyTier = Literal["free", "paid"]

# 키 값을 알고 있으면 그대로 지우고, 모르는 형식(다른 키가 섞인 오류 본문 등)은 패턴으로 지운다.
_SECRET_PATTERNS = (
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), MASK),
    (re.compile(r"\bAQ\.[0-9A-Za-z_.-]{16,}"), MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {MASK}"),
    (re.compile(r"(?i)([?&]key=)[^&\s\"']+"), rf"\1{MASK}"),
)
# 이보다 짧은 키는 마지막 4자리도 보여주지 않는다.
_MIN_LEN_FOR_LAST4 = 12


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class KeyStatus:
    index: int
    last4: str
    success_count: int
    failure_count: int
    rate_limited_count: int
    last_429_at: datetime | None
    # 모델 id → 남은 쿨다운 초. 쿨다운이 없는 모델은 빠진다.
    cooldown_remaining: dict[str, float]
    # 401·403을 받아 이 프로세스가 끝날 때까지 쓰지 않는 키.
    disabled: bool = False
    tier: KeyTier = "free"
    # 모델 id → 일일 한도가 풀리기까지 남은 초.
    exhausted_remaining: dict[str, float] = field(default_factory=dict[str, float])


@dataclass
class _KeyState:
    secret: SecretStr
    tier: KeyTier = "free"
    success_count: int = 0
    failure_count: int = 0
    rate_limited_count: int = 0
    last_429_at: datetime | None = None
    cooldown_until: dict[str, float] = field(default_factory=dict[str, float])
    exhausted_until: dict[str, float] = field(default_factory=dict[str, float])
    disabled: bool = False


def _secrets(keys: Sequence[SecretStr | str]) -> list[SecretStr]:
    secrets = [key if isinstance(key, SecretStr) else SecretStr(key) for key in keys]
    return [secret for secret in secrets if secret.get_secret_value()]


class KeyPool:
    """GEMINI_API_KEYS 순환과 유료 키. 상태는 메모리에만 있고 재기동하면 처음부터 시작한다.

    429 쿨다운은 (키, 모델) 단위다. Gemini 한도가 모델별이라, 한 모델에서 모든 키가 429여도
    다음 모델은 같은 키들로 다시 시도할 수 있어야 폴백이 의미가 있다.
    401·403은 키 자체의 문제(폐기·권한)라 모델과 관계없이 그 키를 영구히 뺀다. 키는 .env에만
    있고 바꾸려면 재기동해야 하므로 다시 살릴 일이 없다.

    유료 키(GEMINI_PAID_API_KEY)는 무료 키 뒤의 index를 받고, 등급마다 커서가 따로라 유료 호출이
    무료 키의 순환 위치를 바꾸지 않는다. pick은 요청한 등급의 키만 고른다.
    """

    def __init__(
        self,
        keys: Sequence[SecretStr | str],
        *,
        cooldown_sec: float,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        paid_keys: Sequence[SecretStr | str] = (),
    ) -> None:
        self._keys = [_KeyState(secret) for secret in _secrets(keys)]
        free_count = len(self._keys)
        self._keys.extend(_KeyState(secret, tier="paid") for secret in _secrets(paid_keys))
        self._ranges: dict[KeyTier, range] = {
            "free": range(0, free_count),
            "paid": range(free_count, len(self._keys)),
        }
        self._cursors: dict[KeyTier, int] = {"free": 0, "paid": free_count}
        self._cooldown_sec = cooldown_sec
        self._clock = clock
        self._now = now
        # 긴 키부터 지워야 한 키가 다른 키의 일부일 때도 남는 조각이 없다.
        self._redact_values = sorted(
            {state.secret.get_secret_value() for state in self._keys}, key=len, reverse=True
        )

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        free, paid = self.count("free"), self.count("paid")
        return f"KeyPool(keys={free}, paid={paid}, cursor={self.cursor})"

    @property
    def cursor(self) -> int:
        return self._cursors["free"]

    def count(self, tier: KeyTier = "free") -> int:
        return len(self._ranges[tier])

    def tier_of(self, index: int) -> KeyTier:
        return self._keys[index].tier

    def secret(self, index: int) -> str:
        return self._keys[index].secret.get_secret_value()

    def order(self, tier: KeyTier = "free") -> list[int]:
        indexes = self._ranges[tier]
        if not indexes:
            return []
        start = self._cursors[tier] - indexes.start
        return [indexes.start + (start + offset) % len(indexes) for offset in range(len(indexes))]

    def is_cooling_down(self, index: int, model: str) -> bool:
        return self._active(self._keys[index].cooldown_until, model)

    def is_exhausted(self, index: int, model: str) -> bool:
        return self._active(self._keys[index].exhausted_until, model)

    def any_exhausted(self, model: str, tier: KeyTier = "free") -> bool:
        """그 등급의 키 중 하나라도 이 모델의 일일 한도를 다 썼는지.

        일일 한도에서 다른 키로 넘어가면 하루 총량이 키 수만큼 늘어나므로(3단계 설계 Z2) 호출하는
        쪽이 이 모델을 건너뛸 때 쓴다.
        """
        return any(self.is_exhausted(index, model) for index in self._ranges[tier])

    def _active(self, until_by_model: dict[str, float], model: str) -> bool:
        until = until_by_model.get(model)
        if until is None:
            return False
        if self._clock() >= until:
            del until_by_model[model]
            return False
        return True

    def is_disabled(self, index: int) -> bool:
        return self._keys[index].disabled

    def all_disabled(self, tier: KeyTier = "free") -> bool:
        indexes = self._ranges[tier]
        return bool(indexes) and all(self._keys[index].disabled for index in indexes)

    def pick(
        self,
        model: str,
        exclude: Collection[int] = (),
        tier: KeyTier = "free",
        prefer: int | None = None,
    ) -> int | None:
        # 커서부터 이 모델에 쓸 수 있는 그 등급의 첫 키. prefer가 쓸 수 있으면 그 키가 먼저다.
        # None이면 이 모델의 키가 소진된 것이다(모델 폴백 신호, 모두 disabled면 인증 실패).
        order = self.order(tier)
        if prefer is not None and prefer in order:
            order.remove(prefer)
            order.insert(0, prefer)
        for index in order:
            if (
                index not in exclude
                and not self._keys[index].disabled
                and not self.is_cooling_down(index, model)
                and not self.is_exhausted(index, model)
            ):
                return index
        return None

    def _advance(self, index: int) -> None:
        # 동시에 돈 다른 요청이 이미 커서를 옮겼으면 그대로 둔다.
        tier = self.tier_of(index)
        if self._cursors[tier] == index:
            indexes = self._ranges[tier]
            self._cursors[tier] = indexes.start + (index - indexes.start + 1) % len(indexes)

    def mark_success(self, index: int) -> None:
        self._keys[index].success_count += 1
        self._cursors[self.tier_of(index)] = index

    def mark_auth_failed(self, index: int) -> None:
        state = self._keys[index]
        state.failure_count += 1
        state.disabled = True
        self._advance(index)

    def mark_rate_limited(self, index: int, model: str) -> None:
        state = self._keys[index]
        state.failure_count += 1
        state.rate_limited_count += 1
        state.last_429_at = self._now()
        state.cooldown_until[model] = self._clock() + self._cooldown_sec
        self._advance(index)

    def mark_failure(self, index: int) -> None:
        self._keys[index].failure_count += 1

    def hold(self, index: int, model: str, seconds: float) -> None:
        """(키, 모델)을 seconds 동안 pick에서 뺀다. 커서는 그대로라 기다린 뒤 같은 키로 돌아온다.

        mark_rate_limited는 커서를 다음 키로 옮기므로 "같은 키로 기다리기"에는 이것을 쓴다.
        """
        cooldowns = self._keys[index].cooldown_until
        cooldowns[model] = max(cooldowns.get(model, 0.0), self._clock() + seconds)

    def exhaust(self, index: int, model: str, seconds: float) -> None:
        """일일 한도 429. (키, 모델)을 seconds 동안 pick에서 빼고 status()에 남긴다.

        커서는 옮기지 않는다. 일일 한도에서는 다른 키로 순환하지 않는다(3단계 설계 Z2).
        """
        state = self._keys[index]
        state.failure_count += 1
        state.rate_limited_count += 1
        state.last_429_at = self._now()
        until = state.exhausted_until
        until[model] = max(until.get(model, 0.0), self._clock() + seconds)

    def status(self) -> list[KeyStatus]:
        now = self._clock()
        result: list[KeyStatus] = []
        for index, state in enumerate(self._keys):
            secret = state.secret.get_secret_value()
            result.append(
                KeyStatus(
                    index=index,
                    last4=secret[-4:] if len(secret) >= _MIN_LEN_FOR_LAST4 else "",
                    success_count=state.success_count,
                    failure_count=state.failure_count,
                    rate_limited_count=state.rate_limited_count,
                    last_429_at=state.last_429_at,
                    cooldown_remaining=_remaining(state.cooldown_until, now),
                    disabled=state.disabled,
                    tier=state.tier,
                    exhausted_remaining=_remaining(state.exhausted_until, now),
                )
            )
        return result

    def redact(self, text: str) -> str:
        for value in self._redact_values:
            text = text.replace(value, MASK)
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def _remaining(until_by_model: dict[str, float], now: float) -> dict[str, float]:
    return {model: round(until - now, 1) for model, until in until_by_model.items() if until > now}
