# CI e2e 서버 로그의 샌드박스 실행 요약과 오류 줄을 GitHub Actions 주석 명령으로 찍는다.
# scripts/ci-e2e.sh가 실패했을 때 부른다. Actions 로그는 로그인해야 보이지만 check-run 주석은 공개
# API로 읽힌다. 등급을 경고로 두는 것은 error 주석이 스텝당 10개까지이고 Playwright github
# 리포터가 실패 주석으로 그 몫을 먼저 다 쓰기 때문이다.
#
#   python scripts/ci-log-annotations.py <서버 로그> <포트>
#
# 서버 로그는 한 줄에 JSON 하나이고 한글이 \uXXXX로 들어 있어서 풀어서 보여 준다.
# 샌드박스 요약은 sandbox.exec_started·exec_finished 기록의 수, timed_out·killed 수, duration_ms
# 최소·중앙·최대, 끝 기록이 없는 실행 수, 처음 몇 개의 (kind, code, timed_out, duration_ms)다.
# 오류 줄은 Traceback·error 이상인 줄을 먼저, 5xx 응답 줄을 그다음에 골라 서로 다른 것 5개까지 낸다.
# 키 모양(AQ.·AIza로 시작하는 토큰, ENCRYPTION_KEY 값)은 가린다.
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIMIT = 5
SANDBOX_SHOWN = 8
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


@dataclass
class SandboxExec:
    kind: str
    finished: dict[str, Any] | None = None

    def shown(self) -> str:
        if self.finished is None:
            return f"({self.kind}, 끝 기록 없음)"
        values = [self.finished.get(key) for key in ("code", "timed_out", "duration_ms")]
        return f"({', '.join([self.kind, *map(str, values)])})"


def sandbox_summary(rows: list[str]) -> list[str]:
    execs: list[SandboxExec] = []
    # exec_id는 대개 도구 호출 id라 한 번만 나오지만 sql·sql-prepare처럼 되풀이되는 것도 있다. 끝
    # 기록은 같은 id로 시작해 아직 끝나지 않은 것 중 먼저 시작한 것과 짝짓는다.
    running: dict[str, list[SandboxExec]] = {}
    started = 0
    finished: list[dict[str, Any]] = []
    for row in rows:
        if "sandbox.exec_" not in row:
            continue
        try:
            record = json.loads(row)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        event = record.get("event")
        exec_id = str(record.get("exec_id"))
        if event == "sandbox.exec_started":
            started += 1
            entry = SandboxExec(str(record.get("kind")))
            execs.append(entry)
            running.setdefault(exec_id, []).append(entry)
        elif event == "sandbox.exec_finished":
            finished.append(record)
            waiting = running.get(exec_id)
            if waiting:
                entry = waiting.pop(0)
            else:
                entry = SandboxExec("?")
                execs.append(entry)
            entry.finished = record
    if not execs:
        return ["샌드박스 실행 기록 없음"]
    unfinished = sum(len(left) for left in running.values())
    timed_out = sum(1 for record in finished if record.get("timed_out") is True)
    killed = sum(1 for record in finished if record.get("killed") is True)
    lines = [
        f"시작 {started}, 끝 {len(finished)}, 끝 기록 없음 {unfinished}, "
        f"timed_out {timed_out}, killed {killed}"
    ]
    durations = [ms for record in finished if isinstance(ms := record.get("duration_ms"), int)]
    if durations:
        lines.append(
            f"duration_ms 최소 {min(durations)}, 중앙 {round(statistics.median(durations))}, "
            f"최대 {max(durations)}"
        )
    shown = execs[:SANDBOX_SHOWN]
    lines.append(f"처음 {len(shown)}개 (kind, code, timed_out, duration_ms)")
    lines.extend(entry.shown() for entry in shown)
    return lines


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
    # 경고 주석도 스텝당 10개까지다. 요약 하나와 오류 줄 5개면 넘지 않지만, 같은 스텝의 Playwright가
    # 느린 시험 경고(최대 5)를 먼저 냈으면 넘친 뒤쪽이 빠지므로 요약을 먼저 낸다.
    summary = message(sandbox_summary(rows), secret)
    print(f"::warning title=e2e 샌드박스 실행 ({port})::{summary}")
    for text in chosen:
        print(f"::warning title=e2e 서버 로그 ({port})::{text}")


if __name__ == "__main__":
    main()
