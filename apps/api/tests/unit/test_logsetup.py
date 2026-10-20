import base64
import json
import logging
import secrets
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import structlog
from pydantic import SecretStr

from app.config import Settings
from app.logsetup import configure_logging, mask_secrets, secret_masker


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


def test_http_client_request_lines_stay_out_of_the_log(tmp_path: Path) -> None:
    # 요청 URL에 든 워크플로우 비밀 env 값은 마스커가 모르는 값이다. 그 줄 자체를 남기지 않는다.
    log_file = tmp_path / "logs" / "app.log"
    configure_logging("DEBUG", log_file)
    try:
        logging.getLogger("httpx").info("HTTP Request: GET https://h.example/?token=t0ken-value")
        logging.getLogger("httpcore.http11").debug("send_request_headers.started")
        logging.getLogger("httpx").warning("still kept")
        for handler in logging.getLogger().handlers:
            handler.flush()

        text = log_file.read_text(encoding="utf-8")
        assert "t0ken-value" not in text and "send_request_headers" not in text
        assert "still kept" in text
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


def _encryption_key_like() -> str:
    # ENCRYPTION_KEY 꼴(32바이트 base64). 앞 3바이트를 골라 +, /, =가 반드시 들어가게 한다.
    value = base64.b64encode(b"\xfb\xff\xbf" + secrets.token_bytes(29)).decode()
    assert {"+", "/", "="} <= set(value)
    return value


# 실제 키는 어디에도 쓰지 않는다. 모양만 같은 값을 실행할 때 만든다.
FAKE_SECRETS = {
    "google_key": "AIza" + secrets.token_hex(18)[:35],
    "aq_key": "AQ" + "." + secrets.token_urlsafe(24),
    "platform_api_key": "sk-wf-" + secrets.token_urlsafe(32),
    "bearer": "Bearer " + secrets.token_hex(4) + "." + secrets.token_hex(4),
    "configured_value": _encryption_key_like(),
}


def _as_text(value: str) -> None:
    structlog.get_logger("test").info("upstream.failed", detail=f"upstream said {value} twice")


def _in_nested_dict(value: str) -> None:
    structlog.get_logger("test").info("upstream.failed", payload={"request": {"note": value}})


def _in_list(value: str) -> None:
    structlog.get_logger("test").info("upstream.failed", items=["first", value, {"deep": [value]}])


def _in_exception(value: str) -> None:
    try:
        raise RuntimeError(f"upstream rejected {value}")
    except RuntimeError:
        structlog.get_logger("test").exception("upstream.crashed")


def _in_access_log(value: str) -> None:
    # uvicorn.access가 내는 줄과 같은 모양. 쿼리 문자열은 받은 그대로 찍힌다.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", f"/api/v1/info?k={value}", "1.1", 200
    )


LOG_PLACES: dict[str, Callable[[str], None]] = {
    "text": _as_text,
    "nested_dict": _in_nested_dict,
    "list": _in_list,
    "exception": _in_exception,
    "uvicorn_access": _in_access_log,
}


@pytest.mark.parametrize("place", sorted(LOG_PLACES))
@pytest.mark.parametrize("kind", sorted(FAKE_SECRETS))
def test_secrets_never_reach_the_log_file(tmp_path: Path, kind: str, place: str) -> None:
    value = FAKE_SECRETS[kind]
    log_file = tmp_path / "app.log"
    # 형식 패턴이 아니라 설정값이라서 가려지는 것은 configured_value 하나뿐이다.
    configure_logging("INFO", log_file, secrets=[FAKE_SECRETS["configured_value"]])
    try:
        LOG_PLACES[place](value)
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
    finally:
        configure_logging("INFO")

    assert value not in text
    assert "***" in text


@pytest.mark.parametrize(("length", "masked"), [(19, False), (20, True)])
def test_aq_keys_are_masked_from_twenty_characters(length: int, masked: bool) -> None:
    value = "AQ" + "." + "k" * length

    shown = mask_secrets(None, "info", {"event": f"key {value} end"})["event"]

    assert shown == ("key *** end" if masked else f"key {value} end")


def test_ordinary_fields_are_left_alone() -> None:
    event = {
        "event": "storage.read",
        "storage_key": "snapshots/a.zip",
        "input_tokens": 120,
        "headers": {"Accept": "text/plain"},
    }

    assert mask_secrets(None, "info", dict(event)) == event


def test_secret_values_feed_every_configured_key_to_the_masker(tmp_path: Path) -> None:
    gemini = ["gm-" + secrets.token_hex(12), "gm-" + secrets.token_hex(12)]
    encryption = _encryption_key_like()
    e2b = "e2b-" + secrets.token_hex(12)
    settings = Settings.model_validate(
        {"gemini_api_keys": gemini, "encryption_key": encryption, "e2b_api_key": e2b}
    )
    log_file = tmp_path / "app.log"

    assert settings.secret_values() == [*gemini, encryption, e2b]
    assert isinstance(settings.encryption_key, SecretStr)
    configure_logging("INFO", log_file, secrets=settings.secret_values())
    try:
        structlog.get_logger("test").info(
            "config.loaded", detail=" | ".join([*gemini, encryption, e2b])
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
    finally:
        configure_logging("INFO")

    assert json.loads(text.splitlines()[-1])["detail"] == " | ".join(["***"] * 4)
    for value in [*gemini, encryption, e2b]:
        assert value not in text
