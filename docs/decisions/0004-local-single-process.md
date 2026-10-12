# ADR-0004: Docker 없는 로컬 단일 프로세스, LLM은 Gemini만

- 상태: 채택
- 날짜: 2026-10-12
- 대체: ADR-0002 D2'의 docker compose 배포, ADR-0003의 "Anthropic 어댑터 유지" 항목, 설계 v2의 서비스 7개 구조. ADR-0001의 D3(기능 복제), D4(Dify 의존 없음), D5(단계 순서)와 ADR-0002의 D1'(본인 1명), D3'(GitHub 코드 보존용)은 유지한다.
- 배경: PC 메모리가 16GB인데 Docker를 켜기 전부터 95%를 쓰고 있다. WSL2 VM을 하나 더 띄울 여유가 없다. 사용자가 로컬 전용으로 재설계하기로 했고, LLM은 Gemini만 쓰기로 했다.

## 결정

### D1. 실행 형태
- 파이썬 프로세스 1개가 전부다. uvicorn + FastAPI 안에 API, 에이전트 러너(asyncio 태스크), 빌드된 화면의 정적 서빙이 들어간다.
- 127.0.0.1:8080 바인드, TLS 없음. 시작 명령은 하나(`uv run platform serve`). 끄면 백그라운드에 남는 것이 없다.
- 플랫폼 메모리 목표는 300MB 이하(브라우저 탭 제외).

### D2. 저장과 큐
- SQLite 파일 하나(`data/app.db`, WAL 모드). SQLAlchemy 2 async + Alembic.
- 파일, 스냅샷, 업로드는 `data/storage/` 폴더.
- 런 큐는 asyncio.Queue, 런 이벤트는 메모리 링버퍼 + SQLite 영속. 브라우저 전달은 SSE 그대로.
- Postgres, Redis는 쓰지 않는다.

### D3. LLM은 Gemini만
- Gemini의 OpenAI 호환 엔드포인트를 openai SDK로 호출한다. 키 8개 순환, 모델 폴백 gemini-3.8-flash → 3.7 → 3.6(ADR-0003의 로테이션·폴백 규칙 유지).
- Anthropic 어댑터와 관련 설정은 삭제한다. 모델 호출 인터페이스는 남기되 구현은 Gemini 하나다.

### D4. 샌드박스
- `SandboxBackend` 인터페이스 하나에 구현을 둔다. 기본은 "로컬 작업공간" 백엔드다.
  - 세션마다 `data/workspaces/<세션id>` 폴더. 파일 도구는 이 폴더 밖 접근을 경로 정규화로 차단한다.
  - 셸은 PowerShell. 작업 폴더 고정, 타임아웃, Windows Job Object로 프로세스 트리 종료와 메모리 상한.
  - 파이썬은 `python -I`로 실행하고 패키지는 세션별 폴더에 `pip --target`으로 설치한다.
  - `http_request`는 파이썬 httpx로 호출하며 도메인 허용 목록을 적용한다.
  - 한계: 셸이 직접 여는 네트워크는 막지 못한다. 그래서 승인 모드 기본값은 `all_shell`(셸·파이썬 실행 전에 사용자가 승인)이다.
  - 스냅샷은 작업공간 폴더의 zip.
  - 샌드박스 런타임은 파이썬만 제공한다. Node는 넣지 않는다.
- E2B 백엔드는 인터페이스만 맞춰 두고 구현은 나중에 한다. Docker 백엔드는 Docker를 쓸 수 있게 되면 추가한다.

### D5. 프론트, 인증
- Vite + React + TypeScript는 그대로. 빌드 결과물을 FastAPI가 `/`에서 서빙하고, 개발 중에는 vite dev 서버가 `/api`를 8080으로 프록시한다.
- 로컬 계정 1개, 비밀번호 로그인, 쿠키 세션은 SQLite에 저장. 서비스 API는 Bearer API 키.

### D6. 제거와 유지
- 제거: Docker, compose, Postgres, Redis, nginx, squid, sandboxd 컨테이너, sandbox-manager 서비스, egress 프록시, Anthropic 어댑터.
- 유지: M1의 `apps/api`, `apps/web` 뼈대, 설정 로딩, 로그, 헬스체크, CI의 python·web 잡. `infra/`는 삭제하고 CI의 compose 잡도 뺀다.

## 영향
- 설계 문서를 v3로 개정한다. 마일스톤도 로컬 모드 기준으로 다시 나눈다.
- Docker가 가능해지면 샌드박스 백엔드만 추가하면 되도록 인터페이스를 지킨다.
