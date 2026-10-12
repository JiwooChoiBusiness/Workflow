# Workflow

Dify 같은 LLM 에이전트·워크플로우 플랫폼을 혼자 쓰려고 직접 만드는 프로젝트다. 내 PC의 Docker Desktop에서 돌리는 걸 기준으로 하고, 이 저장소는 코드 보존용이다.

코드는 1단계 첫 마일스톤(M1) 뼈대까지 있다. 서비스들은 아직 헬스체크에만 응답한다.

- `docs/research/dify-analysis.md` : Dify를 뜯어본 기록
- `docs/decisions/` : 범위, 배포 방식 같은 결정 기록
- `docs/design/phase1-agent-sandbox.md` : 1단계(에이전트 샌드박스) 설계

진행 순서는 에이전트 샌드박스, 워크플로우 엔진, RAG 순이다.

`.claude/`에는 Claude Code 작업 설정이 들어 있다. 설계와 구현은 Claude와 같이 하되 결정과 커밋은 내가 한다.

## 개발

Docker Desktop, Python 3.12와 [uv](https://docs.astral.sh/uv/), Node 24가 필요하다. pnpm은 corepack으로 부른다(`corepack pnpm ...`).

처음 한 번 `.env`를 만든다. 비밀값 세 개는 `.env.example` 주석에 있는 명령으로 만든다. `.env`가 이미 있으면 `.env.example`에서 빠진 항목만 옮긴다.

```bash
cp .env.example .env
```

기동과 확인. 브라우저 주소는 http://127.0.0.1:8080 이다.

```bash
bash scripts/dev-up.sh   # 전체 기동. api와 runner는 소스 마운트 + 자동 리로드
bash scripts/smoke.sh    # 서비스별 헬스체크
```

compose를 직접 부를 때는 저장소 루트에서 `--env-file .env`를 붙인다. compose 파일이 `infra/docker`에 있어서 이걸 빼면 루트 `.env`의 포트와 Postgres 값이 치환되지 않는다.

```bash
docker compose --env-file .env -f infra/docker/compose.yaml up -d --build
docker compose --env-file .env -f infra/docker/compose.yaml down
```

백엔드 테스트와 린트(`apps/sandboxd`도 같다):

```bash
cd apps/api
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

프론트엔드:

```bash
cd apps/web
corepack pnpm install
corepack pnpm dev        # 5173. /api 요청은 gateway로 넘긴다
corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test && corepack pnpm build
```

샌드박스 이미지는 `bash infra/sandbox-image/build.sh`로 따로 만든다. M3부터 쓴다.
