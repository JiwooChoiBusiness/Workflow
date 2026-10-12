from app.logging import mask_secrets


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
        "storage_key": "snapshots/a.tar.gz",
    }

    masked = mask_secrets(None, "info", event)

    assert masked["event"] == "upstream call failed: Authorization: Bearer ***"
    assert masked["api_key"] == "***"
    assert masked["headers"] == {"X-Api-Key": "***", "Cookie": "***", "Accept": "text/plain"}
    assert masked["detail"] == "keys *** and ***"
    assert masked["input_tokens"] == 120
    assert masked["storage_key"] == "snapshots/a.tar.gz"
