import re
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr

MASK = "***"

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


@dataclass
class _KeyState:
    secret: SecretStr
    success_count: int = 0
    failure_count: int = 0
    rate_limited_count: int = 0
    last_429_at: datetime | None = None
    cooldown_until: dict[str, float] = field(default_factory=dict[str, float])
    disabled: bool = False


class KeyPool:
    """GEMINI_API_KEYS 순환. 상태는 메모리에만 있고 재기동하면 처음부터 시작한다.

    429 쿨다운은 (키, 모델) 단위다. Gemini 한도가 모델별이라, 한 모델에서 모든 키가 429여도
    다음 모델은 같은 키들로 다시 시도할 수 있어야 폴백이 의미가 있다.
    401·403은 키 자체의 문제(폐기·권한)라 모델과 관계없이 그 키를 영구히 뺀다. 키는 .env에만
    있고 바꾸려면 재기동해야 하므로 다시 살릴 일이 없다.
    """

    def __init__(
        self,
        keys: Sequence[SecretStr | str],
        *,
        cooldown_sec: float,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        secrets = [key if isinstance(key, SecretStr) else SecretStr(key) for key in keys]
        self._keys = [_KeyState(secret) for secret in secrets if secret.get_secret_value()]
        self._cooldown_sec = cooldown_sec
        self._clock = clock
        self._now = now
        self._cursor = 0
        # 긴 키부터 지워야 한 키가 다른 키의 일부일 때도 남는 조각이 없다.
        self._redact_values = sorted(
            {state.secret.get_secret_value() for state in self._keys}, key=len, reverse=True
        )

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return f"KeyPool(keys={len(self._keys)}, cursor={self._cursor})"

    @property
    def cursor(self) -> int:
        return self._cursor

    def secret(self, index: int) -> str:
        return self._keys[index].secret.get_secret_value()

    def order(self) -> list[int]:
        count = len(self._keys)
        return [(self._cursor + offset) % count for offset in range(count)]

    def is_cooling_down(self, index: int, model: str) -> bool:
        state = self._keys[index]
        until = state.cooldown_until.get(model)
        if until is None:
            return False
        if self._clock() >= until:
            del state.cooldown_until[model]
            return False
        return True

    def is_disabled(self, index: int) -> bool:
        return self._keys[index].disabled

    def all_disabled(self) -> bool:
        return bool(self._keys) and all(state.disabled for state in self._keys)

    def pick(self, model: str, exclude: Collection[int] = ()) -> int | None:
        # 커서부터 이 모델에 쓸 수 있는 첫 키.
        # None이면 이 모델의 키가 소진된 것이다(모델 폴백 신호, 모두 disabled면 인증 실패).
        for index in self.order():
            if (
                index not in exclude
                and not self._keys[index].disabled
                and not self.is_cooling_down(index, model)
            ):
                return index
        return None

    def mark_success(self, index: int) -> None:
        self._keys[index].success_count += 1
        self._cursor = index

    def mark_auth_failed(self, index: int) -> None:
        state = self._keys[index]
        state.failure_count += 1
        state.disabled = True
        if self._cursor == index:
            self._cursor = (index + 1) % len(self._keys)

    def mark_rate_limited(self, index: int, model: str) -> None:
        state = self._keys[index]
        state.failure_count += 1
        state.rate_limited_count += 1
        state.last_429_at = self._now()
        state.cooldown_until[model] = self._clock() + self._cooldown_sec
        # 동시에 돈 다른 요청이 이미 커서를 옮겼으면 그대로 둔다.
        if self._cursor == index:
            self._cursor = (index + 1) % len(self._keys)

    def mark_failure(self, index: int) -> None:
        self._keys[index].failure_count += 1

    def status(self) -> list[KeyStatus]:
        now = self._clock()
        result: list[KeyStatus] = []
        for index, state in enumerate(self._keys):
            secret = state.secret.get_secret_value()
            remaining = {
                model: round(until - now, 1)
                for model, until in state.cooldown_until.items()
                if until > now
            }
            result.append(
                KeyStatus(
                    index=index,
                    last4=secret[-4:] if len(secret) >= _MIN_LEN_FOR_LAST4 else "",
                    success_count=state.success_count,
                    failure_count=state.failure_count,
                    rate_limited_count=state.rate_limited_count,
                    last_429_at=state.last_429_at,
                    cooldown_remaining=remaining,
                    disabled=state.disabled,
                )
            )
        return result

    def redact(self, text: str) -> str:
        for value in self._redact_values:
            text = text.replace(value, MASK)
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
