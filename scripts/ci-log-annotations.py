# CI e2e 서버 로그의 오류 줄을 GitHub Actions 주석 명령으로 찍는다. scripts/ci-e2e.sh가 실패했을 때
# 부른다. Actions 로그는 로그인해야 보이지만 check-run 주석은 공개 API로 읽힌다.
#
#   python scripts/ci-log-annotations.py <서버 로그> <포트>
#
# 서버 로그는 한 줄에 JSON 하나이고 한글이 \uXXXX로 들어 있어서 풀어서 보여 준다.
# Traceback·error 이상인 줄을 먼저, 5xx 응답 줄을 그다음에 골라 서로 다른 것 5개까지 낸다.
# 키 모양(AQ.·AIza로 시작하는 토큰, ENCRYPTION_KEY 값)은 가린다.
import json
import os
import re
import sys
from pathlib import Path

LIMIT = 5
LINE_CHARS = 300
MESSAGE_CHARS = 2000
# Traceback은 끝쪽(마지막 프레임과 예외)만 남긴다.
TAIL_LINES = 10
KEY_SHAPES = re.compile(r"AQ\.[A-Za-z0-9_-]+|AIza[0-9A-Za-z_-]{35}")
SERVER_ERROR = re.compile(r'HTTP/[\d.]+" 5\d\d$')
PLAIN_ERROR = re.compile(r"(?i)\b(error|fatal)\b")
SHOWN_APART = ("event", "level", "logger", "timestamp", "exception")


# (주석에 넣을 줄, 오류인지). 고르지 않을 줄이면 None.
def from_record(record: dict[str, object], row: str) -> tuple[list[str], bool] | None:
    event = str(record.get("event", ""))
    exception = str(record.get("exception") or "")
    if record.get("level") in ("error", "critical") or exception or "Traceback" in row:
        head = f"{record.get('logger')}: {event}"
        extra = {key: value for key, value in record.items() if key not in SHOWN_APART}
        if extra:
            head += " " + json.dumps(extra, ensure_ascii=False)
        return [head, *exception.strip().splitlines()[-TAIL_LINES:]], True
    if SERVER_ERROR.search(event):
        return [event], False
    return None


def pick(rows: list[str]) -> list[list[str]]:
    errors: list[list[str]] = []
    responses: list[list[str]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        index += 1
        try:
            record = json.loads(row)
        except ValueError:
            record = None
        if isinstance(record, dict):
            found = from_record(record, row)
            if found is not None:
                (errors if found[1] else responses).append(found[0])
        elif row.startswith("Traceback"):
            # 로그 설정 전에 죽으면 JSON이 아닌 여러 줄로 남는다. 들여 쓰지 않은 줄(예외)까지가
            # 한 덩이다.
            block: list[str] = []
            while index < len(rows):
                line = rows[index]
                index += 1
                block.append(line)
                if line and not line[0].isspace():
                    break
            errors.append([row, *block[-TAIL_LINES:]])
        elif PLAIN_ERROR.search(row):
            errors.append([row])
    return errors + responses


def message(lines: list[str], secret: str) -> str:
    text = "\n".join(line[:LINE_CHARS] for line in lines)[:MESSAGE_CHARS]
    if secret:
        text = text.replace(secret, "***")
    text = KEY_SHAPES.sub("***", text)
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    path, port = Path(sys.argv[1]), sys.argv[2]
    rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    secret = os.environ.get("ENCRYPTION_KEY", "")
    chosen: list[str] = []
    for lines in pick(rows):
        text = message(lines, secret)
        if text not in chosen:
            chosen.append(text)
        if len(chosen) == LIMIT:
            break
    for text in chosen:
        print(f"::error title=e2e 서버 로그 ({port})::{text}")


if __name__ == "__main__":
    main()
