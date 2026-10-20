# 플랫폼 서버를 따로 띄워 /healthz의 rss_mb를 0.5초마다 잰다(설계 2.7). 유휴 상태를 잰 뒤 가짜
# 어댑터로 셸 도구를 쓰는 런 1개를 돌리고, 이어서 워크플로우 미리보기 런 1개(llm, code, 원소 20개
# iteration 안의 code, txt 파일을 읽는 document_extractor)와 챗플로우 미리보기 대화 3턴(llm 메모리,
# answer)을 돌린 다음(2단계 8장), 런이 끝난 뒤 다시 같은 시간만큼 잰다. 런 중 구간은 초기 설정·
# 로그인부터 run.completed까지, 워크플로우 런 중 구간은 워크플로우를 만들 때부터 workflow.completed
# 까지, 챗플로우 구간은 챗플로우를 만들 때부터 세 번째 턴의 workflow.completed까지다.
# 최댓값이 MEMORY_BUDGET_MB(기본 300)를 넘으면 종료 코드 1, 서버가 뜨지 않거나 런이 succeeded로
# 끝나지 않으면 2다.
#
#   python scripts/memcheck.py [--seconds 5] [--idle-only]
#
# 서버 의존성 없이 표준 라이브러리만 쓴다. 서버는 uv sync로 만든 .venv의 platform 명령으로 띄우고,
# 이미 떠 있는 서버나 실제 데이터와 겹치지 않게 빈 포트와 임시 데이터 폴더를 쓴다. 셸은 서버가
# 플랫폼에 맞춰 고르므로(Windows는 PowerShell, 그 밖은 bash) 환경변수를 더 줄 필요가 없다.
import argparse
import http.client
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
READY_TIMEOUT_SEC = 60
STOP_TIMEOUT_SEC = 15
RUN_TIMEOUT_SEC = 120
SAMPLE_INTERVAL_SEC = 0.5
# 가짜 모델이 스텝마다 쉬는 초. 셸 호출과 답으로 스텝이 둘이라 런이 샘플 간격보다 충분히 길어진다.
FAKE_DELAY_SEC = "1"
EMAIL = "memcheck@example.com"
PASSWORD = "memcheck-password"  # noqa: S105  임시 DATA_DIR에만 만드는 계정
PHASES = {
    "idle": "유휴",
    "run": "런 중",
    "workflow": "워크플로우 런 중",
    "chat": "챗플로우 3턴 중",
    "after": "런 뒤",
}
ITERATION_ITEMS = 20
CHAT_TURNS = 3
# document_extractor가 서버 안에서 읽는 txt(pdf·docx와 달리 파서 설치가 없다).
DOC_TEXT = "메모리 확인용 문서\n" * 200
WORKFLOW_GRAPH = {
    "nodes": [
        {
            "id": "start",
            "type": "start",
            "config": {
                "fields": [{"name": "q", "type": "string"}, {"name": "doc", "type": "file"}]
            },
        },
        {
            "id": "llm_1",
            "type": "llm",
            "config": {"prompt": [{"role": "user", "text": "{{ start.q }}"}]},
        },
        {
            "id": "code_1",
            "type": "code",
            "config": {
                "code": f"def main():\n    return {{'items': list(range({ITERATION_ITEMS}))}}\n",
                "outputs": [{"name": "items", "type": "array[number]"}],
            },
        },
        {
            "id": "iteration_1",
            "type": "iteration",
            "config": {"input_var": "code_1.items", "output_var": "code_2.v"},
        },
        {"id": "iteration_start_1", "type": "iteration_start", "parent_id": "iteration_1"},
        {
            "id": "code_2",
            "type": "code",
            "parent_id": "iteration_1",
            "config": {
                "code": "def main(item):\n    return {'v': item * 2}\n",
                "inputs": [{"name": "item", "var": "iteration_start_1.item"}],
                "outputs": [{"name": "v", "type": "number"}],
            },
        },
        {"id": "doc_1", "type": "document_extractor", "config": {"file_var": "start.doc"}},
        {
            "id": "end_1",
            "type": "end",
            "config": {
                "outputs": [
                    {"name": "answer", "var": "llm_1.text"},
                    {"name": "doubled", "var": "iteration_1.output"},
                    {"name": "text", "var": "doc_1.text"},
                ]
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "llm_1"},
        {"id": "e2", "source": "llm_1", "target": "code_1"},
        {"id": "e3", "source": "code_1", "target": "iteration_1"},
        {"id": "e4", "source": "iteration_1", "target": "doc_1"},
        {"id": "e5", "source": "doc_1", "target": "end_1"},
        {"id": "e6", "source": "iteration_start_1", "target": "code_2"},
    ],
}
CHAT_GRAPH = {
    "nodes": [
        {"id": "start", "type": "start", "config": {"fields": []}},
        {
            "id": "llm_1",
            "type": "llm",
            "config": {
                "prompt": [{"role": "user", "text": "{{ sys.query }}"}],
                "memory": {"enabled": True, "window_turns": 2},
            },
        },
        {"id": "answer_1", "type": "answer", "config": {"text": "{{ llm_1.text }}"}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "llm_1"},
        {"id": "e2", "source": "llm_1", "target": "answer_1"},
    ],
}


class ScenarioError(Exception):
    pass


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


class Sampler(threading.Thread):
    """SAMPLE_INTERVAL_SEC마다 rss_mb를 그때의 구간(phase) 이름으로 모은다."""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.phase = "idle"
        self.samples: dict[str, list[float]] = {phase: [] for phase in PHASES}
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.is_set():
            phase = self.phase
            body = _healthz(self.port)
            if body is not None and body.get("rss_mb") is not None:
                self.samples[phase].append(float(body["rss_mb"]))
            self._done.wait(SAMPLE_INTERVAL_SEC)

    def stop(self) -> None:
        self._done.set()
        self.join()


class Console:
    """콘솔 API를 부르는 최소 클라이언트. 로그인 쿠키는 Set-Cookie에서 꺼내 다시 보낸다."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookies: dict[str, str] = {}

    def _send(self, conn: http.client.HTTPConnection, method: str, path: str, body: Any) -> None:
        # 변경 요청의 CSRF 검사는 JSON 본문이면 통과한다. Origin은 브라우저가 보낼 때만 본다.
        headers = {"Content-Type": "application/json"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        conn.request(method, path, body=json.dumps(body).encode(), headers=headers)

    def _keep_cookies(self, response: http.client.HTTPResponse) -> None:
        for header in response.headers.get_all("Set-Cookie") or []:
            for name, morsel in SimpleCookie(header).items():
                self.cookies[name] = morsel.value

    def upload(self, path: str, filename: str, data: bytes) -> dict[str, Any]:
        """multipart 한 파일. 업로드 경로의 CSRF 검사는 X-Requested-With: fetch로 통과한다."""
        boundary = uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        )
        body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Requested-With": "fetch",
            "Cookie": "; ".join(f"{name}={value}" for name, value in self.cookies.items()),
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        if response.status != 201:
            detail = payload[:300].decode("utf-8", errors="replace")
            raise ScenarioError(f"POST {path} 응답이 {response.status}다: {detail}")
        return json.loads(payload)

    def call(self, method: str, path: str, body: Any, *, expect: int) -> dict[str, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            self._send(conn, method, path, body)
            response = conn.getresponse()
            data = response.read()
            self._keep_cookies(response)
        finally:
            conn.close()
        if response.status != expect:
            detail = data[:300].decode("utf-8", errors="replace")
            raise ScenarioError(f"{method} {path} 응답이 {response.status}다: {detail}")
        return json.loads(data) if data else {}

    def stream_message(self, session_id: str, text: str) -> str:
        """메시지를 보내고 SSE를 run.completed까지 읽어 런의 끝 상태를 돌려준다."""
        path = f"/api/console/sessions/{session_id}/messages"
        return self.stream_until(path, {"text": text}, "run.completed")

    def stream_until(self, path: str, body: Any, terminal: str) -> str:
        """POST 응답 SSE를 terminal 이벤트까지 읽어 그 data.status를 돌려준다."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=RUN_TIMEOUT_SEC)
        try:
            self._send(conn, "POST", path, body)
            response = conn.getresponse()
            if response.status != 200:
                detail = response.read()[:300].decode("utf-8", errors="replace")
                raise ScenarioError(f"POST {path} 응답이 {response.status}다: {detail}")
            event = ""
            while line := response.readline():
                field = line.decode("utf-8").rstrip("\r\n")
                if field.startswith("event: "):
                    event = field.removeprefix("event: ")
                elif field.startswith("data: ") and event == terminal:
                    return str(json.loads(field.removeprefix("data: "))["data"]["status"])
        finally:
            conn.close()
        raise ScenarioError(f"{terminal}이 오기 전에 스트림이 끝났다")


def _run_agent(console: Console) -> str:
    account = {"email": EMAIL, "password": PASSWORD}
    console.call("POST", "/api/console/setup", account, expect=201)
    console.call("POST", "/api/console/auth/login", account, expect=200)
    agent = console.call("POST", "/api/console/agents", {"name": "memcheck"}, expect=201)
    session = console.call(
        "POST", f"/api/console/agents/{agent['id']}/preview/sessions", {}, expect=201
    )
    # 새 에이전트의 승인 모드는 all_shell이다. 셸 승인에서 멈추지 않게 세션 자동 승인을 켠다.
    console.call(
        "PATCH", f"/api/console/sessions/{session['id']}", {"approval_override": "off"}, expect=200
    )
    return console.stream_message(session["id"], "메모리 확인")


def _save_draft(console: Console, base: str, graph: dict[str, Any]) -> None:
    draft = console.call("GET", f"{base}/draft", None, expect=200)
    body = {
        "graph": graph,
        "env_vars": [],
        "conversation_vars": [],
        "settings": draft["settings"],
        "updated_at": draft["updated_at"],
    }
    saved = console.call("PUT", f"{base}/draft", body, expect=200)
    errors = [issue for issue in saved["issues"] if issue["level"] == "error"]
    if errors:
        raise ScenarioError(f"초안 검증 오류: {errors[0]['message']}")


def _run_workflow(console: Console) -> str:
    """WORKFLOW_GRAPH 초안을 저장하고 txt를 올려 미리보기 런을 workflow.completed까지 받는다."""
    workflow = console.call("POST", "/api/console/workflows", {"name": "memcheck"}, expect=201)
    base = f"/api/console/workflows/{workflow['id']}"
    _save_draft(console, base, WORKFLOW_GRAPH)
    uploaded = console.upload(f"{base}/files", "memo.txt", DOC_TEXT.encode("utf-8"))
    inputs = {"q": "메모리 확인", "doc": uploaded["id"]}
    return console.stream_until(f"{base}/draft/runs", {"inputs": inputs}, "workflow.completed")


def _run_chatflow(console: Console) -> str:
    """llm(메모리) → answer 챗플로우의 미리보기 대화에서 CHAT_TURNS턴을 차례로 보낸다."""
    workflow = console.call(
        "POST", "/api/console/workflows", {"name": "memcheck-chat", "kind": "chatflow"}, expect=201
    )
    base = f"/api/console/workflows/{workflow['id']}"
    _save_draft(console, base, CHAT_GRAPH)
    conversation = console.call("POST", f"{base}/preview/conversations", {}, expect=201)
    path = f"/api/console/wf-conversations/{conversation['id']}/messages"
    for turn in range(1, CHAT_TURNS + 1):
        status = console.stream_until(path, {"text": f"{turn}번째 질문"}, "workflow.completed")
        if status != "succeeded":
            return status
    return "succeeded"


def _measure(
    server: subprocess.Popen[bytes], port: int, seconds: float, *, idle_only: bool
) -> dict[str, list[float]]:
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while _healthz(port) is None:
        if server.poll() is not None:
            raise ScenarioError(f"서버가 종료 코드 {server.returncode}로 끝났다")
        if time.monotonic() > deadline:
            raise ScenarioError(f"서버가 {READY_TIMEOUT_SEC}초 안에 응답하지 않았다")
        time.sleep(0.2)
    sampler = Sampler(port)
    sampler.start()
    try:
        time.sleep(seconds)
        if not idle_only:
            sampler.phase = "run"
            console = Console(port)
            status = _run_agent(console)
            if status != "succeeded":
                raise ScenarioError(f"런이 {status}로 끝났다")
            sampler.phase = "workflow"
            status = _run_workflow(console)
            if status != "succeeded":
                raise ScenarioError(f"워크플로우 런이 {status}로 끝났다")
            sampler.phase = "chat"
            status = _run_chatflow(console)
            if status != "succeeded":
                raise ScenarioError(f"챗플로우 턴이 {status}로 끝났다")
            sampler.phase = "after"
            time.sleep(seconds)
    finally:
        sampler.stop()
    return sampler.samples


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


def _report(samples: dict[str, list[float]], phases: list[str], budget_mb: float) -> int:
    peaks = {phase: max(values) for phase, values in samples.items() if values}
    if not peaks:
        print("RSS 샘플을 하나도 얻지 못했다", file=sys.stderr)
        return 2
    peak = max(peaks.values())
    detail = ", ".join(
        f"{PHASES[phase]} {peaks[phase]:.1f}" if phase in peaks else f"{PHASES[phase]} -"
        for phase in phases
    )
    print(f"RSS 최대 {peak:.1f} MB ({detail})")
    counts = ", ".join(f"{PHASES[phase]} {len(samples[phase])}" for phase in phases)
    print(f"예산 {budget_mb:.0f} MB, 샘플 수({counts})", flush=True)
    if peak > budget_mb:
        print(f"RSS가 예산 {budget_mb:.0f} MB를 넘었다", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # CI(Windows)처럼 출력이 파이프면 기본 인코딩이 cp1252라 한글을 못 쓴다.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="플랫폼 서버의 RSS를 유휴·런 중·런 뒤로 나눠 재고 최댓값을 출력한다"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=5,
        help="유휴 상태를 재는 시간(초). 런 뒤에도 같은 시간만큼 잰다. 기본 5",
    )
    parser.add_argument("--idle-only", action="store_true", help="런 없이 유휴 상태만 잰다")
    args = parser.parse_args(argv)
    budget_mb = float(os.environ.get("MEMORY_BUDGET_MB") or 300)

    command = _platform_command()
    if command is None:
        print("platform 명령이 없다. 저장소 루트에서 uv sync를 먼저 실행한다.", file=sys.stderr)
        return 2

    port = _free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="wf-memcheck-"))
    env = {
        **os.environ,
        "SERVER_PORT": str(port),
        "DATA_DIR": str(data_dir),
        "LLM_FAKE": "true",
        "LLM_FAKE_DELAY_SEC": FAKE_DELAY_SEC,
    }
    output = data_dir / "server.out"
    error: str | None = None
    samples: dict[str, list[float]] = {}
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
            samples = _measure(server, port, args.seconds, idle_only=args.idle_only)
        except (ScenarioError, OSError) as exc:
            error = str(exc) or type(exc).__name__
        finally:
            _stop(server)
        if error is not None:
            print(f"측정하지 못했다: {error}. 서버 출력 끝부분:", file=sys.stderr)
            tail = output.read_bytes()[-3000:].decode("utf-8", errors="replace")
            print(tail, file=sys.stderr)
            return 2
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    phases = ["idle"] if args.idle_only else list(PHASES)
    return _report(samples, phases, budget_mb)


if __name__ == "__main__":
    sys.exit(main())
