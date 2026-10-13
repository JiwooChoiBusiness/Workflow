import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from app.logging import configure_logging, mask_secrets, secret_masker


def test_mask_secrets_by_key_and_value() -> None:
    # 비밀 탐지 도구에 걸리지 않도록 가짜 키는 실행 시에 만든다.
    fake_openai_key = "sk-" + "x" * 24
    fake_google_key = "AIza" + "A" * 35
    event = {
        "event": "upstream call failed: Authorization: Bearer abc.def-123",
        "api_key": "plain",
        "headers": {"X-Api-Key": "plain", "Cookie": "sid=1", "Accept": "text/plain"},
        "detail": f"keys {fake_openai_key} and {fake_google_key}",
        "input_tokens": 120,
        "storage_key": "snapshots/a.zip",
    }

    masked = mask_secrets(None, "info", event)

    assert masked["event"] == "upstream call failed: Authorization: Bearer ***"
    assert masked["api_key"] == "***"
    assert masked["headers"] == {"X-Api-Key": "***", "Cookie": "***", "Accept": "text/plain"}
    assert masked["detail"] == "keys *** and ***"
    assert masked["input_tokens"] == 120
    assert masked["storage_key"] == "snapshots/a.zip"


def test_mask_secrets_covers_aq_prefixed_keys() -> None:
    # 가짜 키는 실행 시에 만든다(비밀 탐지 도구와 키 접두어 검사에 걸리지 않게).
    token_key = "AQ" + "." + "Ab8RN6" + "x_y-z" * 6

    masked = mask_secrets(None, "info", {"event": f"call failed: key={token_key} rejected"})

    assert masked["event"] == "call failed: key=*** rejected"


def test_secret_masker_removes_configured_values_in_any_format() -> None:
    # 형식 패턴에 걸리지 않는 값도 설정에 있으면 통째로 지운다.
    configured = "custom-" + "Q" * 20
    shorter = configured[:12]
    mask = secret_masker([configured, shorter, ""])
    event = {
        "event": f"upstream said {configured}!",
        "nested": {"items": [f"prefix {shorter} suffix"]},
        "count": 3,
    }

    masked = mask(None, "info", event)

    assert masked["event"] == "upstream said ***!"
    assert masked["nested"] == {"items": ["prefix *** suffix"]}
    assert masked["count"] == 3


def test_configured_secrets_are_masked_in_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "app.log"
    configured = "plainvalue" + "7" * 20
    token_key = "AQ" + "." + "Z" * 30
    configure_logging("INFO", log_file, secrets=[configured])
    try:
        logger = structlog.get_logger("test")
        logger.info("upstream.failed", detail=f"bad {configured} and {token_key}")
        try:
            raise RuntimeError(f"boom {configured}")
        except RuntimeError:
            logger.exception("upstream.crashed")
        for handler in logging.getLogger().handlers:
            handler.flush()

        text = log_file.read_text(encoding="utf-8")
        lines = [json.loads(line) for line in text.splitlines()]
        assert lines[0]["detail"] == "bad *** and ***"
        assert "boom ***" in lines[1]["exception"]
        assert configured not in text
        assert token_key not in text
    finally:
        configure_logging("INFO")


def test_log_file_gets_masked_json_and_rotates(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "app.log"
    configure_logging("INFO", log_file)
    try:
        structlog.get_logger("test").info("file.written", api_key="plain", count=1)
        handler = next(
            h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
        )
        handler.flush()

        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5
        line = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
        assert line["event"] == "file.written"
        assert line["api_key"] == "***"
        assert line["count"] == 1
    finally:
        # 파일 핸들러를 닫아야 Windows에서 임시 폴더를 지울 수 있다.
        configure_logging("INFO")
