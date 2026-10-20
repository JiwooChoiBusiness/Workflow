import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

MASK = "***"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5
# httpx는 요청마다 INFO로 URL 전체를 남기고 httpcore는 DEBUG로 헤더까지 남긴다. HTTP 요청 노드·
# web_fetch·http_request 도구의 URL에 들어간 워크플로우 비밀 env 값은 여기서 모르는 값이라 가릴 수
# 없으므로(2단계 9.4) 이 둘은 경고부터만 남긴다.
QUIET_LOGGERS = ("httpx", "httpcore")

# 키 이름은 '_'로 나눈 단어 단위로 본다. input_tokens, storage_key 같은 일반 필드는 가리지 않는다.
_SENSITIVE_KEY = re.compile(
    r"(^|_)(password|passwd|secrets?|token|api_?keys?|private_key|encryption_key"
    r"|authorization|cookie)($|_)"
)
_SENSITIVE_VALUES = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {MASK}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    # Google API 키 형식. Gemini 키는 AIza로 시작하거나 AQ.로 시작한다.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MASK),
    (re.compile(r"AQ\.[A-Za-z0-9_-]{20,}"), MASK),
)

MaskProcessor = Callable[[WrappedLogger, str, EventDict], EventDict]


def _is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key.lower().replace("-", "_")) is not None


def _mask(value: Any, secrets: Sequence[str] = ()) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, MASK)
        for pattern, replacement in _SENSITIVE_VALUES:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, Mapping):
        return {
            k: MASK if isinstance(k, str) and _is_sensitive_key(k) else _mask(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_mask(v, secrets) for v in value]
    return value


def mask_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    return _mask(event_dict)


def secret_masker(secrets: Iterable[str] = ()) -> MaskProcessor:
    """mask_secrets에 더해 설정에 든 비밀값(키 원문)을 형식과 관계없이 통째로 지운다."""
    # 긴 값부터 지워야 한 값이 다른 값의 일부일 때도 조각이 남지 않는다.
    values = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))

    def mask(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        return _mask(event_dict, values)

    return mask


_installed_handlers: list[logging.Handler] = []


def configure_logging(
    level: str = "INFO", log_file: Path | None = None, *, secrets: Iterable[str] = ()
) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    # 마스킹은 예외 문자열까지 덮도록 렌더링 직전에 한 번만 건다.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            secret_masker(secrets),
            structlog.processors.JSONRenderer(),
        ],
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
                delay=True,
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    # 다시 설정할 때 이전 파일 핸들러를 닫아야 Windows에서 로그 파일 잠금이 풀린다.
    for old in _installed_handlers:
        old.close()
    _installed_handlers[:] = handlers

    root = logging.getLogger()
    root.handlers = list(handlers)
    root.setLevel(level.upper())

    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # uvicorn이 따로 붙이는 핸들러를 떼어 내 같은 JSON 형식으로 모은다.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
