# 플랫폼 서버를 따로 띄워 /healthz의 rss_mb를 1초마다 재고 최댓값을 출력한다.
# 최댓값이 MEMORY_BUDGET_MB(기본 300)를 넘으면 종료 코드 1이다.
# 지금은 유휴 상태만 잰다. 런을 돌리면서 재는 시나리오는 M10에서 붙인다.
#
#   python scripts/memcheck.py [--seconds 10]
#
# 서버 의존성 없이 표준 라이브러리만 쓴다. 서버는 uv sync로 만든 .venv의 platform 명령으로 띄우고,
# 이미 떠 있는 서버나 실제 데이터와 겹치지 않게 빈 포트와 임시 데이터 폴더를 쓴다.
import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
READY_TIMEOUT_SEC = 60
STOP_TIMEOUT_SEC = 15


def _platform_command() -> str | None:
    venv = str(ROOT / ".venv")
    scripts = sysconfig.get_path("scripts", "venv", vars={"base": venv, "platbase": venv})
    return shutil.which("platform", path=scripts)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthz(port: int) -> dict[str, Any] | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        body = response.read()
    except OSError:
        return None
    finally:
        conn.close()
    if response.status != 200:
        return None
    return json.loads(body)


def _sample(server: subprocess.Popen[bytes], port: int, seconds: int) -> list[float] | None:
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while _healthz(port) is None:
        if server.poll() is not None or time.monotonic() > deadline:
            return None
        time.sleep(0.2)
    samples: list[float] = []
    for _ in range(seconds):
        body = _healthz(port)
        if body is not None and body.get("rss_mb") is not None:
            samples.append(float(body["rss_mb"]))
        time.sleep(1)
    return samples


def _stop(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    # Windows에서는 SIGINT를 보낼 수 없어 Ctrl+Break를 보낸다. 서버를 새 프로세스 그룹으로
    # 띄웠으므로 이 스크립트는 받지 않는다.
    try:
        server.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))
        server.wait(timeout=STOP_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired):
        server.kill()
        server.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="플랫폼 서버의 RSS를 1초마다 재고 최댓값을 출력한다"
    )
    parser.add_argument("--seconds", type=int, default=10, help="샘플링 시간(초). 기본 10")
    args = parser.parse_args(argv)
    budget_mb = float(os.environ.get("MEMORY_BUDGET_MB") or 300)

    command = _platform_command()
    if command is None:
        print("platform 명령이 없다. 저장소 루트에서 uv sync를 먼저 실행한다.", file=sys.stderr)
        return 2

    port = _free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="wf-memcheck-"))
    env = {**os.environ, "SERVER_PORT": str(port), "DATA_DIR": str(data_dir), "LLM_FAKE": "true"}
    output = data_dir / "server.out"
    try:
        with output.open("wb") as out:
            server = subprocess.Popen(
                [command, "serve"],
                cwd=ROOT,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        try:
            samples = _sample(server, port, args.seconds)
        finally:
            _stop(server)
        if not samples:
            print("서버가 응답하지 않았다. 서버 출력 끝부분:", file=sys.stderr)
            tail = output.read_bytes()[-3000:].decode("utf-8", errors="replace")
            print(tail, file=sys.stderr)
            return 2
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    peak = max(samples)
    print(
        f"RSS 최대 {peak:.1f}MB, 처음 {samples[0]:.1f}MB, 마지막 {samples[-1]:.1f}MB "
        f"(샘플 {len(samples)}개, 예산 {budget_mb:.0f}MB)"
    )
    return 1 if peak > budget_mb else 0


if __name__ == "__main__":
    sys.exit(main())
