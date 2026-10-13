from collections.abc import Callable

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings는 model_validate로 만들어도 저장소의 .env와 환경변수를 읽는다.
    # 개발 PC의 값(실제 키, DATA_DIR)이 테스트에 섞이지 않게 둘 다 끊는다.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def build_skill_zip(
    name: str = "csv-tools",
    description: str = "CSV 파일을 읽고 요약하는 도구",
    *,
    top: str | None = "csv-tools",
    files: dict[str, bytes | str] | None = None,
    skill_md: str | None = None,
) -> bytes:
    """SKILL.md(프런트매터 + 본문)와 files를 담은 스킬 zip. top이 None이면 루트에 둔다."""
    import io
    import zipfile

    prefix = f"{top}/" if top else ""
    if skill_md is None:
        skill_md = f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n사용법\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "SKILL.md", skill_md)
        for path, data in (files or {}).items():
            archive.writestr(prefix + path, data)
    return buffer.getvalue()


@pytest.fixture
def skill_zip() -> Callable[..., bytes]:
    # 테스트 모듈은 conftest를 import할 수 없어(--import-mode=importlib) 픽스처로 넘긴다.
    return build_skill_zip
