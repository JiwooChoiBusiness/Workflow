# Workflow

Dify 같은 LLM 에이전트·워크플로우 플랫폼을 혼자 쓰려고 직접 만드는 프로젝트다. 내 PC(Windows 11)에서 파이썬 프로세스 하나로 돌리는 걸 기준으로 하고, 이 저장소는 코드 보존용이다.

백엔드는 M4까지 있다. 계정 로그인, SQLite 스키마, 로컬 샌드박스 백엔드, Gemini 어댑터가 들어갔고 에이전트 실행 루프와 화면은 아직이다.

- `docs/research/dify-analysis.md` : Dify를 뜯어본 기록
- `docs/decisions/` : 범위, 실행 방식 같은 결정 기록
- `docs/design/phase1-agent-sandbox.md` : 1단계(에이전트 샌드박스) 설계

진행 순서는 에이전트 샌드박스, 워크플로우 엔진, RAG 순이다.

`.claude/`에는 Claude Code 작업 설정이 들어 있다. 설계와 구현은 Claude와 같이 하되 결정과 커밋은 내가 한다.

## 개발

Python 3.12와 [uv](https://docs.astral.sh/uv/), Node 24가 필요하다. Docker는 쓰지 않는다. pnpm은 corepack으로 부른다(`corepack pnpm ...`). uv가 PATH에 없으면 `python -m uv`로 불러도 된다.

처음 한 번 `.env`를 만들고 의존성을 받는다. `ENCRYPTION_KEY`는 `.env.example` 주석에 있는 명령으로 만들고, `GEMINI_API_KEYS`에 키를 넣는다. `DATA_DIR`은 `C:\wf-data`처럼 짧은 경로로 두는 게 좋다(비우면 `./data`).

```bash
cp .env.example .env
uv sync
corepack pnpm --dir apps/web install
```

기동. 브라우저 주소는 http://127.0.0.1:8500 이고, Ctrl+C로 끄면 남는 프로세스가 없다.

```bash
uv run platform serve        # --reload는 코드가 바뀔 때 다시 뜨지만 Windows에선 도구 실행이 안 된다
uv run platform doctor       # 셸, 파이썬과 pip, 데이터 경로, 실행 정책, 포트 점검
bash scripts/smoke.sh        # 떠 있는 서버 기본 확인
python scripts/memcheck.py   # 서버를 따로 띄워 메모리(RSS) 측정
```

화면은 `corepack pnpm --dir apps/web build`로 빌드해 두면 서버가 `apps/web/dist`를 서빙한다. 화면을 고치는 중에는 `corepack pnpm --dir apps/web dev`(5173)를 쓰면 되고, `/api` 요청은 127.0.0.1:8500으로 넘어간다.

테스트와 린트는 저장소 루트에서 돌린다.

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
corepack pnpm --dir apps/web lint && corepack pnpm --dir apps/web typecheck && corepack pnpm --dir apps/web test
```

`uv run pytest`는 Windows 전용(`windows`)과 실제 Gemini를 부르는(`live`) 테스트를 빼고 돈다. 앞의 것은 `uv run pytest -m windows`, 뒤의 것은 `.env`의 키로 `uv run pytest -m live`로 따로 돌린다.
