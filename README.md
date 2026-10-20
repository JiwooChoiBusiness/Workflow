# Workflow_Jiwoo Choi

n8n 형식의 LLM 에이전트 & 워크플로우 플랫폼을 혼자 쓰려고 직접 만드는 프로젝트다. 내 PC(Windows 11)에서 파이썬 프로세스 하나로 돌리는 걸 기준으로 하고, 이 저장소는 코드 보존용이다.

1단계(에이전트 샌드박스)는 끝났다. 계정 로그인, 로컬 샌드박스와 실행 승인, Gemini 어댑터, 에이전트 빌더와 미리보기, 발행(버전관리용 내부 커밋) & 웹앱 & 서비스 API, 로그 & 작업공간 & 사용량 화면이 있고, 백업 명령과 운영 문서도 갖췄다.

2단계(워크플로우)도 끝났다. 캔버스에서 워크플로우와 챗플로우를 편집하고 실행·발행하며(웹앱과 서비스 API 포함), YAML(DSL)로 내보내고 가져온다(Dify DSL은 일부만). 노드는 LLM·조건 분기·반복·코드·HTTP 요청 같은 기본 노드에 더해 루프, 리스트 연산, 문서 추출(pdf·docx 포함), 내장 도구 호출, 사람 입력(폼으로 런을 멈추고 답을 받음)이 있다. 에이전트가 다른 에이전트를 도구로 부르는 지휘자 모드도 들어갔다.

남은 일은 3단계 RAG(아직 설계 전)와 2단계에서 미룬 두 가지다. 에이전트 노드의 '대화마다 같은 세션' 옵션은 아직 '실행마다 새 세션'처럼 돌고, 편집기의 실행 탭은 새로 고치면 진행 중이던 런에 다시 붙지 않는다(실행 기록 화면에서는 이어 받는다). 마일스톤별 기록은 `docs/progress.md`에 있다.

- `docs/research/dify-analysis.md` : n8n 중 Dify를 뜯어본 기록(참조용)
- `docs/decisions/` : 범위, 실행 방식 같은 결정 기록. 2단계는 ADR 0005(워크플로우 엔진)와 0006(지휘자 에이전트)
- `docs/design/phase1-agent-sandbox.md` : 1단계(에이전트 샌드박스) 설계
- `docs/design/phase2-workflow.md` : 2단계(워크플로우 엔진과 캔버스) 설계
- `docs/design/agent-orchestration.md` : 지휘자 에이전트(에이전트를 도구로 부르는 모드) 설계
- `docs/manual.md` : 사용 설명서. 화면에서 에이전트와 워크플로우를 만들고 발행해 쓰는 과정
- `docs/progress.md` : 마일스톤별 진행 기록
- `docs/ops/` : [설치](docs/ops/install.md), [운영](docs/ops/operations.md), [백업과 복구](docs/ops/backup-restore.md), [보안 확인](docs/ops/security.md), [샌드박스 Windows 검증](docs/ops/sandbox-windows-check.md)

순서는 에이전트 샌드박스, 워크플로우 엔진, RAG이고, 그 뒤에 도구 선택지를 넓히는 고도화 단계를 둘 생각이다. 3단계 설계는 아직 시작하지 않았다.

`.claude/`에는 Claude Code 작업 설정이 들어 있다. 설계와 구현은 Claude와 같이 하되 결정과 커밋은 내가 한다. 라이선스는 Apache 2.0이다.

## 구조

- `apps/api/app/` : FastAPI 서버 하나가 API, 러너, 화면 서빙을 다 한다. `agent/`는 에이전트 루프와 도구, `sandbox/`는 세션별 작업공간과 PowerShell 실행·Job Object, `llm/`은 Gemini 어댑터와 키 순환, `workflow/`는 그래프 엔진·노드·DSL 변환, `runner/`는 실행 큐와 재기동 복구, `streaming/`은 이벤트 링버퍼와 SSE, `services/`와 `api/`(console·web·v1)는 그 위의 서비스와 라우터, `db/`는 SQLAlchemy 모델과 리포지토리다.
- `apps/api/migrations/` : Alembic 마이그레이션. 서버가 뜰 때 자동으로 적용된다.
- `apps/api/tests/` : pytest. `unit`, `integration`, `windows`(Windows 전용)와 픽스처.
- `apps/web/src/` : React 화면. `app/`(라우트와 페이지), `components/`(builder·chat·flow·logs·ui), `features/`(기능별 API 호출과 상태), `api/`(서버 OpenAPI와 이벤트 스키마에서 만든 타입), `locales/ko/`(화면 문구).
- `apps/web/e2e/` : Playwright 브라우저 스모크.
- `packages/event-schema/` : 런 이벤트 JSON 스키마. 서버가 만들고 화면 타입을 여기서 생성한다.
- `scripts/` : 스모크, 메모리 측정, 타입 생성.

