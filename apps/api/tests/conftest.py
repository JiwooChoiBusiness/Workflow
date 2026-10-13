import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings는 model_validate로 만들어도 저장소의 .env와 환경변수를 읽는다.
    # 개발 PC의 값(실제 키, DATA_DIR)이 테스트에 섞이지 않게 둘 다 끊는다.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
