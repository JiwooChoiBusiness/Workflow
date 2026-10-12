import logging
import re
from collections.abc import Mapping
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

MASK = "***"

# 키 이름은 '_'로 나눈 단어 단위로 본다. input_tokens, storage_key 같은 일반 필드는 가리지 않는다.
_SENSITIVE_KEY = re.compile(
    r"(^|_)(password|passwd|secrets?|token|api_?keys?|private_key|encryption_key"
    r"|authorization|cookie)($|_)"
)
_SENSITIVE_VALUES = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {MASK}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    # Google API 키 형식. Gemini 키가 이 형식이다.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MASK),
)


def _is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key.lower().replace("-", "_")) is not None


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _SENSITIVE_VALUES:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, Mapping):
        return {
            k: MASK if isinstance(k, str) and _is_sensitive_key(k) else _mask(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_mask(v) for v in value]
    return value


def mask_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    return _mask(event_dict)


def configure_logging(level: str = "INFO") -> None:
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
            mask_secrets,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn이 따로 붙이는 핸들러를 떼어 내 같은 JSON 형식으로 모은다.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