## 개발

Python 3.12와 [uv](https://docs.astral.sh/uv/), Node 24가 필요하다. Docker는 쓰지 않는다. pnpm은 corepack으로 부른다(`corepack pnpm ...`). uv가 PATH에 없으면 `python -m uv`로 불러도 된다.

처음 한 번 `.env`를 만들고 의존성을 받는다. `ENCRYPTION_KEY`는 `.env.example` 주석에 있는 명령으로 만들고, `GEMINI_API_KEYS`에 키를 넣는다. `DATA_DIR`은 `C:\wf-data`처럼 짧은 경로로 두는 게 좋다(비우면 `./data`).

```bash
cp .env.example .env
uv sync
corepack pnpm --dir apps/web install
```

기동. 브라우저 주소는 http://127.0.0.1:8500 이고, Ctrl+C로 끄면 남는 프로세스가 없다. 발행한 에이전트는 빌더의 API 탭에서 웹앱 공개를 켜면 `http://127.0.0.1:8500/apps/<슬러그>`에서 대화 화면으로 열리고, 워크플로우는 편집기의 API 탭에서 켜면 `/flows/<슬러그>`로 열린다.

```bash
uv run platform serve        # --reload를 붙이면 코드가 바뀔 때 다시 뜬다(개발용)
uv run platform doctor       # 셸, 파이썬과 pip, 데이터 경로, 실행 정책, 포트, 암호화 키 점검
uv run platform backup       # DATA_DIR을 backups/<시각>/에 복사(서버가 떠 있어도 된다)
bash scripts/smoke.sh        # 떠 있는 서버 기본 확인
python scripts/memcheck.py   # 서버를 따로 띄워 메모리(RSS) 측정
```

화면은 `corepack pnpm --dir apps/web build`로 빌드해 두면 서버가 `apps/web/dist`를 서빙한다. 화면을 고치는 중에는 `corepack pnpm --dir apps/web dev`(5173)를 쓰면 되고, `/api` 요청은 127.0.0.1:8500으로 넘어간다.

서비스 API는 발행한 에이전트나 워크플로우·챗플로우의 API 키로 `bash scripts/smoke-api.sh <API키>`를 돌려 훑는다. 키 종류는 `/api/v1/info`로 알아내 그에 맞는 경로를 부르고(워크플로우 입력은 `SMOKE_INPUTS`), 실제 Gemini를 몇 번 부르며, 승인 모드가 all_shell이면 승인을 스크립트가 대신 보낸다. 8500이 아닌 서버는 `SERVER_PORT=8502 bash scripts/smoke-api.sh <API키>`처럼 포트를 주고, `SMOKE_RATE_LIMIT=1`을 붙이면 마지막에 분당 60회 제한의 429까지 확인한다(그 키는 1분 동안 막힌다).

API가 바뀌면 서버를 띄운 채 `bash scripts/gen-api-types.sh`로 화면의 API 타입(`apps/web/src/api/schema.d.ts`)을 다시 만든다. 채팅 패널은 개발 서버의 `/dev/chat-fixture`에서 녹음된 런 이벤트로 확인하고, 브라우저 스모크(`corepack pnpm --dir apps/web e2e`)는 8502에 따로 띄운 시험용 서버와 설치된 Edge로 돈다(서버가 없으면 건너뛴다).

테스트와 린트는 저장소 루트에서 돌린다.

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
corepack pnpm --dir apps/web lint && corepack pnpm --dir apps/web typecheck && corepack pnpm --dir apps/web test
```

`uv run pytest`는 Windows 전용(`windows`)과 실제 Gemini를 부르는(`live`) 테스트를 빼고 돈다. 앞의 것은 `uv run pytest -m windows`, 뒤의 것은 `.env`의 키로 `uv run pytest -m live`로 따로 돌린다. GitHub Actions도 같은 검사를 돌린다. pytest는 Linux와 Windows에서, 웹 검사와 빌드는 Linux에서, 브라우저 스모크는 windows-latest에 설치된 Edge로 돈다(`.github/workflows/ci.yml`).

검증은 순서대로 돌린다. pytest, ruff·pyright, 웹 lint·typecheck·test, build 순으로 하나가 끝난 뒤에 다음을 시작하고, 둘 이상을 동시에 띄우지 않는다. e2e는 build를 마치고 시험용 서버를 띄운 뒤에만 돌린다. 이 PC는 메모리가 빠듯해서 겹쳐 돌리면 vitest나 build가 `Out of memory`로 죽는다.
