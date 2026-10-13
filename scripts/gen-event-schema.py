# 런 이벤트 JSON Schema(packages/event-schema/run-events.schema.json)를 Pydantic 모델에서 다시
# 만든다. 원본은 apps/api/app/schemas/events.py다. 화면(apps/web)은 이 파일에서 타입을 만든다.
#
#   uv run python scripts/gen-event-schema.py          다시 쓴다
#   uv run python scripts/gen-event-schema.py --check  같은지만 본다(다르면 종료 코드 1)
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "packages" / "event-schema" / "run-events.schema.json"


def render() -> str:
    from app.schemas.events import run_events_json_schema

    return json.dumps(run_events_json_schema(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="런 이벤트 JSON Schema를 만든다")
    parser.add_argument("--check", action="store_true", help="파일이 최신인지만 확인한다")
    args = parser.parse_args(argv)
    text = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else None
    if args.check:
        if current != text:
            print(
                f"{TARGET.relative_to(ROOT)}가 모델과 다르다. 스크립트를 다시 돌린다.",
                file=sys.stderr,
            )
            return 1
        return 0
    if current == text:
        print(f"{TARGET.relative_to(ROOT)}: 바뀐 것 없음")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"{TARGET.relative_to(ROOT)}: 다시 썼다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
