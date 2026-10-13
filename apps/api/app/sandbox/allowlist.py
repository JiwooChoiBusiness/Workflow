import ipaddress
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger("app.sandbox")

RELOAD_CHECK_SEC = 2.0

# .pypi.org는 하위 도메인만 뜻하고 pypi.org 자신은 포함하지 않으므로 최상위 도메인을 따로 둔다.
DEFAULT_ENTRIES = (
    "pypi.org",
    ".pypi.org",
    ".pythonhosted.org",
    "github.com",
    ".github.com",
    ".githubusercontent.com",
)

DEFAULT_FILE_TEXT = (
    "# http_request 도구가 접속할 수 있는 호스트. 한 줄에 하나, 저장하면 2초 안에 반영된다.\n"
    "#   pypi.org      그 호스트만\n"
    "#   .pypi.org     하위 도메인 전부(pypi.org 자신은 제외)\n"
    "#   *             모든 공개 호스트(사설·루프백 주소는 여전히 막힌다)\n"
    "#   203.0.113.7   IP로 접속하려면 그 주소를 그대로 적는다(*에 포함되지 않는다)\n"
    "# 셸·파이썬이 직접 여는 네트워크는 이 목록과 관계없다. 그쪽은 승인 모드가 통제한다.\n"
    + "".join(f"{entry}\n" for entry in DEFAULT_ENTRIES)
)


def ensure_default_file(path: Path) -> bool:
    """파일이 없을 때만 기본 허용 목록을 만든다. 만들었으면 True."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(DEFAULT_FILE_TEXT)
    except FileExistsError:
        return False
    return True


def normalize_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if _is_ip(host):
        # 2001:0DB8::1과 2001:db8::1을 같은 주소로 비교한다.
        return str(ipaddress.ip_address(host))
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class AllowlistRules:
    exact: frozenset[str]
    suffixes: tuple[str, ...]
    allow_any: bool

    @classmethod
    def parse(cls, lines: Iterable[str]) -> "AllowlistRules":
        exact: set[str] = set()
        suffixes: list[str] = []
        allow_any = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line == "*":
                allow_any = True
            elif line.startswith(".") and len(line) > 1:
                suffixes.append("." + normalize_host(line[1:]))
            else:
                exact.add(normalize_host(line))
        return cls(frozenset(exact), tuple(suffixes), allow_any)

    def allows(self, host: str) -> bool:
        host = normalize_host(host)
        if not host:
            return False
        if host in self.exact:
            return True
        if _is_ip(host):
            # IP로 접속하려면 그 주소를 목록에 그대로 적어야 한다. *는 이름에만 적용한다.
            return False
        if self.allow_any:
            return True
        return any(host.endswith(suffix) for suffix in self.suffixes)


class Allowlist:
    """허용 목록 파일. 2초마다 mtime·크기를 확인해 바뀌었으면 다시 읽는다."""

    def __init__(
        self,
        path: Path,
        *,
        check_interval: float = RELOAD_CHECK_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._interval = check_interval
        self._clock = clock
        self._signature: tuple[int, int] | None = None
        self._rules = AllowlistRules(frozenset(), (), False)
        self._checked_at: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    def rules(self) -> AllowlistRules:
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._interval:
            self._checked_at = now
            self._reload_if_changed()
        return self._rules

    def allows(self, host: str) -> bool:
        return self.rules().allows(host)

    def _reload_if_changed(self) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            if self._signature != (-1, -1):
                log.warning("allowlist.missing", path=str(self._path))
            # 파일이 없으면 아무 호스트도 허용하지 않는다.
            self._signature = (-1, -1)
            self._rules = AllowlistRules(frozenset(), (), False)
            return
        except OSError as exc:
            log.warning("allowlist.stat_failed", path=str(self._path), error=str(exc))
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("allowlist.read_failed", path=str(self._path), error=str(exc))
            return
        self._signature = signature
        self._rules = AllowlistRules.parse(text.splitlines())
        log.info(
            "allowlist.loaded",
            path=str(self._path),
            entries=len(self._rules.exact) + len(self._rules.suffixes) + self._rules.allow_any,
        )
