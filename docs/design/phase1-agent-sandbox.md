# 1단계 설계: 에이전트 샌드박스

- 상태: 초안 v2 (개인용 재정의 반영, 2026-10-12)
- 변경 이력
  - v1 (2026-10-12): architect 초안. 사내 다인 사용 전제.
  - v1.1 (2026-10-12): 오케스트레이터 1차 검토. 3.8절 7번(sandbox-manager 포트 노출 방어) 추가.
  - v2 (2026-10-12): ADR-0002 반영. 사용자 본인 1명, Windows Docker Desktop 기본, gateway 127.0.0.1 바인드, GitHub은 코드 보존용. 초대·역할·감사 로그·엔드유저 제거, 동시 샌드박스 3.
- 날짜: 2026-10-12
- 상위 문서: `CLAUDE.md`, `docs/decisions/0001-scope-and-deployment.md`, `docs/decisions/0002-personal-use.md`, `docs/research/dify-analysis.md` (3.6, 3.7, 3.8, 4.2, 4.5, 4.7, 4.8, 8장)
- 대상 독자: 구현 담당(implementer). 이 문서의 결정은 그대로 따르고, 모호한 부분은 12장 미결 사항에 추가한 뒤 질문으로 보고한다.

## 0. 용어 정의

Dify 용어를 참고하되 우리 제품 용어를 아래와 같이 고정한다. 코드 식별자(테이블·모듈·API 경로)는 영문 용어를 쓴다.

| 우리 용어 (영문 식별자) | 정의 | Dify 대응 |
|---|---|---|
| 워크스페이스 (`workspace`) | 모든 자원의 최상위 소유 단위. 1단계는 1개만 존재하지만 모든 핵심 테이블이 `workspace_id`를 가진다 | Workspace/Tenant |
| 계정 (`account`) | 초기 설정에서 만드는 로컬 로그인 계정 1개. 역할 구분 없음 | Account |
| 에이전트 (`agent`) | 이름·아이콘·설명을 가진 "능력" 정의의 식별자. 실제 설정은 버전에 있다 | New Agent (Capability) |
| 에이전트 버전 (`agent_version`) | 지시문·모델·도구·스킬·파일·환경변수·샌드박스 설정의 불변 묶음. `draft`(에이전트당 1개, 수정 가능)와 `published`(번호 있는 불변 버전) | Draft / Published version |
| 스킬 (`skill`, `skill_version`) | `SKILL.md`와 참고 자료·스크립트를 담은 디렉터리 패키지. 라이브러리 스킬(워크스페이스 공유)과 임베디드 스킬(에이전트 전용) | Skills |
| 세션 (`session`) | 한 에이전트와의 대화 하나. 소스는 `preview`(빌더), `webapp`, `api`, `task`(2단계 워크플로우) | Conversation |
| 메시지 (`message`) | 세션 안의 사용자/어시스턴트 발화. 정규화된 블록 목록으로 저장 | Message |
| 런 (`run`) | 사용자 입력 하나(또는 과업 하나)를 처리하는 에이전트 루프 실행. 여러 스텝으로 구성 | (Dify의 message/agent 실행) |
| 스텝 (`step`) | 런 안에서 모델 호출 1회와 그에 따른 도구 호출들 | Iteration |
| 도구 호출 (`tool_call`) | 모델이 요청한 내장 도구 실행 1회 | Tool call |
| 런 이벤트 (`run_event`) | 런 중에 발생한 관측 가능한 사건. SSE로 스트리밍되고 DB에 기록 | SSE event / log |
| 샌드박스 (`sandbox_instance`) | 세션에 묶인 리눅스 컨테이너 1개. `sandboxd`가 안에서 명령·파일 API를 제공 | local_sandbox |
| 스냅샷 (`snapshot`) | 샌드박스 홈 디렉터리(`/home/agent`)의 tar.gz. `published`(버전 초기 상태)와 `session`(유휴 종료 시 보존) | Home directory snapshot |
| 웹앱 (`webapp`) | 발행된 에이전트의 채팅 화면. `/apps/<slug>` 경로, 같은 계정으로 로그인해 사용. 초안을 쓰는 미리보기와 분리 | WebApp |
| 서비스 API, API 키 (`api_key`) | 외부 프로그램과 2단계 엔진용 Bearer 키 인증 API. 키는 에이전트 1개에 묶임 | Service API |
| 외부 사용자 식별자 (`session.external_user`) | 서비스 API 호출자가 `user` 필드로 주는 선택 문자열. Dify 호환용이며 세션에 그대로 저장해 대화 목록 필터에 쓴다 | End user의 `user` |
| 과업 (`agent task`) | 2단계 워크플로우 노드가 에이전트에게 주는 1회성 작업: 입력 변수 + 지시 + 출력 스키마 | Task |

## 1. 요약과 완료 기준

### 1.1 요약

1단계는 "지시문·모델·도구·스킬을 가진 에이전트를 정의하고, 세션마다 격리된 리눅스 컨테이너 안에서 셸·파이썬·파일·HTTP 도구를 쓰는 에이전트 루프를 실행하며, 그 과정을 실시간으로 보여주고 기록하고, 웹앱과 서비스 API로 발행하는" 개인용 플랫폼을 만든다. 사용자는 작성자 본인 1명이고 실행 환경은 본인 PC의 Docker Desktop이다(ADR-0002). 서비스는 gateway(nginx+SPA), api, runner, sandbox-manager, egress-proxy, postgres, redis 7개를 docker compose 한 벌로 띄우며, LLM은 Anthropic과 OpenAI 호환 API 두 어댑터로 호출한다. 2단계 워크플로우가 에이전트를 노드로 쓸 수 있도록 "과업(Agent Task)" 실행 모델과 API를 1단계에 포함한다.

### 1.2 1단계 완료 기준 (수용 조건)

사용자가 직접 확인할 수 있는 조건이다. 모두 만족하면 1단계 완료로 본다.

| # | 수용 조건 | 확인 방법 |
|---|---|---|
| A1 | Windows 11 + Docker Desktop(또는 Linux)에서 `.env` 작성 후 `docker compose up -d` 한 번으로 전체가 기동되고, 브라우저 `http://127.0.0.1:8080`에서 초기 설정 화면(계정 생성)이 뜬다 | 설치 문서 따라 10분 내 완료 |
| A2 | 초기 설정에서 만든 계정으로 로그인·로그아웃하고 비밀번호를 변경한다. 로그인하지 않은 요청은 콘솔·웹앱 API에서 401을 받고, `/setup`은 계정이 생긴 뒤 404를 돌려준다 | 브라우저와 curl로 확인 |
| A3 | Anthropic 키와 OpenAI 호환 엔드포인트(base URL + 키)를 등록하고 "연결 테스트"로 모델 목록을 받아온다. 키는 DB에 평문으로 남지 않는다 | `psql`로 컬럼 확인 |
| A4 | 에이전트를 만들어 지시문·모델·도구를 설정하고 미리보기 채팅에서 "현재 디렉터리 파일을 보고 requests를 설치해 example.com 상태 코드를 알려줘" 같은 요청을 하면, 화면에 어시스턴트 텍스트가 토큰 단위로 흐르고 도구 호출 카드에 명령·stdout·종료 코드가 실시간으로 표시된다 | 미리보기 화면 |
| A5 | 에이전트가 만든 파일(예: CSV)이 "변경된 파일" 목록에 나타나고 다운로드된다. 샌드박스가 종료된 뒤에도 다운로드된다 | 다운로드 후 유휴 종료 대기 후 재다운로드 |
| A6 | `SKILL.md`를 포함한 zip을 라이브러리 스킬로 업로드하고 에이전트에 연결하면, 관련 요청 시 에이전트가 `skills/<slug>/SKILL.md`를 읽는 도구 호출이 로그에 나타나고 스킬의 스크립트를 실행한다 | 로그 화면 |
| A7 | 미리보기 샌드박스에 파일을 만들어 둔 뒤 "발행"하면 스냅샷이 생성되고, 발행 후 새 웹앱 세션의 첫 `ls`에서 그 파일이 보인다 | 웹앱 세션 |
| A8 | 발행된 에이전트의 웹앱 링크(`/apps/<slug>`)를 열어 발행 버전과 대화할 수 있다. 대화 목록이 남고 이어서 대화할 수 있으며, 초안을 쓰는 미리보기 세션과 분리된다 | 웹앱 화면 |
| A9 | API 키를 만들고 `curl -N -H "Authorization: Bearer ..." POST /api/v1/chat-messages`로 SSE 스트림을 받는다. 대화 목록·메시지 조회·중지 API가 동작한다 | curl 스크립트 `scripts/smoke-api.sh` |
| A10 | 로그 화면에서 세션(소스: preview/webapp/api 구분), 런, 스텝별 도구 호출 입출력, 토큰 사용량·추정 비용을 조회한다 | 로그 화면 |
| A11 | 샌드박스 안에서 `id`가 uid 1000 비루트이고, 포크 폭탄·메모리 폭식 명령이 컨테이너 안에서만 죽는다. `curl https://pypi.org`는 성공하고 `curl https://example.org`(허용 목록 외)와 `curl http://postgres:5432`는 실패한다 | 미리보기에서 명령 실행 |
| A12 | 설정한 유휴 시간(기본 30분) 뒤 샌드박스 컨테이너가 사라지고, 같은 세션에 다시 메시지를 보내면 이전 파일이 복원된 새 샌드박스에서 이어진다 | `docker ps`와 `ls` |
| A13 | `ask_user` 도구로 에이전트가 질문하면 런이 `waiting_input`으로 멈추고, 답을 보내면 이어서 실행된다 | 미리보기 |
| A14 | `POST /api/v1/agent-tasks`에 입력 변수·지시·JSON 출력 스키마를 보내면 스키마에 맞는 JSON 결과가 돌아온다 | curl |
| A15 | 백업 스크립트로 Postgres 덤프와 스토리지 볼륨을 받아 새 환경에 복원했을 때 에이전트·세션·스냅샷이 유지된다 | 운영 문서 절차 1회 수행 |

## 2. 시스템 아키텍처

### 2.1 서비스 구성

docker compose 한 벌(개발·운영 공용, 개발은 override 파일 추가)로 아래 7개 고정 서비스를 띄운다. 샌드박스 컨테이너는 sandbox-manager가 동적으로 만든다.

| 서비스 | 이미지 | 책임 | 네트워크 |
|---|---|---|---|
| `gateway` | nginx (빌드된 SPA 포함) | 정적 SPA 서빙, `/api/*`를 api로 프록시. SSE를 위해 버퍼링 끔. TLS 없음 | app-net, 호스트 `127.0.0.1:${GATEWAY_HTTP_PORT:-8080}`에만 바인드 |
| `api` | `platform-api` (apps/api) | 콘솔 API, 웹앱 API, 서비스 API, 내부 콜백 API. 런 생성·큐 투입, Redis 스트림을 SSE로 중계, 파일 업/다운로드 | app-net |
| `runner` | `platform-api` (entrypoint `python -m app.runner`) | 런 큐 소비, 에이전트 루프 실행(모델 호출, 도구 실행), 이벤트 발행·기록, 스냅샷 생성 요청 | app-net, sandbox-net |
| `sandbox-manager` | `platform-api` (entrypoint `python -m app.sandbox_manager`) | Docker 소켓을 가진 유일한 서비스. 샌드박스 생성·종료·목록·유휴 회수·동시 수 제한. 스냅샷 복원·추출을 sandboxd와 중계 | app-net, sandbox-net |
| `egress-proxy` | squid 6 | 샌드박스 아웃바운드 HTTP/HTTPS(CONNECT) 허용 목록 프록시. 사설 IP 대역 차단 | sandbox-net, egress-net |
| `postgres` | postgres:16 | 모든 영속 데이터 | app-net |
| `redis` | redis:7 | 런 큐, 런 이벤트 스트림(재생 버퍼), 취소 플래그, 하트비트, 로그인 세션, 레이트 리밋 | app-net |
| (동적) `sbx-<id>` | `platform-sandbox` (infra/sandbox-image) | 세션별 샌드박스. 안에서 `sandboxd`가 7000 포트로 명령·파일 API 제공 | sandbox-net만 |

네트워크 3개:
- `app-net`: 일반 브리지. 플랫폼 서비스끼리 통신.
- `sandbox-net`: `internal: true`(외부 라우트 없음). 샌드박스, runner, sandbox-manager, egress-proxy가 붙는다. 샌드박스는 이 네트워크로만 나갈 수 있어 egress-proxy를 거치지 않는 외부 통신이 불가능하다.
- `egress-net`: 일반 브리지. egress-proxy만 붙어 인터넷으로 나간다. egress-proxy는 `postgres`, `redis` 같은 이름을 해석할 수 없다.

볼륨: `pgdata`, `redisdata`, `storage`(api·runner·sandbox-manager가 `/data`로 마운트: 스냅샷·스킬 패키지·업로드·결과 파일), 샌드박스별 `sbx-<id>-home`(홈 디렉터리, 컨테이너 종료 시 삭제).

### 2.2 텍스트 다이어그램

```
 브라우저 (콘솔 SPA / 웹앱)            외부 프로그램 (curl, 2단계 워크플로우 엔진)
        │  HTTP 127.0.0.1, 쿠키 세션           │  HTTP 127.0.0.1, Bearer API 키
        ▼                                      ▼
 ┌──────────────────────── gateway (nginx, SPA 정적 파일) ────────────────────────┐
 │  /api/console/*  /api/web/*  /api/v1/*   (SSE: proxy_buffering off)           │
 └───────────────────────────────┬────────────────────────────────────────────────┘
                                 ▼
                    ┌─────────── api (FastAPI) ───────────┐
                    │ 인증·RBAC, CRUD, 런 생성, 파일       │
                    │ SSE 중계: XREAD run:{id}:events      │
                    └──────┬──────────────┬───────────────┘
            SQL            │              │ LPUSH runs:queue, SET run:{id}:cancel
        ┌──────────────────▼──┐      ┌────▼──────────────────────────────────┐
        │ postgres            │      │ redis                                 │
        │ 모든 영속 데이터     │      │ 큐·이벤트 스트림·플래그·로그인 세션      │
        └──────────▲──────────┘      └────▲──────────────────────────────────┘
                   │ SQL (이벤트 배치 기록)   │ BLMOVE runs:queue / XADD run:{id}:events
                   │                        │
             ┌─────┴────────────────────────┴─────┐          ┌──────────────────────┐
             │ runner (asyncio, N개 동시 런)        │──HTTPS──▶│ LLM API              │
             │ 에이전트 루프: 모델 어댑터 ↔ 도구 실행 │          │ Anthropic / OpenAI호환 │
             └─────┬───────────────────────┬───────┘          └──────────────────────┘
   HTTP(sandbox-net)│ create/destroy        │ exec, fs, http (sandbox-net, Bearer 토큰)
                    ▼                       ▼
     ┌──────────────────────┐     ┌───────────────────────────────┐    ┌───────────────┐
     │ sandbox-manager      │     │ sbx-<id> (세션별 컨테이너)      │    │ egress-proxy  │
     │ Docker 소켓 보유      │─────▶│  sandboxd :7000, uid 1000     │───▶│ squid 허용목록 │──▶ 인터넷
     │ 생성·종료·회수·제한    │ Docker│  /home/agent (볼륨)           │    │ 사설IP 차단    │
     └──────────────────────┘  API │  HTTP(S)_PROXY=egress-proxy   │    └───────────────┘
                                   └───────────────────────────────┘
          [app-net] ──────────────── [sandbox-net, internal] ──────────── [egress-net]
```

### 2.3 서비스 간 통신 방식

| 구간 | 방식 | 비고 |
|---|---|---|
| 브라우저 → api | HTTP(127.0.0.1) JSON + SSE(`fetch` 스트림) | 콘솔·웹앱은 httpOnly 쿠키 세션. 서비스 API는 Bearer |
| api → runner | Redis 리스트 큐 `runs:queue` (`LPUSH`/`BLMOVE`) | 런 레코드는 Postgres에 먼저 저장, 큐에는 `run_id`만 |
| runner → api(브라우저) | Redis Stream `run:{id}:events` (`XADD`, MAXLEN 10000, 종료 후 24h 만료) | api가 `XREAD BLOCK`으로 읽어 SSE로 중계. 늦게 붙은 클라이언트도 재생 가능 |
| api → runner (취소) | Redis 키 `run:{id}:cancel` + Pub/Sub `run:{id}:control` | runner는 스텝 경계와 도구 실행 중 주기적으로 확인 |
| runner/api → sandbox-manager | HTTP JSON (`http://sandbox-manager:7100`), 내부 공유 비밀 헤더 `X-Internal-Token` | 생성·조회·종료·스냅샷 |
| runner → sandboxd | HTTP JSON + NDJSON 스트림 (`http://sbx-<id>:7000`), `Authorization: Bearer <샌드박스별 토큰>` | 명령 실행 스트림, 파일 I/O, http_request 대행 |
| sandbox-manager → api | HTTP 콜백 `POST /internal/sandboxes/{id}/events` (`X-Internal-Token`) | 유휴 종료·OOM 등 상태 변경 통지 |
| runner → LLM | HTTPS (Anthropic SDK, OpenAI SDK) | 샌드박스는 LLM을 직접 호출하지 않는다 |

### 2.4 실시간 스트리밍 경로: SSE 채택

결정: 서버→브라우저 실시간 전달은 SSE(`text/event-stream`)로 한다. WebSocket은 쓰지 않는다.

근거:
- 브라우저→서버 방향은 모두 REST(메시지 전송, 중지, 질문 답변)로 충분하다. 양방향 소켓이 필요한 기능(2단계 실시간 협업 편집)은 1단계에 없다.
- SSE는 nginx 뒤에서 설정 한 줄(`proxy_buffering off`)로 동작하고, HTTP 인증(쿠키·Bearer)을 그대로 쓴다. WebSocket은 별도 업그레이드 경로·인증·하트비트 처리가 필요하다.
- Dify 서비스 API와 같은 형태(POST 응답이 SSE 스트림)라 외부 클라이언트 작성이 쉽다.
- 재접속: 이벤트의 `id`에 Redis Stream 엔트리 ID를 넣고, `GET /runs/{id}/events?after=<id>`로 끊긴 지점부터 다시 받는다. 브라우저는 `EventSource` 대신 `fetch` + `ReadableStream` 파서를 쓴다(POST와 커스텀 헤더 지원).

### 2.5 장기 실행 처리 방식

- 런은 수 분~수십 분 걸릴 수 있으므로 HTTP 요청 수명과 분리한다. api는 `run` 행을 `queued`로 만들고 큐에 넣은 뒤, 요청이 스트리밍이면 Redis Stream을 중계하고 아니면(`blocking`) 종료 이벤트까지 기다린다. 클라이언트가 끊어져도 runner는 계속 실행한다.
- runner는 asyncio 단일 프로세스에 `RUNNER_MAX_CONCURRENT_RUNS`(기본 3)개의 워커 태스크를 두고 `BLMOVE runs:queue runs:processing`으로 런을 가져간다. 각 런은 5초마다 `run:{id}:hb` 하트비트를 갱신한다.
- 장애 복구: runner 기동 시 `runs:processing`을 훑어 하트비트가 30초 이상 끊긴 런을 `failed(reason=runner_lost)`로 마감하고 `run.error` 이벤트를 발행한다. 사용자는 메시지를 다시 보내면 된다(런은 멱등하지 않으므로 자동 재실행하지 않는다).
- 사용자 입력 대기(`ask_user`, 위험 명령 승인)는 런을 `waiting_input` 상태로 끝내고 워커 슬롯을 반납한다. 답이 오면 같은 `run` 행을 다시 큐에 넣어 이어서 실행한다(4.9절).
- 유휴 샌드박스 회수, 만료 스냅샷 삭제, 오래된 이벤트 스트림 정리는 sandbox-manager와 api의 주기 태스크(asyncio 루프, Redis 락으로 단일 실행 보장)로 처리한다. 별도 스케줄러 서비스는 두지 않는다.

## 3. 샌드박스 설계

### 3.1 구조 결정: 컨테이너 안의 `sandboxd` + 바깥의 `sandbox-manager`

결정: 샌드박스 컨테이너 안에 우리가 만든 작은 HTTP 서버 `sandboxd`(apps/sandboxd, Python)를 PID 1 아래 자식으로 띄우고, runner는 명령 실행·파일 I/O를 모두 sandboxd HTTP API로 수행한다. Docker API는 sandbox-manager만 쓰며 생성·종료·목록·통계에 한정한다.

근거:
- `docker exec`를 매 도구 호출마다 쓰면 스트리밍·stdin·타임아웃·kill을 Docker 소켓 프로토콜 위에서 다뤄야 하고, 그 소켓을 runner에 노출해야 한다. sandboxd 방식은 Docker 소켓 보유 서비스를 하나로 줄이고(3.8절), 도구 API를 우리가 정의한 JSON으로 고정한다.
- 나중에 컨테이너 대신 Firecracker VM이나 외부 샌드박스 서비스로 바꿔도 runner 쪽 인터페이스(`SandboxClient`)는 그대로다.
- 한 세션이 다른 세션의 sandboxd를 호출하는 것은 컨테이너별 무작위 Bearer 토큰으로 막는다(토큰은 생성 시 환경변수로 주입되고 DB `sandbox_instance.token_encrypted`에 저장).

### 3.2 생명주기

```
 세션 첫 런 ──▶ [creating] ──▶ [running] ──(유휴 30분 / 명시 종료 / OOM / 오류)──▶ [stopping] ──▶ [terminated]
                  │                 ▲                                              │
                  │ 스냅샷 복원       │ 다음 런(재사용)                                 │ kind=session 스냅샷 저장(옵션)
                  ▼                 │                                              ▼
            runner가 세션에 샌드박스가 없거나 죽었으면 새로 만든다 (복원 우선순위: 최신 session 스냅샷 > 버전 published 스냅샷 > 빈 홈)
```

| 단계 | 담당 | 동작 |
|---|---|---|
| 생성 | runner → sandbox-manager `POST /sandboxes` | manager가 동시 수 한도 확인 → 볼륨 생성 → 컨테이너 생성·시작(3.4 옵션) → sandboxd `/health` 200까지 최대 20초 대기 → 응답 `{id, container_id, base_url, token}` |
| 초기화 | runner | (1) 스냅샷이 있으면 storage에서 읽어 sandboxd `POST /fs/restore`로 스트리밍 (2) 에이전트 파일을 `/home/agent/agent-files/`에, 스킬을 `/home/agent/skills/<slug>/`에 동기화(sandboxd `PUT /fs/upload`) (3) 환경변수는 컨테이너 생성 시 주입이므로 생성 전에 manager에 전달 |
| 재사용 | runner | 같은 세션의 다음 런은 `session.sandbox_instance_id`로 sandboxd `/health` 확인 후 그대로 사용. 홈 디렉터리와 사용자 설치 패키지가 유지된다 |
| 유휴 회수 | sandbox-manager 주기 태스크(60초) | 모든 샌드박스의 `/health`에서 `idle_seconds`를 읽어 `SANDBOX_IDLE_TIMEOUT_SEC`(기본 1800) 초과 시 종료 절차. 런 실행 중(`busy=true`)이면 제외 |
| 종료 절차 | sandbox-manager | (1) `session.persist_on_idle`(기본 true)이고 홈 크기 ≤ 한도면 sandboxd `GET /fs/snapshot`을 storage에 저장하고 api 콜백으로 `snapshot(kind=session)` 행 생성 요청 (2) `docker stop`(5초) → `rm` → 볼륨 삭제 (3) api 콜백 `terminated(reason)` |
| 강제 종료 | api(세션 삭제, 관리자 화면) → manager `DELETE /sandboxes/{id}` | 스냅샷 없이 즉시 제거 |
| 복구 | sandbox-manager 기동 시 | 라벨 `platform.sandbox=1`인 컨테이너를 나열해 DB와 대조(api `GET /internal/sandboxes/active`). DB에 없는 컨테이너는 제거, 컨테이너가 없는 running 행은 api 콜백으로 terminated 처리 |

세션 스냅샷 보존: 세션당 최신 1개만 유지(새로 만들면 이전 것 삭제), `SNAPSHOT_SESSION_TTL_DAYS`(기본 7) 후 삭제. 발행 스냅샷은 버전과 함께 영구 보존(버전 삭제 시 삭제).

### 3.3 이미지 구성 (`infra/sandbox-image/Dockerfile`)

| 항목 | 내용 | 근거 |
|---|---|---|
| 베이스 | `python:3.12-slim-bookworm` | 파이썬 도구가 핵심. Debian 패키지 생태계 |
| 시스템 패키지 | `git curl wget ca-certificates jq ripgrep unzip zip tar gzip less procps build-essential libffi-dev libssl-dev pandoc(제외) sqlite3` | pip 네이티브 빌드와 일반 CLI 작업. 이미지 약 900MB 허용 |
| 런타임 | Python 3.12(이미지 기본), Node.js 22 LTS(nodesource), `uv`, `pip`, `npm` | 데이터·스크립트 작업 양대 생태계 |
| 사전 설치 파이썬 패키지(시스템 site-packages, 읽기 전용) | `requests httpx pandas numpy openpyxl python-docx python-pptx pypdf matplotlib beautifulsoup4 lxml pyyaml` | 흔한 작업을 설치 없이. 홈 볼륨이 아닌 이미지 계층에 두어 스냅샷을 작게 유지 |
| 사용자 | `agent`(uid/gid 1000), 홈 `/home/agent`, 셸 `bash` | 비루트 |
| 사용자 설치 경로 | `PIP_USER=1`, `PYTHONUSERBASE=/home/agent/.local`, `NPM_CONFIG_PREFIX=/home/agent/.npm-global`, PATH에 `~/.local/bin:~/.npm-global/bin` | 루트 파일시스템이 읽기 전용이어도 `pip install`, `npm i -g`가 동작하고 홈 스냅샷에 포함된다 |
| apt | 지원하지 않음(비루트, 읽기 전용 루트) | 필요한 시스템 패키지는 이미지에 추가하고 재빌드. 12장 미결 |
| sandboxd | `/opt/sandboxd/`(root 소유, 자체 venv, 읽기 전용) | 에이전트의 `pip install`이 sandboxd 의존성을 깨지 못하게 분리 |
| 엔트리포인트 | `docker init`(tini) → `/opt/sandboxd/venv/bin/python -m sandboxd` (uid 1000) | sandboxd가 죽으면 컨테이너 종료 → runner가 감지해 재생성 |
| 홈 초기 구조 | `/home/agent/{agent-files,skills,uploads,outputs,.local,.npm-global}` | 역할별 고정 경로를 시스템 프롬프트에 명시 |

### 3.4 보안 설정 (sandbox-manager가 Docker SDK `containers.create`에 넘기는 값)

| 옵션 | 값 | 목적 |
|---|---|---|
| `user` | `"1000:1000"` | 비루트 |
| `cap_drop` | `["ALL"]` | 모든 capability 제거. `cap_add` 없음 |
| `security_opt` | `["no-new-privileges:true", "seccomp=<profile JSON 문자열>"]` | setuid 상승 차단. seccomp는 Docker 기본 프로파일(`infra/sandbox-image/seccomp.json`에 복사)에서 `userfaultfd, bpf, perf_event_open, personality, add_key, request_key, keyctl, kexec_*, mount, umount2, pivot_root, ptrace(허용 유지: 디버깅 도구 필요)`를 제외한 변형. SDK는 파일 경로가 아니라 JSON 내용을 요구하므로 manager가 파일을 읽어 문자열로 넘긴다 |
| `read_only` | `True` | 루트 파일시스템 읽기 전용 |
| `tmpfs` | `{"/tmp": "rw,nosuid,size=512m", "/run": "rw,nosuid,size=16m", "/var/tmp": "rw,nosuid,size=128m"}` | 쓰기 가능한 임시 영역. `noexec`는 pip 빌드를 깨므로 적용하지 않음 |
| `volumes` | `{"sbx-<id>-home": {"bind": "/home/agent", "mode": "rw"}}` | 홈 디렉터리 전용 볼륨 |
| `mem_limit`, `memswap_limit` | 기본 `2g`, 동일 값(스왑 0) | 메모리 상한. 에이전트 설정에서 관리자 상한(`SANDBOX_MAX_MEM`) 이하로 조정 가능 |
| `nano_cpus` | 기본 `1_000_000_000`(1 vCPU) | CPU 상한 |
| `pids_limit` | `256` | 포크 폭탄 차단 |
| `ulimits` | `nofile 4096/4096`, `core 0` | 파일 핸들·코어 덤프 |
| `shm_size` | `64m` | |
| `network` | `sandbox-net` | 내부 전용 네트워크 |
| `environment` | `HTTP_PROXY/HTTPS_PROXY=http://egress-proxy:3128`, `NO_PROXY=localhost,127.0.0.1`, `SANDBOX_ID`, `SANDBOX_TOKEN`, `HOME`, `PIP_USER` 등 + 에이전트 버전 환경변수(복호화) | 프록시 강제는 네트워크로, 환경변수는 편의 |
| `init` | `True` | 좀비 프로세스 회수 |
| `hostname` | `sandbox` | 호스트 정보 노출 방지 |
| `labels` | `platform.sandbox=1`, `platform.sandbox_id`, `platform.session_id`, `platform.workspace_id` | 복구·정리용 |
| `runtime` | `SANDBOX_RUNTIME` 설정 시 `runsc`(gVisor) | 운영 Linux에서 선택. Docker Desktop은 미지원 |
| `stop_timeout` | 5 | |
| 미사용 | `privileged`, `pid_mode`, `ipc_mode`, 호스트 바인드 마운트, 디바이스 | 금지 목록. 코드 리뷰 체크 항목 |

운영 Linux 권장(선택): 데몬 `userns-remap` 활성화, gVisor 설치 후 `SANDBOX_RUNTIME=runsc`.

### 3.5 네트워크 격리와 egress 프록시

- 샌드박스는 `internal: true`인 `sandbox-net`에만 붙는다. 기본 게이트웨이가 없어 프록시를 거치지 않는 외부 연결은 TCP 단계에서 실패한다. DNS도 외부 이름을 해석하지 못하며, 프록시가 대신 해석한다(curl·pip·npm·git은 `HTTPS_PROXY`로 CONNECT를 보내므로 로컬 해석이 필요 없다).
- egress-proxy(squid)는 `sandbox-net`과 `egress-net`에 양다리로 붙는다. 설정 파일 `infra/egress/squid.conf`:
  - `acl allowed dstdomain "/etc/squid/allowlist.txt"` — 한 줄에 도메인 하나, `.pypi.org`처럼 앞에 점을 붙이면 하위 도메인 포함.
  - `acl to_private dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 100.64.0.0/10 fc00::/7 fe80::/10` → `http_access deny to_private` (허용 목록보다 먼저). 허용 도메인이 사설 IP로 해석되는 DNS 리바인딩도 이 규칙으로 막힌다.
  - `http_access allow allowed` → `http_access deny all`. CONNECT는 443만 허용.
  - HTTPS는 복호화하지 않는다(CONNECT 호스트명으로 판정).
- 기본 허용 목록(`infra/egress/allowlist.txt`): `.pypi.org .pythonhosted.org .npmjs.org .npmjs.com .github.com .githubusercontent.com .debian.org(미사용) .huggingface.co(주석)`. 추가 도메인은 사용자가 파일에 직접 넣는다.
- 변경 반영: 파일 수정 후 `docker compose exec egress-proxy squid -k reconfigure` 또는 재시작. 1단계에서는 콘솔 UI로 편집하지 않는다(12장).
- 접근 로그(`access.log`)는 볼륨에 남겨 감사에 쓴다. 로그에는 호스트명과 바이트 수만 남고 본문은 없다.
- `http_request` 내장 도구는 runner가 아니라 샌드박스 안(sandboxd `POST /http`)에서 실행한다. 따라서 모델이 어떤 경로로 요청하든 동일한 허용 목록이 적용되고, runner의 네트워크(Postgres·Redis·LLM API 접근 가능)는 모델에 노출되지 않는다.

### 3.6 홈 디렉터리 스냅샷

| 항목 | 결정 |
|---|---|
| 형식 | `tar.gz`(Python `tarfile` 표준 라이브러리, 외부 의존 없음). 루트는 `/home/agent` 상대 경로 |
| 제외 | `.cache/`, `.npm/_cacache/`, `**/__pycache__/`, `.local/share/uv/cache`, `uploads/`(메시지 첨부는 메시지 단위로 storage에 있음) |
| 저장 위치 | `storage` 볼륨 `/data/snapshots/<workspace_id>/<snapshot_id>.tar.gz`. `snapshot` 행에 `storage_key, size_bytes, sha256` |
| 생성 | sandboxd `GET /fs/snapshot`(스트리밍) → 호출자(runner: 발행 시, manager: 유휴 종료 시)가 storage에 기록하며 sha256 계산 |
| 복원 | sandboxd `POST /fs/restore`(tar.gz 스트림). sandboxd가 `/home/agent`에서 uid 1000으로 풀기 때문에 소유권 문제가 없다. 복원은 빈 홈에만 수행(기존 파일 있으면 409) |
| 크기 한도 | 압축 전 홈 크기 `SNAPSHOT_MAX_HOME_BYTES`(기본 1GB) 초과 시 발행 실패(오류 메시지에 큰 디렉터리 상위 10개 표시). 세션 스냅샷은 초과 시 건너뛰고 `terminated(reason=idle_timeout, snapshot_skipped=true)` |
| 홈 디스크 한도 | 볼륨 쿼터는 Docker 로컬 드라이버로 강제할 수 없으므로 sandboxd가 60초마다 `du`로 측정해 `/health`에 보고. `SANDBOX_HOME_HARD_LIMIT_BYTES`(기본 4GB) 초과 시 manager가 종료(reason=`disk_limit`) |
| 발행 흐름 | `POST /agents/{id}/publish` → api가 draft 세션의 샌드박스가 살아 있으면 스냅샷 생성(runner 작업 큐 `jobs:queue`로 위임, 동기 대기 최대 60초) → 없으면 직전 발행 스냅샷을 그대로 상속 → `agent_version(published, snapshot_id)` 생성 |
| 무결성 | 복원 시 sha256 검증. 심볼릭 링크는 홈 밖을 가리키면 건너뜀(`tarfile` 필터 `data`) |

### 3.7 파일 입출력 경로

| 경로 | 용도 | 출처 |
|---|---|---|
| `/home/agent/agent-files/` | 에이전트 버전에 첨부된 파일(읽기용 참고 자료) | 샌드박스 초기화 시 storage에서 동기화 |
| `/home/agent/skills/<slug>/` | 스킬 패키지(`SKILL.md`, 자료, 스크립트) | 초기화 시 `skill_version` 패키지 풀기 |
| `/home/agent/uploads/<message_id>/` | 사용자가 메시지에 첨부한 파일 | 런 시작 시 sandboxd `PUT /fs/upload` |
| `/home/agent/outputs/` | 에이전트가 사용자에게 전달할 파일을 두도록 프롬프트에서 안내하는 디렉터리 | 에이전트 |
| `/home/agent/task/` | 과업 실행 시 `inputs.json`과 입력 파일 | runner |
| 그 외 `/home/agent/**` | 작업 공간 | 에이전트 |

런 종료 시 runner는 sandboxd `GET /fs/changes?since=<run_started_at>`로 변경 파일(mtime 기준, 숨김 디렉터리·`.local`·`.npm-global`·`node_modules`·`.venv` 제외)을 받아 파일당 50MB, 런당 200MB·200개까지 storage `/data/session-files/<session_id>/<run_id>/<path>`로 복사하고 `session_file(kind=output)` 행을 만든다. 한도를 넘는 파일은 `session_file.storage_key=null, reason=too_large`로 목록에만 남는다. 다운로드는 항상 storage에서 하므로 샌드박스 종료와 무관하다.

### 3.8 Docker API 접근과 소켓 노출 위험 완화

Docker 소켓 접근은 호스트 root와 동등하다. 완화책:

1. 소켓은 sandbox-manager 한 서비스에만 마운트한다. api·runner·gateway 컨테이너에는 없다.
2. sandbox-manager는 외부 포트를 열지 않고 `app-net`에서 `X-Internal-Token`(`.env`의 `INTERNAL_API_TOKEN`)으로만 호출받는다.
3. manager의 Docker 호출은 `app/sandbox_manager/docker_backend.py` 한 모듈에 모으고, 생성 옵션은 3.4절 표를 하드코딩한다. 호출자가 이미지·마운트·네트워크·capability를 지정할 수 없다(요청 스키마에 해당 필드가 없다).
4. 라벨 `platform.sandbox=1`이 없는 컨테이너는 절대 조작하지 않는다(목록·종료·삭제 모두 라벨 필터).
5. Linux 호스트에 올릴 때는(선택) `tecnativa/docker-socket-proxy`를 사이에 두어 `CONTAINERS=1 POST=1 VOLUMES=1 IMAGES=1(read) EXEC=0 NETWORKS=0(read만) SYSTEM=0 SWARM=0`으로 엔드포인트를 제한한다(compose 프로파일 `hardened`). Docker Desktop에서는 직접 마운트.
6. manager 코드 변경은 design-reviewer 검토 필수 항목으로 둔다.
7. sandbox-manager는 `sandbox-net`에도 붙어 있으므로(샌드박스 `/health`·스냅샷 호출용 클라이언트 역할) 7100 포트가 샌드박스에서 보인다. 방어: compose에서 sandbox-manager에 `app-net` 고정 IP(`ipv4_address`)를 주고 서버를 그 주소에만 bind한다. 추가로 ASGI 미들웨어가 요청 출발지 IP가 `sandbox-net` 대역이면 403으로 거부한다(이중 방어). 샌드박스 → sandbox-manager 호출이 401/연결 거부인지 M3 네트워크 테스트에 포함한다. (오케스트레이터 1차 검토에서 추가, 2026-10-12)

### 3.9 Windows 기본 환경과 Linux(선택) 환경의 차이

기본 운영 환경은 본인 PC의 Windows 11 + Docker Desktop(WSL2)이다. Linux VM은 나중에 옮길 수 있는 선택지이며 같은 compose를 쓴다.

| 항목 | Windows 11 + Docker Desktop(WSL2), 기본 | Linux VM(선택) | 대응 |
|---|---|---|---|
| Docker 소켓 | compose가 Linux VM 안에서 실행되므로 `/var/run/docker.sock` 마운트가 그대로 동작 | 동일 | 없음 |
| gVisor | 불가 | 선택 설치 | `SANDBOX_RUNTIME` 비우면 runc |
| userns-remap | 설정 불가 | 선택 | 선택 사항으로 문서화 |
| seccomp/cgroup 제한 | 지원 | 지원 | 동일 프로파일 |
| 바인드 마운트 성능 | Windows 경로 바인드는 느림 | 빠름 | 소스 핫리로드는 dev override에서만. 데이터는 모두 named volume |
| 줄바꿈 | CRLF 위험 | LF | `.gitattributes`로 `*.sh, Dockerfile, *.conf` LF 강제 |
| 경로 | Windows 경로를 샌드박스에 마운트하지 않음 | | 바인드 마운트 금지 규칙과 일치 |
| 포트 | 8080이 다른 앱과 충돌 가능 | | `.env`의 `GATEWAY_HTTP_PORT`(기본 8080). 바인드 주소는 127.0.0.1 고정 |
| WSL2 메모리 | `.wslconfig`의 `memory` 한도 안에서 모든 컨테이너가 돈다 | 호스트 전체 | 12GB 이상 권장. 샌드박스 동시 수(3.10)와 함께 조정 |
| 호스트 네트워크 | 미지원 | | 사용하지 않음 |

### 3.10 동시 세션 수 제한

| 한도 | 기본값 | 적용 지점 |
|---|---|---|
| `SANDBOX_MAX_CONCURRENT` | 3 | manager. 초과 시 `429 sandbox_capacity`, runner는 런을 `queued(reason=capacity)`로 두고 15초 간격 재시도(최대 10분), 클라이언트에는 `run.queued` 이벤트 |
| `RUNNER_MAX_CONCURRENT_RUNS` | 3 | runner 워커 수 |
| 세션당 동시 런 | 1 | api: 같은 세션에 실행 중 런이 있으면 `409 run_in_progress` |

PC 산정 기준: 샌드박스 기본 1 vCPU/2GB이므로 동시 3개면 3 vCPU/6GB가 상한이고, 플랫폼 서비스 7개가 약 2GB를 더 쓴다. WSL2 메모리 한도를 12GB 이상으로 두면 여유가 있다. 8코어/32GB PC라면 `SANDBOX_MAX_CONCURRENT=5`까지 올려도 된다. 계정이 1개이므로 계정별 한도는 두지 않는다.

### 3.11 sandboxd API

| 메서드·경로 | 요청 | 응답 | 비고 |
|---|---|---|---|
| `GET /health` | - | `{status, uptime_sec, idle_seconds, busy, home_bytes, exec_running}` | 인증 없음(정보 최소) |
| `POST /exec` | `{command: str, cwd?, env?, timeout_sec? (기본 120, 최대 900), stdin?}` | NDJSON 스트림: `{"t":"start","exec_id"}`, `{"t":"stdout","d":"..."}`, `{"t":"stderr","d":"..."}`, `{"t":"exit","code":int,"timed_out":bool,"duration_ms"}` | `bash -lc`로 실행, 세션 그룹 분리(`setsid`)해 타임아웃 시 그룹 전체 kill. 동시 실행 최대 4 |
| `POST /exec/{id}/kill` | - | `{killed: bool}` | 런 취소 시 |
| `GET /fs/read` | `path, offset?, limit?`(바이트) | `{content, encoding: utf-8|base64, size, truncated}` | 텍스트 판별 실패 시 base64 |
| `PUT /fs/write` | `{path, content, encoding, mode: overwrite|append, mkdirs: bool}` | `{size}` | |
| `GET /fs/list` | `path, recursive?, max_entries?(기본 500)` | `[{path, type, size, mtime}]` | |
| `GET /fs/stat` | `path` | `{type, size, mtime, mode}` | |
| `GET /fs/download` | `path` | 바이트 스트림 | 결과 파일 수집 |
| `PUT /fs/upload` | `path`, 바이트 스트림, 헤더 `X-Mkdirs` | `{size}` | 첨부·스킬 동기화 |
| `GET /fs/changes` | `since`(epoch ms), `max?` | `[{path, size, mtime}]` | mtime 스캔, 제외 규칙 3.7절 |
| `GET /fs/snapshot` | - | tar.gz 스트림, 헤더 `X-Home-Bytes` | 3.6절 |
| `POST /fs/restore` | tar.gz 스트림 | `{files, bytes}` | 빈 홈에만 |
| `POST /http` | `{method, url, headers?, body_b64?, timeout_sec?(기본 60), max_body_bytes?(기본 64KB)}` | `{status, headers, body_b64, truncated, saved_path?}` | 프록시 경유. 본문이 한도 초과면 `/home/agent/downloads/<uuid>`에 저장하고 경로 반환 |

모든 경로는 `/home/agent` 아래로 정규화(`resolve()` 후 `is_relative_to`)하고 벗어나면 400. 인증은 `Authorization: Bearer $SANDBOX_TOKEN`(`/health` 제외). 구현은 `starlette + uvicorn`(자체 venv), 요청 본문 상한 256MB.

## 4. 에이전트 루프 설계

### 4.1 루프 개요

```
run 시작 ─▶ 샌드박스 확보(3.2) ─▶ 첨부 업로드 ─▶ 히스토리 로드(세션 메시지 → 정규 메시지) ─▶ 컨텍스트 예산 확인(압축 필요 시 4.7)
   ─▶ ┌─ 스텝 i ──────────────────────────────────────────────────────────────────────────┐
      │ 모델 스트림 호출 ─▶ 텍스트 델타·도구 호출 델타를 이벤트로 발행                          │
      │ stop_reason:                                                                        │
      │   end_turn / refusal ─▶ 어시스턴트 메시지 저장, 런 종료                                 │
      │   max_tokens ─▶ 도구 호출 미완이면 max_tokens 올려 1회 재시도, 아니면 그대로 종료          │
      │   tool_use ─▶ 도구 호출을 순서대로 실행(각각 이벤트 스트림) ─▶ tool_result 묶음 추가       │
      │              ask_user/승인 필요 ─▶ run=waiting_input 으로 종료(슬롯 반납)                 │
      │ 한도 확인: max_steps, max_run_seconds, 토큰 예산, 취소 플래그 ─▶ 초과 시 종료 처리          │
      └─────────────────────────────────────────────────────────────────────────────────────┘
   ─▶ 변경 파일 수집(3.7) ─▶ 사용량 집계 ─▶ run.completed 이벤트
```

루프 코드는 `app/agent/loop.py`의 `AgentLoop.run(ctx) -> AsyncIterator[RunEvent]`에 두고, HTTP·Redis·DB에 의존하지 않는다(의존은 `LoopPorts` 프로토콜로 주입: 모델 어댑터, 샌드박스 클라이언트, 이벤트 싱크, 시계). 2단계 워크플로우 엔진이 같은 클래스를 프로세스 내에서 호출할 수 있어야 하기 때문이다(13장).

### 4.2 모델 어댑터 인터페이스

```python
# app/llm/base.py
class ModelAdapter(Protocol):
    provider: Literal["anthropic", "openai_compatible"]
    async def stream(self, req: ChatRequest) -> AsyncIterator[ModelEvent]: ...
    def estimate_tokens(self, req: ChatRequest) -> int: ...          # 휴리스틱(문자수/3.5 + 블록당 가산), 직전 usage로 보정
    def capabilities(self, model_id: str) -> ModelCapabilities: ...  # context_window, max_output, supports_tools, supports_thinking

class ChatRequest(BaseModel):
    model: str
    system: str                      # 단일 문자열. 세션 동안 불변(4.8)
    messages: list[CanonicalMessage]
    tools: list[ToolSpec]            # name, description, input_schema(JSON Schema), strict: bool
    params: ModelParams              # temperature?, top_p?, max_output_tokens, effort?("low"|"medium"|"high"|"xhigh"|"max"), thinking_display?
    cache_hint: bool = True
    metadata: dict                   # run_id 등 로깅용. 프로바이더에 전달하지 않음
```

정규 메시지·블록(`app/llm/types.py`):

| 타입 | 필드 | 비고 |
|---|---|---|
| `CanonicalMessage` | `role: user|assistant`, `blocks: list[Block]` | tool_result는 `user` 메시지의 블록으로 들어간다(Anthropic 모양을 정규형으로 채택). OpenAI 변환 시 `role: tool` 메시지로 분해 |
| `TextBlock` | `text` | |
| `ImageBlock` | `media_type, data_b64` 또는 `storage_key` | 1단계는 사용자 첨부 이미지만(PNG/JPEG/WebP). 모델이 미지원이면 "[이미지 첨부: 파일명]" 텍스트로 대체 |
| `ToolUseBlock` | `id, name, input: dict` | |
| `ToolResultBlock` | `tool_use_id, content: str, is_error: bool` | |
| `OpaqueBlock` | `provider, payload: dict` | Anthropic `thinking`/`redacted_thinking` 블록 등 프로바이더 전용 블록을 원문 그대로 보관·재전송. 다른 프로바이더로 보낼 때는 제거 |

`ModelEvent`:

| 이벤트 | 필드 | Anthropic 출처 | OpenAI 호환 출처 |
|---|---|---|---|
| `text_delta` | `text` | `content_block_delta.text_delta` | `choices[0].delta.content` |
| `thinking_delta` | `text` | `thinking_delta`(display=summarized 요청 시) | `delta.reasoning_content`(있을 때) |
| `tool_call_start` | `index, id, name` | `content_block_start(type=tool_use)` | `delta.tool_calls[i]`에 `id`,`function.name`이 처음 나타날 때 |
| `tool_call_delta` | `index, json_fragment` | `input_json_delta.partial_json` | `delta.tool_calls[i].function.arguments` |
| `tool_call_end` | `index` | `content_block_stop` | 다음 index 등장 또는 finish |
| `opaque_block` | `payload` | thinking 블록 완료 시 원문 | 없음 |
| `message_end` | `stop_reason, usage{input, output, cache_read, cache_write}, raw_stop` | `message_delta` + `message_stop` | `finish_reason` + 마지막 청크 `usage`(`stream_options.include_usage=true`) |
| `error` | `code, message, retryable` | SDK 예외 매핑 | 동일 |

stop_reason 정규화: `end_turn`(Anthropic `end_turn`, OpenAI `stop`), `tool_use`(`tool_use`, `tool_calls`), `max_tokens`(`max_tokens`, `length`), `refusal`(`refusal`, `content_filter`), `pause_turn`은 서버 도구 미사용이라 발생하지 않지만 받으면 `end_turn`으로 취급하고 경고 로그.

### 4.3 프로바이더별 구현 요점

**Anthropic (`app/llm/anthropic_adapter.py`, 공식 `anthropic` SDK, `client.messages.stream`)**
- `system`은 `[{"type":"text","text":..., "cache_control":{"type":"ephemeral"}}]`로, `tools`의 마지막 항목에도 `cache_control`을 붙인다. 메시지 쪽은 직전 턴 마지막 블록에 이동식 브레이크포인트 1개.
- 도구는 우리 정의 커스텀 도구(`name, description, input_schema, strict: true, eager_input_streaming: true`)로 보낸다. Anthropic 전용 `bash_20250124`·`text_editor_20250728`는 쓰지 않는다. 근거: 동일 도구 집합을 OpenAI 호환에도 그대로 써야 하고, 입력 스키마를 우리가 통제해야 한다.
- 사고(thinking): 모델 능력표에서 `supports_thinking`이면 `thinking={"type":"adaptive"}`(표시는 `params.thinking_display`가 `summarized`일 때만 `display` 지정), `output_config={"effort": params.effort}`. `budget_tokens`, `temperature` 등 샘플링 파라미터는 최신 모델이 거부하므로 능력표의 `accepts_sampling_params`가 true일 때만 전달.
- `tool_choice`는 항상 `auto`. 강제 호출(`any`/`tool`)은 최신 모델에서 400이므로 사용하지 않는다(과업 출력은 4.10).
- 모델이 반환한 `thinking` 블록은 `OpaqueBlock`으로 저장하고 같은 세션의 이후 요청에서 원문 그대로 재전송한다. 세션의 `system`·`tools`가 바뀌면(초안 수정 후 미리보기) 히스토리의 OpaqueBlock을 모두 제거하고 보낸다(4.8).
- `stop_reason=refusal`이면 도구를 실행하지 않고 턴을 끝낸다. 메시지에 `stop_reason` 저장.
- `eager_input_streaming` 때문에 도구 입력 JSON은 클라이언트가 검증한다: 누적 조각을 `json.loads`로 파싱 → 도구 스키마(`jsonschema`)로 검증 → 실패 시 실행하지 않고 `is_error` tool_result(`{"INVALID_JSON": ...}`)로 돌려준다. `max_tokens`로 끊긴 도구 호출은 실행하지 않는다.
- 능력표 기본값(`app/llm/catalog.py`): `claude-opus-5-5`(기본 추천), `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`. 컨텍스트 1M, 출력 128K, thinking 지원. 가격은 관리자가 입력(기본값 비움).
- SDK `max_retries=2`(연결·429·5xx 자동 재시도), `timeout=600`. 스트림 중간 끊김은 4.6절.

**OpenAI 호환 (`app/llm/openai_adapter.py`, 공식 `openai` SDK, Chat Completions, `base_url` 지정)**
- 대상: OpenAI 본가, vLLM, Ollama, LiteLLM, OpenRouter 등 `/v1/chat/completions`를 제공하는 모든 엔드포인트. Responses API는 호환 서버 지원이 고르지 않아 채택하지 않는다.
- 메시지 변환: `system` → 첫 `{"role":"system"}`; assistant `ToolUseBlock` → `tool_calls[{id, type:"function", function:{name, arguments: json.dumps(input)}}]`; user 메시지의 `ToolResultBlock`들 → 각각 `{"role":"tool","tool_call_id","content"}` 메시지로 분해(텍스트 블록은 별도 user 메시지); `OpaqueBlock` 제거.
- 도구: `{"type":"function","function":{name, description, parameters, strict}}`. `strict`는 프로바이더 설정 `supports_strict_tools`가 true일 때만.
- 요청: `stream=True, stream_options={"include_usage": True}`, `max_completion_tokens`(프로바이더 설정 `max_tokens_param`이 `max_tokens`면 그걸로), `temperature/top_p`는 설정된 경우만, `tool_choice="auto"`, `parallel_tool_calls` 기본값.
- 스트림 파싱: `delta.tool_calls[*].index`로 호출을 구분해 `arguments` 조각을 누적. `finish_reason`으로 종료 판정. `usage`가 안 오는 서버는 `estimate_tokens`로 대체하고 `usage.estimated=true`.
- `reasoning_content`(DeepSeek·vLLM 계열)가 있으면 `thinking_delta`로 흘리되 히스토리에는 저장하지 않는다.

### 4.4 내장 도구

도구 정의는 `app/agent/tools/<name>.py`에 `ToolSpec`(이름·설명·JSON Schema)과 `execute(ctx, input) -> ToolResult`를 둔다. 모든 도구는 sandboxd를 통해 샌드박스 안에서 실행되며 runner 프로세스에서 파일·네트워크를 직접 만지지 않는다.

| 도구 | 입력 스키마 요약 | 동작·제한 | 그룹 |
|---|---|---|---|
| `shell` | `{command: string, timeout_sec?: int(≤900), workdir?: string}` | `bash -lc` 실행. stdout/stderr를 `tool_call.output_delta`로 스트리밍. 결과 = 종료 코드 + 합친 출력(4.6 크기 제한) | `shell` |
| `run_python` | `{code: string, timeout_sec?: int}` | 코드를 `/tmp/run_<id>.py`에 쓰고 `python3`로 실행. 동작·제한은 shell과 동일 | `shell` |
| `read_file` | `{path: string, offset?: int, limit?: int}` | 텍스트는 그대로(최대 64KB, 초과 시 `truncated`와 다음 offset 안내), 바이너리는 메타데이터만 | `files` |
| `write_file` | `{path: string, content: string, append?: bool}` | 디렉터리 자동 생성. 최대 2MB | `files` |
| `edit_file` | `{path: string, old_text: string, new_text: string}` | `old_text`가 정확히 1회 나타나야 함(0회·2회 이상이면 오류) | `files` |
| `list_files` | `{path?: string, recursive?: bool, max_entries?: int}` | 기본 `/home/agent`, 숨김 디렉터리 제외 옵션 | `files` |
| `http_request` | `{method: GET|POST|PUT|PATCH|DELETE, url: string, headers?: object, body?: string, timeout_sec?: int}` | sandboxd `/http` 경유(허용 목록 적용). 응답 64KB 초과 시 파일로 저장하고 경로 반환. `Authorization` 헤더는 허용하되 값은 로그에서 가림 | `http` |
| `ask_user` | `{question: string, options?: string[], allow_free_text?: bool(기본 true)}` | 런을 `waiting_input`으로 전환(4.9). 답변 문자열이 tool_result로 들어간다 | `ask_user` |
| `submit_result` | 과업의 `output_schema` | 과업 런에서만 자동 추가(4.10) | (시스템) |

에이전트 버전의 `tools` 설정은 그룹 목록(`["shell","files","http","ask_user"]`)이다. 도구 간 병렬 호출이 와도 순서대로 하나씩 실행한다(샌드박스 작업 공간 공유, 결정성). 결과는 한 user 메시지에 모아 돌려준다.

### 4.5 스트리밍 이벤트(런 이벤트) 타입

SSE 포맷: `id: <redis stream id>`, `event: <type>`, `data: <JSON>`. JSON 공통 필드 `{run_id, session_id, seq, ts, type, data}`. 콘솔·웹앱·서비스 API 모두 같은 이벤트를 쓴다.

| type | data | 영속(run_event) |
|---|---|---|
| `run.queued` | `{position?, reason?}` | 예 |
| `run.started` | `{step_limit, model, sandbox_id, resumed: bool}` | 예 |
| `step.started` | `{step}` | 예 |
| `message.delta` | `{message_id, text}` | 아니오(최종 메시지에만 저장) |
| `thinking.delta` | `{text}` | 아니오 |
| `tool_call.started` | `{tool_call_id, name}` | 예 |
| `tool_call.input_delta` | `{tool_call_id, json_fragment}` | 아니오 |
| `tool_call.ready` | `{tool_call_id, name, input}` | 예(input 전체) |
| `tool_call.output_delta` | `{tool_call_id, stream: stdout|stderr, text}` | 아니오 |
| `tool_call.completed` | `{tool_call_id, status: ok|error|timeout|cancelled, exit_code?, duration_ms, output_excerpt, output_bytes, output_storage_key?}` | 예 |
| `file.changed` | `{path, size, session_file_id?, downloadable: bool}` | 예 |
| `run.waiting_input` | `{interaction_id, kind: question|approval, question, options?, tool_call_id}` | 예 |
| `run.resumed` | `{interaction_id}` | 예 |
| `context.compacted` | `{before_tokens, after_tokens, summary_message_id}` | 예 |
| `usage` | `{step, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd?}` | 예 |
| `message.completed` | `{message_id, text, stop_reason, files: [...]}` | 예(메시지 테이블) |
| `run.completed` | `{status: succeeded|failed|cancelled|waiting_input, usage_total, steps, output? (과업)}` | 예 |
| `run.error` | `{code, message, retryable}` | 예 |
| `ping` | `{}` | 아니오. 15초마다 |

### 4.6 한도·크기·실패·재시도 정책

| 항목 | 기본값 | 조정 범위 | 초과 시 |
|---|---|---|---|
| `max_steps`(모델 호출 수) | 30 | 1~200 (에이전트 버전) | 마지막 스텝 후 "스텝 한도 도달" 텍스트를 붙여 `run.completed(status=succeeded, limit=steps)` |
| `max_run_seconds` | 1800 | 60~7200 | 진행 중 도구 kill, `failed(code=timeout)` |
| 도구 1회 타임아웃 | shell/python 120초(모델이 ≤900 지정 가능), http 60초 | | `tool_call.completed(status=timeout)` + 모델에 오류 결과 |
| 도구 결과 크기 | 32KB를 모델에 전달(앞 24KB + 뒤 8KB, 중간에 `[... N bytes omitted ...]`) | | 전체 출력은 1MB까지 `run_event`에, 초과분은 storage `tool-outputs/`에 저장 |
| 모델 출력 토큰 | `max_output_tokens` 기본 16000 | 모델 상한까지 | `max_tokens` 종료 시 도구 호출 미완이면 상한을 2배(모델 상한 이하)로 1회 재시도 |
| 런당 토큰 예산 | `max_total_tokens` 기본 2,000,000(입력+출력 누적) | 에이전트 버전 | `failed(code=token_budget)` |
| 런당 비용 상한 | `max_cost_usd` 기본 없음 | 가격표 있을 때만 | 동일 |
| 컨텍스트 압축 임계 | 모델 컨텍스트의 60% 또는 `context_budget_tokens` | | 4.7 |
| 동시 도구 실행 | 1 | | |

실패·재시도:

| 상황 | 처리 |
|---|---|
| 모델 API 429/5xx/연결 오류 | SDK 재시도(2회) 후에도 실패하면 runner가 지수 백오프(2, 4, 8초) 2회 추가. 스트림 중간 끊김은 해당 스텝의 부분 출력(텍스트·도구 호출)을 버리고 `run.retrying` 로그 후 스텝 재실행(최대 2회) |
| 모델 API 400/401/403 | 재시도 없이 `failed(code=model_request_invalid|model_auth)`. 메시지에 프로바이더 오류 본문(키 등 비밀 마스킹) |
| 도구 입력 JSON 오류·스키마 불일치 | 실행하지 않고 `is_error` 결과로 모델에 반환(모델이 수정 재시도). 연속 3회면 `failed(code=tool_input_invalid)` |
| 도구 실행 오류(비 0 종료, 예외) | `is_error` 결과로 모델에 반환. 런은 계속 |
| sandboxd 연결 실패 | `/health` 3회 실패 시 샌드박스를 죽은 것으로 보고 최신 스냅샷에서 재생성 후 같은 도구 호출 1회 재실행. 재생성도 실패하면 `failed(code=sandbox_unavailable)` |
| 샌드박스 OOM/디스크 한도 종료 | manager 콜백으로 감지. 진행 중 도구 호출은 `error`, 모델에 "샌드박스가 메모리 한도로 재시작됨" 결과 전달 후 재생성 |
| 취소(`run:{id}:cancel`) | 스텝 경계와 도구 출력 수신 중 1초마다 확인. 모델 스트림은 태스크 취소, 도구는 sandboxd kill. 부분 어시스턴트 텍스트는 메시지로 저장(`stop_reason=cancelled`) |
| runner 프로세스 죽음 | 2.5절 하트비트 복구 |

### 4.7 컨텍스트 압축 전략

1. 예방: 도구 결과를 32KB로 자르고, `read_file`은 64KB·offset 페이지네이션, `list_files`는 500개 제한. 긴 출력은 파일로 저장해 경로만 전달하도록 시스템 프롬프트에서 안내한다.
2. 측정: 각 스텝 전에 `estimated = 직전 응답 usage.input_tokens + 그 뒤 추가된 블록의 estimate_tokens`. 첫 스텝은 전체 휴리스틱.
3. 압축 시점: `estimated > context_budget`(기본 모델 컨텍스트의 60%, 에이전트 설정으로 변경)이고 현재 도구 라운드가 끝난 상태(assistant의 tool_use에 대한 tool_result까지 모두 있음)일 때만. 도구 라운드 중간에는 하지 않는다.
4. 방식 "단순 압축": 같은 모델(또는 에이전트 설정의 `compaction_model`, 기본은 같은 프로바이더의 저렴한 모델이 아닌 동일 모델)에게 전체 히스토리를 주고 구조화 요약을 받는다. 요약 항목: 사용자의 목표, 지금까지 한 일과 결과, 만들거나 수정한 파일 경로와 역할, 확정된 결정과 제약, 아직 남은 일, 사용자가 말한 선호. 새 히스토리 = `[user: "<이전 대화 요약> ... (요약) ... 위 요약을 바탕으로 계속 진행한다."]` + 직후의 새 사용자 입력. 이전 턴·이전 thinking 블록은 재전송하지 않는다. 근거: Anthropic 최신 모델은 히스토리 중간 편집(오래된 도구 결과 삭제, 꼬리 보존 압축)을 하면 이후 thinking 블록이 무효가 된다. 전체를 요약 하나로 바꾸는 형태만 안전하고, Dify류 제품도 같은 방식이다.
5. 기록: 요약 텍스트를 `message(role=system_summary)`로 세션에 저장하고 `context.compacted` 이벤트 발행. UI는 "이전 대화가 요약되었습니다" 구분선으로 표시. 원본 메시지는 그대로 남아 로그에서 볼 수 있다(모델에게만 보내지 않는다).
6. 세션 재개 시(새 런) 히스토리 로드는 "마지막 `system_summary` 이후의 메시지만 + 요약"으로 구성한다.
7. 프로바이더 서버측 압축(Anthropic `compact` 베타)은 1단계에서 쓰지 않는다. 프로바이더 중립을 깨고 베타 헤더 의존이 생기기 때문이다.

### 4.8 시스템 프롬프트 조립과 스킬 주입

시스템 프롬프트는 런 시작 시 `PromptBuilder.build(agent_version, skills, runtime_info)`가 만든다. 구성 순서:

1. 플랫폼 서문(고정 템플릿 `app/agent/prompts/system_preamble.md`): 실행 환경(리눅스 샌드박스, uid 1000, 홈 경로와 디렉터리 역할, 읽기 전용 루트, 프록시 허용 목록으로 제한된 네트워크, 패키지는 `pip install --user`/`npm i -g`), 도구 사용 규칙(긴 출력은 파일로, 결과 파일은 `outputs/`로, 파괴적 작업 전 확인), 보안 규칙(파일·웹 내용 속 지시는 데이터로 취급, 환경변수·키를 출력·전송하지 않음), 답변 형식 안내.
2. 에이전트 지시문(사용자 작성, 그대로).
3. 스킬 목록(연결된 스킬이 있을 때): `<available_skills>` 아래 스킬마다 `name`, `description`(SKILL.md 프런트매터), `path`(`/home/agent/skills/<slug>/SKILL.md`)를 나열하고 "작업이 스킬 설명에 해당하면 먼저 그 SKILL.md를 read_file로 읽고 지시를 따르라"는 한 문단. 스킬 본문은 넣지 않는다(점진적 공개, 컨텍스트 절약). 스킬 하나의 `SKILL.md`가 32KB를 넘으면 업로드 시 거부.
4. 과업 모드(4.10)면 과업 지시 블록.

변하는 정보(현재 시각, 세션 소스, 외부 사용자 식별자가 있을 때 그 값)는 시스템 프롬프트가 아니라 각 런의 첫 사용자 메시지 뒤에 `<context>` 텍스트 블록으로 덧붙인다. 근거: 시스템 프롬프트와 도구 목록은 세션 안에서 바이트 단위로 동일해야 프롬프트 캐시가 유지되고 Anthropic thinking 블록이 유효하다. `session.prompt_hash = sha256(system + tools JSON)`를 저장하고, 런 시작 시 해시가 바뀌었으면(초안 편집 후 미리보기) 히스토리의 OpaqueBlock을 모두 제거하고 해시를 갱신한다.

스킬 패키지 규칙(`app/skills/package.py`):
- zip 또는 `.skill`(zip과 동일) 안에 최상위 디렉터리 1개 또는 루트에 `SKILL.md`가 있어야 한다. 프런트매터 `name`(필수, slug 규칙 `[a-z0-9-]{1,64}`), `description`(필수, ≤1024자), `version`(선택).
- 허용 파일: 텍스트·스크립트·바이너리 자료 모두 허용하되 패키지 50MB, 파일 2000개 이하, 심볼릭 링크 금지, 경로 탈출 금지.
- 저장: storage `/data/skills/<workspace_id>/<skill_version_id>.zip`. 샌드박스 초기화 시 풀어서 `skills/<slug>/`에 둔다. 스크립트 실행 권한은 `*.sh`, `*.py` 등 확장자가 아니라 zip의 모드 비트를 그대로 적용하고, 없으면 사용자가 `bash script.sh`로 실행한다.
- 에이전트당 라이브러리 스킬 20개 + 임베디드 스킬 20개 상한. 이름 충돌 시 임베디드가 우선하고 경고.

### 4.9 사용자 확인·질문 (일시정지와 재개)

| 종류 | 발생 | 재개 입력 |
|---|---|---|
| `question` | `ask_user` 도구 호출 | `POST /runs/{id}/input {answer}` 또는 같은 세션에 새 메시지 전송(그 본문이 답으로 쓰임) |
| `approval` | 에이전트 버전 `approval_mode`가 `http_writes`(GET 외 http_request) 또는 `all_shell`(모든 shell/run_python)일 때 해당 도구 호출 직전 | `POST /runs/{id}/input {decision: approve|reject, note?}` |

처리: runner는 `run.pending_interaction = {interaction_id, kind, tool_call_id, tool_name, input}`를 저장하고 `status=waiting_input`으로 런을 끝내며 `run.waiting_input` 이벤트를 발행한다. 이때 assistant 메시지(tool_use 블록 포함, thinking OpaqueBlock 포함)는 이미 저장되어 있다. 재개 요청이 오면 api가 `run.status=queued, resume_payload=...`로 바꾸고 큐에 다시 넣는다. runner는 히스토리를 다시 로드하고, 승인이면 도구를 실제 실행해 tool_result를, 거절이면 `"사용자가 이 작업을 거절했습니다: <note>"`를 `is_error=false` tool_result로 붙여 루프를 이어간다. 샌드박스는 그동안 유휴 회수될 수 있으며 재개 시 3.2절 규칙으로 복원된다. 대기 상한 `WAITING_INPUT_TTL_HOURS`(기본 24) 초과 시 `cancelled(code=input_timeout)`.

기본값은 `approval_mode=off`다. 근거: 샌드박스가 격리되어 셸 명령의 피해 범위가 컨테이너 안으로 한정되고, 외부 부작용은 egress 허용 목록이 제한한다. 쓰기가 가능한 외부 API를 허용 목록에 넣는 에이전트는 `http_writes`를 켜도록 안내한다.

### 4.10 과업(Agent Task) 실행 모드

워크플로우 노드용 1회성 실행이며 1단계에서는 서비스 API(`POST /api/v1/agent-tasks`)와 내부 서비스로 제공한다.

- 입력: `agent_id`(발행 버전 사용; `agent_version_id`로 고정 가능), `inputs: {name: value|file_id}`, `instruction`(템플릿, `{{name}}` 치환은 Jinja2 `SandboxedEnvironment`), `output_schema`(JSON Schema object, `additionalProperties:false` 필수), `files`, `limits`(max_steps 등 덮어쓰기), `session_mode: ephemeral|reuse(session_id)`.
- 실행: `session(source=task)`를 만들고(ephemeral은 완료 후 샌드박스 즉시 종료, 스냅샷 없음) 첫 사용자 메시지로 렌더링된 지시 + 입력값 요약을 보낸다. 파일 입력은 `/home/agent/task/`에 두고 경로를 알려준다.
- 출력 강제: `output_schema`가 있으면 `submit_result(input_schema=output_schema, strict=true)` 도구를 추가하고 시스템 프롬프트에 "작업을 마치면 반드시 submit_result를 호출하라"를 넣는다. 호출이 오면 `jsonschema`로 검증(실패 시 오류 결과로 재시도, 최대 3회), 성공하면 루프 종료. 모델이 도구 호출 없이 턴을 끝내면 "submit_result를 호출해 결과를 제출하라"는 user 메시지로 1회 독촉 후 실패(`output_missing`). 근거: 최신 Anthropic 모델은 강제 tool_choice를 거부하므로 프롬프트 지시 + 검증 + 독촉으로 해결한다.
- 결과: `run.output = {status, result(JSON), text(마지막 어시스턴트 텍스트), files: [session_file...], usage}`. 2단계 엔진은 이 구조를 노드 출력 변수로 매핑한다.

## 5. 데이터 모델

공통 규칙:
- PK는 `id uuid`(v7, 시간 정렬. Python `uuid6` 패키지 또는 자체 생성). `run_event.id`만 `bigserial`.
- 모든 핵심 테이블에 `workspace_id uuid not null` + 인덱스. 리포지토리 계층(`app/db/repositories/*`)의 모든 조회가 `workspace_id` 조건을 강제한다(CI에서 `WorkspaceScopedRepository` 상속 여부 검사).
- `created_at`, `updated_at timestamptz` 기본. 소프트 삭제는 `agent`, `skill`, `session`에만 `deleted_at`.
- 암호화 컬럼은 `*_encrypted bytea`로 명명하고 `app/security/crypto.py`(AES-256-GCM, 키는 `.env` `ENCRYPTION_KEY` 32바이트 base64, 포맷 `v1|nonce|ciphertext|tag`)로만 읽고 쓴다. 표에서 🔒로 표시.
- JSONB 컬럼의 구조는 Pydantic 모델로 고정하고 `app/schemas/*`에 둔다.

### 5.1 테이블 목록

| 테이블 | 주요 컬럼 | 관계·인덱스 |
|---|---|---|
| `workspace` | `id, name, slug, settings jsonb(기본 한도, 유휴 시간, 세션 스냅샷 보존 등), created_at` | 1단계는 1행. 초기 설정 시 생성 |
| `account` | `id, email unique(lower), name, password_hash(argon2id), last_login_at` | 전역. 초기 설정에서 워크스페이스와 함께 1행 생성. 1단계는 계정 1개만 허용(`/setup`은 계정 0개일 때만 동작). 역할·멤버십·초대 테이블은 없다(ADR-0002) |
| `model_provider_credential` | `id, workspace_id, provider_type anthropic|openai_compatible, name, base_url, api_key_encrypted 🔒, extra_headers_encrypted 🔒(jsonb 직렬화), models jsonb[{id, display_name, context_window, max_output_tokens, supports_tools, supports_thinking, accepts_sampling_params, input_price_per_mtok?, output_price_per_mtok?, cache_read_price?, enabled}], config jsonb{max_tokens_param, supports_strict_tools}, last_synced_at, status, created_by` | 에이전트 버전이 `provider_credential_id + model_id`로 참조 |
| `agent` | `id, workspace_id, slug unique(workspace_id), name, icon(이모지 또는 storage_key), description, created_by, draft_version_id, published_version_id, webapp_enabled bool, deleted_at` | index(workspace_id, deleted_at) |
| `agent_version` | `id, workspace_id, agent_id, version_number int(null=draft), status draft|published, instructions text, model jsonb{provider_credential_id, model_id, params{temperature?, top_p?, max_output_tokens, effort?, thinking_display?}}, tools jsonb[string 그룹], limits jsonb{max_steps, max_run_seconds, max_total_tokens, max_cost_usd?, context_budget_tokens?, tool_timeout_sec}, sandbox jsonb{cpu, memory_mb, idle_timeout_sec?, persist_on_idle, image_tag?}, approval_mode off|http_writes|all_shell, env_vars jsonb[{name, value_encrypted 🔒, secret bool}], skill_version_ids uuid[], file_ids uuid[], snapshot_id, published_by, published_at, change_note` | unique(agent_id, version_number). draft는 agent당 1행(`agent.draft_version_id`) |
| `skill` | `id, workspace_id, scope library|embedded, agent_id(embedded만), slug, name, description, current_version_id, created_by, deleted_at` | unique(workspace_id, scope, coalesce(agent_id, uuid_nil), slug) |
| `skill_version` | `id, workspace_id, skill_id, version_number, manifest jsonb{name, description, version?, files:[{path,size,mode}]}, storage_key, size_bytes, sha256, skill_md text(캐시), created_by` | unique(skill_id, version_number) |
| `agent_file` | `id, workspace_id, agent_id, filename, mime, size_bytes, sha256, storage_key, uploaded_by` | 버전의 `file_ids`가 참조. 미참조 파일은 주기 정리 |
| `session` | `id, workspace_id, agent_id, agent_version_id, source preview|webapp|api|task, api_key_id?(source=api), external_user?(서비스 API의 user 문자열, 선택), title, status active|archived, sandbox_instance_id?, prompt_hash, persist_on_idle bool, metadata jsonb(API inputs, 과업 correlation 등), last_message_at, message_count, deleted_at` | index(workspace_id, agent_id, last_message_at desc), index(api_key_id, external_user, last_message_at desc). 별도 엔드유저 테이블은 두지 않는다 |
| `message` | `id, workspace_id, session_id, run_id?, seq int, role user|assistant|system_summary, blocks jsonb(CanonicalMessage.blocks + OpaqueBlock), text(표시·검색용 평문), stop_reason?, files jsonb[session_file_id], usage jsonb?, created_at` | unique(session_id, seq). index(session_id, seq) |
| `run` | `id, workspace_id, session_id, agent_version_id, trigger chat|task|resume, status queued|running|waiting_input|succeeded|failed|cancelled, user_message_id, assistant_message_id?, model jsonb{provider, model_id}, limits jsonb(적용값), input jsonb?(과업: inputs, instruction, output_schema), output jsonb?, pending_interaction jsonb?, resume_payload jsonb?, error jsonb?{code, message}, steps int, usage jsonb{input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd?, estimated bool}, queued_at, started_at, ended_at, runner_id` | index(session_id, queued_at), index(workspace_id, status), index(workspace_id, queued_at desc) |
| `run_event` | `id bigserial, workspace_id, run_id, seq int, type, payload jsonb, created_at` | unique(run_id, seq). 큰 도구 출력은 `payload.output_storage_key` |
| `session_file` | `id, workspace_id, session_id, run_id?, message_id?, kind upload|output, path(샌드박스 내 경로), filename, mime, size_bytes, sha256, storage_key?(null=미보존), reason?, created_at` | index(session_id, created_at) |
| `sandbox_instance` | `id, workspace_id, session_id, container_id, container_name, image, status creating|running|stopping|terminated|failed, token_encrypted 🔒, limits jsonb{cpu, memory_mb, pids}, restored_from_snapshot_id?, created_at, ready_at, last_activity_at, terminated_at, termination_reason?(idle_timeout|session_deleted|oom|disk_limit|error|manual|task_done)` | index(workspace_id, status), index(session_id, created_at desc) |
| `snapshot` | `id, workspace_id, kind published|session, agent_version_id?, session_id?, sandbox_instance_id?, storage_key, size_bytes(압축 후), home_bytes(압축 전), sha256, created_at, expires_at?` | index(session_id, created_at desc), index(expires_at) |
| `api_key` | `id, workspace_id, agent_id, name, key_prefix(앞 12자 표시용), key_hash(sha256), scopes jsonb(기본 ["chat","tasks"]), created_by, last_used_at, expires_at?, revoked_at?` | unique(key_hash). 원문은 생성 응답에 한 번만 |
| `background_job` | `id, workspace_id?, type snapshot_publish|cleanup|..., status, payload jsonb, result jsonb, attempts, run_after, created_at` | runner의 `jobs:queue` 보조 기록. 발행 스냅샷 등 비런 작업용 |

### 5.2 관계 요약

```
workspace 1 ─ account 1 (초기 설정에서 함께 생성, 역할 없음)
workspace 1─n model_provider_credential      agent_version.model → credential + model_id
workspace 1─n agent 1─n agent_version        agent_version n─m skill_version (uuid[]), n─m agent_file (uuid[]), 1─0..1 snapshot(published)
workspace 1─n skill 1─n skill_version
agent 1─n session 1─n message                session n─1 agent_version, 0..1 api_key(source=api), 0..1 sandbox_instance(현재)
session 1─n run 1─n run_event                run 1─1 user message, 0..1 assistant message
session 1─n session_file                     session 1─n snapshot(kind=session), 1─n sandbox_instance(이력)
agent 1─n api_key 1─n session(source=api)
```

### 5.3 상태 전이

- `agent_version`: `draft` ──publish──▶ 새 `published` 행 복제(draft는 남아 계속 편집). 발행 버전은 불변. `agent.published_version_id`만 바뀐다. 롤백 = 이전 발행 버전을 `published_version_id`로 지정(새 행 없음).
- `run`: `queued → running → (succeeded | failed | cancelled | waiting_input)`, `waiting_input → queued(resume) → running → ...`.
- `sandbox_instance`: `creating → running → stopping → terminated`, 어느 단계에서든 `failed`.
- `session`: `active ↔ archived`, `deleted_at` 설정 시 샌드박스 즉시 종료·세션 스냅샷 삭제·세션 파일 보존(로그용).

### 5.4 보존·정리 정책 (api 주기 태스크, 매시)

| 대상 | 규칙 |
|---|---|
| `snapshot(kind=session)` | `expires_at` 지난 것 삭제(파일+행). 세션당 최신 1개만 유지 |
| `run_event` | `RUN_EVENT_RETENTION_DAYS`(기본 90) 지난 런의 이벤트 삭제(메시지·런 요약은 유지) |
| `session_file(kind=upload)` | 세션 삭제 시 삭제 |
| Redis `run:{id}:events` | 런 종료 24h 후 자동 만료 |
| 미참조 `agent_file`, `skill_version` 패키지 | 참조하는 버전이 없고 7일 경과 시 삭제 |

## 6. API 설계

### 6.1 인증 방식

| API 군 | 경로 접두 | 인증 | 대상 |
|---|---|---|---|
| 콘솔 API | `/api/console/*` | httpOnly 쿠키 세션(`sid`, Redis `sess:<token>` → account_id, 7일 슬라이딩). CSRF: `SameSite=Lax` + 변경 요청은 `Content-Type: application/json` 필수 + `Origin` 검사 | 콘솔 SPA |
| 웹앱 API | `/api/web/*` | 콘솔과 동일 쿠키. 발행된 에이전트(`webapp_enabled`)에만 접근 허용 | 웹앱 화면(같은 SPA) |
| 서비스 API | `/api/v1/*` | `Authorization: Bearer <api_key>`. 키는 에이전트 1개에 묶임 | 외부 프로그램, 2단계 엔진 |
| 내부 API | `/internal/*` | `X-Internal-Token: <INTERNAL_API_TOKEN>`. gateway에서 외부 노출 차단(`location /internal { deny all; }`) | sandbox-manager, runner |
| 헬스 | `/healthz`, `/readyz` | 없음 | compose healthcheck |

권한 규칙: 역할 체계가 없다(ADR-0002). 로그인한 계정은 모든 콘솔·웹앱 API를 쓸 수 있고, 서비스 API는 API 키에 묶인 에이전트 범위만 접근한다. 리포지토리 계층의 `workspace_id` 범위 강제는 그대로 유지한다.

### 6.2 콘솔 API

| 메서드·경로 | 목적 |
|---|---|
| `GET /api/console/setup` / `POST /api/console/setup` | 초기 설정 상태 조회 / 계정과 워크스페이스 생성(계정 0개일 때만. 이후에는 404) |
| `POST /api/console/auth/login`, `POST .../logout`, `GET .../me` | 로그인(레이트 리밋 5회/분), 로그아웃, 현재 계정 |
| `POST /api/console/auth/password` | 비밀번호 변경 |
| `GET/POST /api/console/providers`, `PATCH/DELETE .../{id}`, `POST .../{id}/sync-models`, `POST .../{id}/test` | 프로바이더 자격증명 CRUD(응답에 키 미포함, `has_api_key`만), 모델 목록 동기화(`/v1/models`), 연결 테스트(짧은 호출) |
| `GET /api/console/models` | 활성 프로바이더의 사용 가능 모델 목록(에이전트 빌더 셀렉트용) |
| `GET/POST /api/console/agents`, `GET/PATCH/DELETE .../{id}` | 에이전트 목록·생성·메타 수정·삭제 |
| `GET /api/console/agents/{id}/draft`, `PUT .../draft` | 초안 조회·전체 저장(낙관적 잠금 `updated_at`) |
| `POST /api/console/agents/{id}/publish` | 발행(스냅샷 생성 포함, 202 + job id 또는 동기 60초) |
| `GET /api/console/agents/{id}/versions`, `GET .../versions/{vid}`, `POST .../versions/{vid}/restore` | 발행 이력, 상세, 롤백(published_version_id 변경) |
| `GET/POST /api/console/agents/{id}/files`, `DELETE .../files/{fid}` | 에이전트 첨부 파일(멀티파트, 파일 100MB) |
| `GET/POST /api/console/agents/{id}/skills`(embedded), `GET/POST /api/console/skills`(library), `GET/DELETE .../skills/{sid}`, `POST .../skills/{sid}/versions`(새 버전 업로드), `GET .../skills/{sid}/versions/{vid}/files/{path}` | 스킬 관리와 내용 보기 |
| `GET/POST /api/console/agents/{id}/api-keys`, `DELETE .../api-keys/{kid}` | API 키 생성(원문 1회 반환)·폐기 |
| `POST /api/console/agents/{id}/preview/sessions` | 미리보기 세션 생성(초안 사용). 응답 `session_id` |
| `POST /api/console/sessions/{sid}/messages` | 메시지 전송 → 런 생성. 본문 `{text, file_ids?, response_mode: streaming|blocking}`. streaming이면 응답이 SSE |
| `GET /api/console/sessions/{sid}/events?after=<id>` | 진행 중 런(또는 마지막 런) 이벤트 재접속 SSE |
| `POST /api/console/runs/{rid}/stop`, `POST .../runs/{rid}/input` | 중지, 질문·승인 응답 |
| `GET /api/console/sessions`, `GET .../sessions/{sid}`, `GET .../sessions/{sid}/messages`, `DELETE .../sessions/{sid}` | 세션 목록(필터: agent, source, api_key, external_user, 기간), 상세, 메시지(페이지), 삭제 |
| `GET /api/console/sessions/{sid}/files`, `GET .../files/{fid}/download`, `POST .../sessions/{sid}/uploads` | 세션 파일 목록·다운로드·업로드(메시지 첨부용, 응답 file_id) |
| `GET /api/console/sessions/{sid}/sandbox`, `DELETE .../sandbox` | 현재 샌드박스 상태, 강제 종료 |
| `GET /api/console/runs`, `GET .../runs/{rid}`, `GET .../runs/{rid}/events` | 런 목록(필터), 상세(usage 포함), 영속 이벤트(페이지) |
| `GET /api/console/runs/{rid}/tool-calls/{tcid}/output` | 도구 전체 출력 다운로드 |
| `GET /api/console/stats/usage?from&to&group_by=day|agent|model` | 토큰·비용·런 수 집계 |
| `GET /api/console/sandboxes` | 활성 샌드박스 목록 |
| `GET/PATCH /api/console/workspace` | 워크스페이스 설정(기본 한도, 유휴 시간 등) |

### 6.3 웹앱 API

| 메서드·경로 | 목적 |
|---|---|
| `GET /api/web/apps/{slug}` | 발행된 에이전트 공개 정보(이름·아이콘·설명·시작 문구) |
| `GET/POST /api/web/apps/{slug}/sessions` | 웹앱 세션 목록·새 세션(`source=webapp`, 발행 버전 사용) |
| `POST /api/web/sessions/{sid}/messages` | 메시지 전송(SSE). 콘솔과 같은 구현 |
| `GET /api/web/sessions/{sid}/messages`, `GET .../events`, `POST /api/web/runs/{rid}/stop`, `POST .../input`, `GET .../files/{fid}/download`, `POST .../uploads`, `PATCH /api/web/sessions/{sid}`(제목), `DELETE` | 콘솔 세션 API의 부분집합. `source=webapp` 세션만 |

### 6.4 서비스 API (Bearer API 키, 키에 묶인 에이전트의 발행 버전)

Dify `chat-messages` 계열과 유사한 형태로 설계하되 필드명은 우리 것이다. `user`는 모든 엔드포인트에서 선택이다. 주면 `session.external_user`에 저장되어 대화 목록을 그 값으로 구분·필터하고, 없으면 API 키 단위로 본다.

| 메서드·경로 | 목적 | 요청·응답 요약 |
|---|---|---|
| `POST /api/v1/chat-messages` | 메시지 전송 | `{query, user?, conversation_id?, inputs?, files?: [file_id], response_mode: streaming|blocking, metadata?}` → streaming: SSE(4.5), blocking: `{conversation_id, message_id, run_id, answer, files, usage, status}` |
| `POST /api/v1/chat-messages/{run_id}/stop` | 중지 | `{user}` → `{result: "success"}` |
| `POST /api/v1/chat-messages/{run_id}/input` | 질문·승인 응답 | `{user, answer? , decision?, note?}` |
| `GET /api/v1/chat-messages/{run_id}/events?after=` | 재접속 SSE | |
| `GET /api/v1/conversations?user&limit&cursor` | 대화 목록 | |
| `GET /api/v1/conversations/{id}?user` / `PATCH`(이름) / `DELETE` | 대화 상세·이름 변경·삭제 | |
| `GET /api/v1/messages?conversation_id&user&limit&cursor` | 메시지 목록(블록·파일 포함) | |
| `GET /api/v1/runs/{run_id}?user` | 런 상태·usage·output | 폴링용 |
| `POST /api/v1/files/upload` (multipart) | 파일 업로드 | `{user}` + 파일 → `{file_id, filename, size, mime}` |
| `GET /api/v1/files/{file_id}/download?user` | 파일 다운로드 | 세션 파일 |
| `POST /api/v1/agent-tasks` | 과업 실행(4.10) | `{user?, inputs, instruction, output_schema?, files?, limits?, session_mode, response_mode}` → streaming SSE 또는 blocking `{run_id, status, result, text, files, usage}` |
| `GET /api/v1/agent-tasks/{run_id}` | 과업 상태·결과 | |
| `GET /api/v1/info`, `GET /api/v1/parameters` | 에이전트 정보, 입력 파라미터·한도 | Dify의 info/parameters에 대응 |

레이트 리밋: 키당 60 req/분(Redis 슬라이딩 윈도), 초과 시 429 + `Retry-After`. 오류 형식은 전 API 공통 `{error: {code, message, details?}}`, 코드는 `snake_case`.

### 6.5 SSE 전송 규약

- 응답 헤더 `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- 각 이벤트: `id: <stream id>\nevent: <type>\ndata: <JSON 한 줄>\n\n`. 15초 무이벤트 시 `event: ping`.
- 종료: `run.completed` 또는 `run.error` 뒤 서버가 연결을 닫는다. `waiting_input`도 `run.completed(status=waiting_input)`로 닫힌다.
- 재접속: `?after=<마지막 id>` 또는 헤더 `Last-Event-ID`. Redis 스트림 만료(24h) 후에는 `GET /runs/{id}/events`(영속 이벤트)로 대체.
- 콘솔 SPA는 `fetch()` + `ReadableStream` 파서(`apps/web/src/lib/sse.ts`)로 소비한다.

### 6.6 내부 API

| 메서드·경로 | 호출자 | 목적 |
|---|---|---|
| `POST /internal/sandboxes/{id}/events` | sandbox-manager → api | `{type: terminated|failed|snapshot_created, reason, snapshot?: {storage_key, size, home_bytes, sha256}}` |
| `GET /internal/sandboxes/active` | sandbox-manager → api | 기동 시 대조용 활성 샌드박스 목록 |
| sandbox-manager `POST /sandboxes`, `GET /sandboxes/{id}`, `DELETE /sandboxes/{id}?snapshot=bool`, `GET /sandboxes`, `GET /healthz` | runner/api → manager | 생성 `{sandbox_id, session_id, workspace_id, limits, env, image_tag?}` → `{container_id, base_url, token}` |

## 7. 프론트엔드 설계

### 7.1 스택과 원칙

- Vite + React 19 + TypeScript(strict). SSR이 필요 없는 개인용 콘솔이라 Next.js 대신 정적 SPA를 택해 빌드·배포(nginx 정적 서빙)를 단순화한다.
- 라우팅 React Router v7(라이브러리 모드), 서버 상태 TanStack Query v5, 클라이언트 상태 Zustand(런 스트림 스토어, UI 상태), 폼 react-hook-form + zod, 스타일 Tailwind CSS v4 + Radix 기반 자체 컴포넌트(`src/components/ui`), 아이콘 lucide-react, 에디터 CodeMirror 6(지시문·SKILL.md·JSON), 마크다운 react-markdown + remark-gfm + rehype-highlight, i18n i18next(1단계 `ko`만, 문자열은 모두 `src/locales/ko/*.json`).
- API 타입: FastAPI OpenAPI → `openapi-typescript`로 `src/api/schema.d.ts` 생성, 호출은 `openapi-fetch`. 수동 타입 중복 금지.
- 레이아웃 원칙(한 줄): Dify 화면을 참고하지 않고 "좌측 좁은 내비게이션 레일 + 상단 컨텍스트 바 + 본문" 뼈대에 우리 디자인 토큰(`src/styles/tokens.css`)을 적용하며, 빌더는 Dify의 좌·우 분할 설정/미리보기 대신 탭형 설정 폼과 열고 닫는 미리보기 패널로 구성한다.

### 7.2 라우트 구조

| 경로 | 화면 | 접근 |
|---|---|---|
| `/setup` | 초기 설정(계정 생성) | 계정 0개일 때만 |
| `/login` | 로그인 | 비로그인 |
| `/` → `/agents` | 에이전트 목록(카드·검색·생성) | 로그인 |
| `/agents/:id` | 에이전트 빌더(탭: 설정·도구와 스킬·파일·환경변수·발행·API) + 미리보기 패널 | 로그인 |
| `/agents/:id/logs` | 에이전트별 세션·런 로그 | 로그인 |
| `/skills` | 라이브러리 스킬 목록·업로드·내용 보기 | 로그인 |
| `/logs` | 전체 세션·런 로그(필터: 에이전트·소스·기간·상태) | 로그인 |
| `/logs/sessions/:sid` | 세션 상세: 메시지 타임라인 + 런·스텝·도구 호출 인스펙터 | 로그인 |
| `/settings/providers`, `/settings/workspace`, `/settings/sandboxes` | 설정(프로바이더, 기본 한도, 활성 샌드박스) | 로그인 |
| `/account` | 내 계정(비밀번호) | 로그인 |
| `/apps/:slug`, `/apps/:slug/s/:sid` | 웹앱(채팅, 대화 목록) | 로그인 |

### 7.3 화면별 핵심 컴포넌트와 상태

| 화면 | 컴포넌트 | 상태·데이터 |
|---|---|---|
| 에이전트 빌더 | `AgentForm`(react-hook-form, 초안 전체를 하나의 폼 값으로), `ModelPicker`(프로바이더→모델, 파라미터 슬라이더, effort 셀렉트), `ToolGroupToggles`, `SkillPicker`(라이브러리 선택 + 임베디드 업로드), `FileList`, `EnvVarEditor`(secret 토글, 저장 후 값 미표시), `LimitsEditor`, `PublishPanel`(변경 메모, 발행 이력, 롤백), `ApiKeyPanel`(생성 모달 1회 표시, curl 예시) | `useQuery(agent draft)`, 저장은 `PUT draft`(디바운스 없음, 명시 저장 버튼 + 더티 표시). 발행 시 202면 job 폴링 |
| 미리보기 패널 | `ChatPanel`(아래 공통), 상단에 "초안 기준" 배지, 세션 초기화 버튼 | 세션 id는 패널 로컬 상태 |
| 공통 채팅 `ChatPanel` | `MessageList`, `AssistantMessage`(마크다운 스트리밍), `ToolCallCard`(이름·입력 요약, 펼치면 입력 JSON·stdout/stderr 라이브 터미널 뷰·종료 코드·소요 시간), `FileChangeList`(다운로드 버튼), `InteractionPrompt`(질문 옵션 버튼·자유 입력, 승인/거절), `Composer`(텍스트·첨부·전송/중지), `RunStatusBar`(스텝 n/limit, 토큰, 경과 시간), `CompactionDivider` | Zustand `runStore`: `{runId, status, messages[], toolCalls{}, files[], pendingInteraction, usage}`를 SSE 이벤트 리듀서로 갱신. 메시지 목록 초기 로드는 TanStack Query, 스트림 중 추가는 스토어 |
| 로그/세션 상세 | `SessionHeader`(소스·API 키·외부 사용자 식별자·버전·샌드박스 상태), `RunList`, `RunInspector`(스텝 타임라인, 도구 호출 입출력 전체 보기·다운로드, usage), `EventTable`(원본 이벤트 JSON) | `useInfiniteQuery` 페이지네이션 |
| 설정 | `ProviderForm`(키 입력 후 마스킹, 모델 동기화 결과 테이블, 가격 입력), `WorkspaceLimitsForm`, `SandboxTable`(강제 종료) | TanStack Query + mutation 후 invalidate |
| 웹앱 | `AppShell`(에이전트 아이콘·이름·설명), `SessionSidebar`, `ChatPanel` 재사용 | 콘솔과 같은 스토어·파서 |

### 7.4 실시간 스트림 처리

- `src/lib/sse.ts`: `fetch(url, {method, body, signal})` → `ReadableStream`을 줄 단위로 파싱해 `{id, event, data}`를 콜백으로 전달. 네트워크 오류 시 마지막 `id`로 `GET .../events?after=` 재접속(지수 백오프 1·2·4·8초, 최대 5회). 탭 비활성화와 무관하게 유지.
- `runStore.applyEvent(ev)`: 타입별 리듀서. `message.delta`는 현재 어시스턴트 메시지 버퍼에 append(렌더는 `requestAnimationFrame` 배치로 16ms마다 1회). `tool_call.output_delta`는 도구 호출별 링 버퍼(최대 200KB 표시, 초과분은 "전체 출력 보기" 링크).
- 새로고침 복구: 세션 열 때 `GET /sessions/{sid}`의 `active_run`이 있으면 자동으로 `events?after=0` 구독.
- 전송 중 UI: Composer 비활성, 중지 버튼 활성. `run.completed(waiting_input)`이면 `InteractionPrompt` 표시.

### 7.5 품질 도구

ESLint(flat config, typescript-eslint, react-hooks, jsx-a11y), Prettier, Vitest + Testing Library(스토어 리듀서·SSE 파서·폼 검증 단위 테스트), Playwright 스모크 1개(로그인→에이전트 생성→미리보기 메시지→도구 카드 표시), `tsc --noEmit`을 CI에서 실행.

## 8. 저장소 구조

### 8.1 모노레포 디렉터리

```
Workflow_Elon/
├─ CLAUDE.md, README.md, LICENSE(Apache 2.0), .gitattributes(LF 강제), .editorconfig, .gitignore(.env·data·backups·스냅샷 제외), .env.example
├─ docs/                      decisions/, design/, research/, ops/(설치·백업·운영 가이드, M10)
├─ apps/
│  ├─ api/                    백엔드 패키지 (api · runner · sandbox-manager 공용 코드, 하나의 Docker 이미지)
│  │  ├─ pyproject.toml        uv 관리. 의존: fastapi, uvicorn[standard], sqlalchemy[asyncio], asyncpg, alembic,
│  │  │                        pydantic-settings, redis, httpx, anthropic, openai, docker, cryptography, argon2-cffi,
│  │  │                        structlog, jsonschema, jinja2, python-multipart, uuid6
│  │  ├─ alembic.ini, migrations/versions/
│  │  ├─ app/
│  │  │  ├─ main.py            FastAPI 앱 조립(라우터·미들웨어·수명주기)
│  │  │  ├─ config.py          Settings(pydantic-settings, .env)
│  │  │  ├─ logging.py         structlog JSON, 비밀 마스킹 프로세서
│  │  │  ├─ db/                engine.py, base.py, models/*.py(테이블별), repositories/*.py(WorkspaceScopedRepository)
│  │  │  ├─ schemas/           Pydantic 요청·응답·JSONB 모델
│  │  │  ├─ security/          crypto.py(AES-GCM), passwords.py(argon2), tokens.py(api key, 세션), rate_limit.py
│  │  │  ├─ auth/              session_store.py(Redis), dependencies.py(current_account, api_key_auth)
│  │  │  ├─ api/               라우터: console/{setup,auth,providers,agents,skills,files,sessions,runs,stats,workspace,sandboxes}.py,
│  │  │  │                     web/{apps,sessions}.py, v1/{chat_messages,conversations,messages,files,agent_tasks,info}.py, internal/{sandboxes}.py, health.py
│  │  │  ├─ services/          비즈니스 로직: agents.py(초안·발행), skills.py, sessions.py, runs.py(생성·큐·중지·재개), files.py, providers.py, usage.py
│  │  │  ├─ streaming/         sse.py(SSE 인코더·중계), run_stream.py(Redis Stream 읽기/쓰기 래퍼)
│  │  │  ├─ queue/             redis_queue.py(BLMOVE 큐, 하트비트, 취소 플래그)
│  │  │  ├─ storage/           base.py(StorageBackend 프로토콜), local_fs.py(/data)
│  │  │  ├─ llm/               base.py, types.py, catalog.py, anthropic_adapter.py, openai_adapter.py, pricing.py, token_estimate.py
│  │  │  ├─ agent/             loop.py(AgentLoop), context.py(히스토리 로드·압축), prompt_builder.py, prompts/system_preamble.md, prompts/compaction.md,
│  │  │  │                     tools/{base,shell,run_python,read_file,write_file,edit_file,list_files,http_request,ask_user,submit_result}.py, interactions.py, file_collector.py
│  │  │  ├─ sandbox/           client.py(SandboxClient: sandboxd HTTP), manager_client.py(sandbox-manager HTTP), provisioning.py(확보·복원·동기화)
│  │  │  ├─ runner/            __main__.py, worker.py(런 소비·실행·이벤트 발행·영속), jobs.py(스냅샷 발행 등 보조 작업), recovery.py
│  │  │  ├─ sandbox_manager/   __main__.py(FastAPI, 7100), docker_backend.py(유일한 Docker SDK 사용처), limits.py, reaper.py, reconcile.py, callbacks.py
│  │  │  └─ skills/            package.py(zip 검증·SKILL.md 파싱), sync.py
│  │  └─ tests/                unit/(어댑터·루프·압축·패키지·crypto), integration/(API + DB + Redis), sandbox/(Docker 필요, 마커 `docker`)
│  ├─ sandboxd/                컨테이너 내부 서버. pyproject.toml(starlette, uvicorn), sandboxd/{__main__,app,exec,fs,http_proxy,auth}.py, tests/
│  └─ web/                     Vite React. src/{api,app(routes),components/{ui,chat,builder,logs,settings},features,lib(sse.ts, format.ts),stores,locales,styles}, e2e/
├─ packages/
│  └─ event-schema/            run 이벤트·과업 I/O JSON Schema(단일 출처). api가 Pydantic으로 생성해 내보내고 web이 타입 생성에 사용
├─ infra/
│  ├─ docker/                  compose.yaml, compose.dev.yaml, compose.hardened.yaml(socket-proxy), api.Dockerfile, gateway.Dockerfile, nginx/{nginx.conf, default.conf}
│  ├─ sandbox-image/           Dockerfile, seccomp.json, home-skeleton/, build.sh
│  └─ egress/                  squid.conf, allowlist.txt
├─ scripts/                    dev-up.sh, smoke.sh(헬스체크), smoke-api.sh(서비스 API curl), backup.sh, restore.sh, gen-api-types.sh
└─ .github/workflows/          ci.yml(lint·test·compose 스모크만. 이미지 푸시·배포 자동화 없음, ADR-0002)
```

### 8.2 개발 명령

| 작업 | 명령 |
|---|---|
| 최초 설치 | `cp .env.example .env` 후 비밀값 생성: `python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"`로 `ENCRYPTION_KEY`, `INTERNAL_API_TOKEN`, `SESSION_SECRET` 채움 |
| 전체 기동(개발) | `docker compose -f infra/docker/compose.yaml -f infra/docker/compose.dev.yaml up -d --build` (api·runner는 소스 마운트 + `--reload`) |
| 샌드박스 이미지 | `bash infra/sandbox-image/build.sh` → `platform-sandbox:dev` 태그. compose 기동 전 1회 |
| 백엔드 로컬 | `cd apps/api && uv sync && uv run uvicorn app.main:app --reload` (Postgres·Redis는 compose로) |
| 마이그레이션 | `uv run alembic revision --autogenerate -m "..."`, `uv run alembic upgrade head` (api 컨테이너 기동 시 자동 `upgrade head`, `AUTO_MIGRATE=true`) |
| 백엔드 테스트 | `uv run pytest -m "not docker"`(단위·통합), `uv run pytest -m docker`(샌드박스, Docker 필요) |
| 린트·타입 | `uv run ruff check . && uv run ruff format --check . && uv run pyright` |
| 프론트 | `cd apps/web && pnpm i && pnpm dev`(5173, `/api` → `localhost:8080` 프록시), `pnpm test`, `pnpm lint`, `pnpm typecheck`, `pnpm build` |
| API 타입 생성 | `bash scripts/gen-api-types.sh` (api 기동 상태에서 `/openapi.json` → `apps/web/src/api/schema.d.ts`) |
| 스모크 | `bash scripts/smoke.sh`(모든 서비스 healthz), `bash scripts/smoke-api.sh <api_key>` |
| 백업·복원 | `bash scripts/backup.sh <dir>`(pg_dump + storage tar), `bash scripts/restore.sh <dir>` |

### 8.3 코드 품질 도구

- Python: `uv`, `ruff`(lint+format, 규칙 `E,F,I,B,UP,S(bandit 일부)`), `pyright`(basic, `app/`에 `strict` 점진 적용), `pytest`·`pytest-asyncio`·`httpx.AsyncClient`, `import-linter`(계층 규칙: `app.llm`·`app.agent`·`app.sandbox`는 `app.api`·`app.db`를 import 금지 → 2단계 재사용 보장).
- TypeScript: 7.5절.
- 공통: pre-commit(ruff, prettier, eslint, 비밀 패턴 검사 `gitleaks`), Conventional Commits, CI에서 compose 기동 후 `smoke.sh` 실행.

## 9. 기술 선택과 트레이드오프

| # | 결정 | 대안 | 근거 |
|---|---|---|---|
| T1 | 백엔드 **FastAPI + SQLAlchemy 2 async + Pydantic v2** | Django(동기, SSE·asyncio 궁합 약함), Litestar(생태계 작음), Node/NestJS(LLM SDK·Docker SDK 생태계가 Python보다 얇고 팀 스택 분산) | 스트리밍·asyncio 중심 워크로드에 맞고, Anthropic·OpenAI·Docker SDK가 모두 1급 지원. OpenAPI 자동 생성으로 프론트 타입 동기화. Dify가 가려는 방향(FastAPI 전환)을 처음부터 |
| T2 | **세션·런 상태의 원본은 Postgres, Redis는 휘발성 보조** | 전부 Redis(내구성 약함, 쿼리 불가), 전부 Postgres(큐·스트림을 LISTEN/NOTIFY·폴링으로 구현 가능하나 재생 버퍼·만료가 번거로움) | 로그 조회·집계·백업은 관계형이 맞고, 큐·이벤트 스트림·플래그는 Redis 자료구조가 간단하다. Redis가 날아가면 진행 중 런만 실패하고 데이터는 보존 |
| T3 | **자체 asyncio 러너(BLMOVE 큐)** | Celery(prefork + asyncio 스트리밍 궁합 나쁨, revoke 불안정, 구성 복잡), arq(간단하지만 "작업 함수 1회 실행" 모델이라 스트리밍·취소·재개를 덧붙이면 결국 같은 코드), Temporal(1단계엔 과함) | 런은 "수 분간 이벤트를 흘리는 상태 기계"라 작업 큐 추상화보다 직접 쓴 150줄짜리 워커가 명확하다. 하트비트·복구·취소를 우리가 정의. 2단계 HITL 장기 대기가 커지면 Temporal 재검토(12장) |
| T4 | **별도 sandbox-manager 서비스 + 컨테이너 내 sandboxd** | runner가 Docker SDK 직접 제어(소켓이 LLM 호출 프로세스에 노출, exec 스트리밍 복잡), dify-sandbox류 무상태 실행기(상태 유지·패키지 설치 요구와 불일치) | Docker 소켓 보유 범위를 최소화하고, 도구 실행 프로토콜을 Docker에서 분리해 런타임 교체(gVisor·VM·외부 서비스) 여지를 남긴다 |
| T5 | **SSE** | WebSocket | 2.4절. 단방향으로 충분, 프록시·인증·재접속이 단순, Dify 호환 형태 |
| T6 | **Vite SPA + React Router** | Next.js(App Router) | SSR·RSC 불필요, 빌드 산출물이 정적 파일이라 gateway 이미지 하나로 끝남. 빌드 복잡도와 런타임 서비스 1개 감소 |
| T7 | **squid egress 프록시** | 자체 Python 프록시, Envoy, mitmproxy | 검증된 ACL·CONNECT 처리, 설정 파일 몇 줄. 자체 구현은 보안 버그 위험. Envoy는 과함 |
| T8 | **스냅샷 tar.gz 로컬 스토리지(`StorageBackend` 추상화)** | Docker commit 이미지(크고 느림, 레지스트리 필요), S3 즉시 도입 | 단일 VM에 맞고 백업이 단순. S3 백엔드는 인터페이스만 남김 |
| T9 | **Anthropic·OpenAI 공식 SDK 직접 사용, 자체 정규화 계층** | LiteLLM(의존 거대, 추상화 누수), LangChain(불필요한 계층) | 두 프로바이더만 지원하므로 어댑터 2개가 가장 작고 투명하다. 스트리밍·도구 호출 형식 차이를 `ModelEvent`로 흡수 |
| T10 | **커스텀 도구 스키마(프로바이더 내장 bash/text_editor 미사용)** | Anthropic 전용 도구 | 두 프로바이더 동일 동작, 입력 검증·UI 렌더링을 우리가 통제 |
| T11 | **로그인 세션은 Redis 서버측 세션(쿠키)** | JWT, 로그인 없음 | 즉시 폐기 가능, 구현 단순. 사용자 1명이지만 로그인은 유지한다. 로컬 포트를 다른 프로그램이 호출하는 것을 막는 최소 장치이고 서비스 API 키와 구분된다 |
| T12 | **uuid v7 PK** | bigserial, uuid v4 | 시간 정렬로 인덱스 지역성 확보, 분산 생성 가능, URL 노출 시 추측 불가 |
| T13 | **히스토리 압축은 "전체 요약 1개" 방식** | 오래된 도구 결과 삭제, 꼬리 보존 요약 | Anthropic 최신 모델의 thinking 블록 바인딩 제약(4.7)과 프로바이더 중립을 모두 만족하는 유일한 단순 형태 |
| T14 | **역할·권한 체계 없음** | RBAC | 사용자 1명(ADR-0002). 리포지토리의 workspace 범위와 API 키의 에이전트 범위만 강제한다. 다인 사용으로 넓히면 account·session에 소유자 컬럼을 추가하는 마이그레이션으로 대응 |

## 10. 단계별 구현 계획

각 마일스톤은 implementer 1명이 받을 수 있는 단위다. "병렬" 열의 같은 글자는 동시에 진행 가능함을 뜻한다. 모든 마일스톤은 (1) 지정 테스트 통과 (2) `ruff/pyright/eslint/tsc` 통과 (3) 아래 검증 절차 수행 후 종료한다.

| M | 이름 | 의존 | 병렬 |
|---|---|---|---|
| M1 | 스캐폴딩 + compose 빈 서비스 기동 + 헬스체크 | - | - |
| M2 | 백엔드 기반: 설정·DB·마이그레이션·단일 계정 인증·프로바이더·암호화 | M1 | - |
| M3 | 샌드박스 이미지 + sandboxd + sandbox-manager + egress 프록시 | M1 | A |
| M4 | 모델 어댑터 2종 + 정규화 + 토큰·가격 | M1 | A |
| M5 | 에이전트·버전·스킬·파일·API 키 CRUD | M2 | A |
| M6 | 에이전트 루프 + runner + 내장 도구 + 세션/런/이벤트 API(콘솔) | M3, M4, M5 | - |
| M7 | 웹 콘솔(인증·에이전트·빌더·미리보기·설정) | M2(인증 API) 이후 시작, 완성은 M6 | B(M3·M4·M5와 병렬 시작) |
| M8 | 발행: 스냅샷·웹앱·서비스 API·과업 API | M6 | C |
| M9 | 로그·관리 화면 + 사용량 집계 | M6 | C |
| M10 | 하드닝·운영: 보안 체크리스트 통과, 백업, 문서, e2e | M8, M9 | - |

### M1. 스캐폴딩과 빈 서비스 기동

- 생성: 루트 설정 파일들, `apps/api/pyproject.toml`, `apps/api/app/{main,config,logging}.py`, `app/api/health.py`, `app/runner/__main__.py`(큐 없이 하트비트 로그만), `app/sandbox_manager/__main__.py`(`/healthz`만), `apps/sandboxd/`(`/health`만), `apps/web/`(Vite 템플릿 + "준비 중" 페이지), `infra/docker/{compose.yaml, compose.dev.yaml, api.Dockerfile, gateway.Dockerfile, nginx/*}`, `infra/sandbox-image/{Dockerfile, build.sh, seccomp.json(Docker 기본 복사)}`, `infra/egress/{squid.conf, allowlist.txt}`, `scripts/{dev-up.sh, smoke.sh}`, `.github/workflows/ci.yml`, `.env.example`.
- 검증: `docker compose ... up -d --build` 후 `scripts/smoke.sh`가 gateway(`/`, `/api/healthz`), api, sandbox-manager, redis, postgres, egress-proxy(`squidclient` 또는 `curl -x` 로 pypi 200, example.org 403) 모두 OK 출력. Windows Docker Desktop에서 확인하고, CI(Linux)에서 같은 스크립트를 돌린다.

### M2. 백엔드 기반

- 생성: `app/db/{engine,base}.py`, `app/db/models/*.py`(5.1 전부. 이후 마일스톤은 컬럼 추가만), `migrations/versions/0001_initial.py`, `app/db/repositories/{base,accounts,workspaces,providers}.py`, `app/security/*`, `app/auth/*`, `app/api/console/{setup,auth,providers,workspace}.py`, `app/services/providers.py`, `app/llm/catalog.py`(모델 능력 기본표), `tests/integration/test_{setup,auth,providers}.py`, `tests/unit/test_crypto.py`.
- 규칙: 모든 리포지토리는 `WorkspaceScopedRepository`를 상속하고 `workspace_id` 없는 쿼리를 만들 수 없다(테스트로 강제).
- 검증: 통합 테스트(compose의 Postgres·Redis 사용, 테스트 DB `platform_test`) 통과. 수동: `/setup`로 계정 생성 → 로그인 → 비밀번호 변경 → 로그아웃 후 `GET /api/console/agents`가 401 → `/setup` 재호출이 404. 프로바이더 등록 후 `psql`에서 `api_key_encrypted`가 평문이 아님 확인, `sync-models`가 Anthropic·OpenAI 호환 양쪽에서 모델 목록 반환.

### M3. 샌드박스

- 생성: `infra/sandbox-image/Dockerfile`(3.3 완성), `apps/sandboxd/sandboxd/*.py`(3.11 API 전부), `apps/sandboxd/tests/`, `app/sandbox_manager/{docker_backend,limits,reaper,reconcile,callbacks}.py`, `app/sandbox/{client,manager_client}.py`, `app/api/internal/sandboxes.py`(콜백 수신·행 갱신), `tests/sandbox/test_{lifecycle,exec,fs,snapshot,network,limits}.py`(마커 `docker`).
- 검증(자동): 생성 → `/health` → `exec("id")`가 `uid=1000` → `exec("cat /proc/self/status | grep CapEff")`가 `0000000000000000` → `write/read/list` → `snapshot` → 새 샌드박스에 `restore` 후 파일 존재 → `exec(":(){ :|:& };:")`가 pids 제한으로 실패하고 샌드박스 생존 → `exec("python3 -c 'a=bytearray(3*1024**3)'")`가 OOM으로 비 0 종료 → `exec("curl -sS -o /dev/null -w '%{http_code}' https://pypi.org/simple/")`=200, `example.org`=403, `curl http://postgres:5432` 실패 → 11개 동시 생성 시 11번째 429 → 유휴 타임아웃(테스트용 20초)에 컨테이너 소멸과 api 콜백 수신.
- 검증(수동): `docker inspect`로 `ReadonlyRootfs=true`, `CapDrop=[ALL]`, `SecurityOpt`에 seccomp·no-new-privileges, 네트워크가 `sandbox-net`만.

### M4. 모델 어댑터

- 생성: `app/llm/{base,types,anthropic_adapter,openai_adapter,pricing,token_estimate}.py`, `tests/unit/llm/fixtures/`(두 프로바이더의 실제 스트림 응답을 녹음한 SSE 픽스처: 텍스트만, 단일 도구 호출, 병렬 도구 호출 2개, max_tokens 절단, refusal/content_filter, usage 포함), `tests/unit/llm/test_{anthropic,openai,normalize}.py`, `tests/integration/test_llm_live.py`(환경변수 키 있을 때만 실행).
- 검증: 픽스처 재생으로 두 어댑터가 동일한 `ModelEvent` 시퀀스를 내는지 스냅샷 테스트. 정규 메시지 ↔ 프로바이더 메시지 변환 왕복 테스트(OpaqueBlock 보존·제거 포함). 라이브 테스트: 각 프로바이더에 `shell` 도구 1개 주고 "ls를 실행해"라고 하면 `tool_call_start/delta/end`가 나온다.

### M5. 에이전트·스킬·파일·API 키

- 생성: `app/api/console/{agents,skills,files}.py`(api-keys 포함), `app/services/{agents,skills,files}.py`, `app/skills/package.py`, `app/storage/{base,local_fs}.py`, `app/schemas/agent_version.py`, `tests/integration/test_{agents,skills,api_keys}.py`, `tests/unit/test_skill_package.py`.
- 발행은 이 단계에서 "스냅샷 없이 버전 복제"까지만 구현하고 스냅샷 연결은 M8.
- 검증: 초안 생성·수정·발행·롤백 흐름 테스트. 스킬 zip 검증 테스트(프런트매터 누락·경로 탈출 `../`·심볼릭 링크·50MB 초과 거부). API 키는 원문이 생성 응답에만 있고 DB에는 해시만.

### M6. 에이전트 루프와 runner

- 생성: `app/agent/{loop,context,prompt_builder,interactions,file_collector}.py`, `app/agent/prompts/*.md`, `app/agent/tools/*.py`, `app/sandbox/provisioning.py`, `app/queue/redis_queue.py`, `app/streaming/{sse,run_stream}.py`, `app/runner/{worker,jobs,recovery}.py`, `app/services/{sessions,runs}.py`, `app/api/console/{sessions,runs}.py`, `packages/event-schema/`(Pydantic→JSON Schema 내보내기 스크립트), `tests/unit/agent/test_{loop,compaction,prompt_builder,tools_schema}.py`(가짜 어댑터·가짜 샌드박스로 루프 전체 시나리오), `tests/integration/test_run_stream.py`(큐→runner→SSE 중계 end-to-end, 가짜 어댑터).
- 루프 시나리오 테스트 목록: 텍스트만 응답 / 도구 1회 후 응답 / 병렬 도구 2개 순차 실행 / 도구 오류 후 모델 재시도 / max_steps 도달 / 취소 / 도구 입력 JSON 오류 / 압축 발생(가짜 어댑터가 큰 usage 반환) / ask_user 일시정지와 재개 / approval 거절 / 샌드박스 사망 후 재생성 / max_tokens 재시도 / refusal.
- 검증(수동, curl + 실제 LLM): 콘솔 API로 미리보기 세션 → "requests 설치하고 pypi.org 상태 코드 알려줘" → SSE에 `tool_call.*`·`message.delta`·`file.changed`·`usage`·`run.completed`가 순서대로 온다. 중지 API 호출 시 3초 내 `run.completed(cancelled)`. `ask_user`를 유도하는 프롬프트로 `waiting_input` 후 `input` API로 재개.

### M7. 웹 콘솔

- 생성: `apps/web/src/*` 7장 전부(웹앱 라우트는 M8에서 추가), `scripts/gen-api-types.sh`, Vitest 테스트(`runStore` 리듀서, `sse.ts` 파서, 폼 스키마).
- 시작 조건: M2의 OpenAPI로 인증·프로바이더·에이전트 화면을 먼저 만들고, 채팅 패널은 M6의 이벤트 스키마(`packages/event-schema`)가 확정되면 붙인다. 그 전에는 녹음된 이벤트 픽스처로 UI를 개발한다.
- 검증: Playwright 스모크(로그인→에이전트 생성→모델 선택→저장→미리보기 메시지→도구 카드와 스트리밍 텍스트 표시→파일 다운로드). 수동: 새로고침 후 진행 중 런 자동 재구독, 중지 버튼, 질문 응답 UI.

### M8. 발행

- 생성: `app/runner/jobs.py`에 발행 스냅샷 작업, `app/services/agents.py` 발행 흐름 완성(3.6), `app/api/web/*`, `app/api/v1/*`, `app/auth/dependencies.py`에 api_key 인증·세션 매핑(`external_user`), `app/security/rate_limit.py`, 과업 모드(`app/agent/tools/submit_result.py`, `app/services/tasks.py`), `apps/web`의 `/apps/:slug` 라우트와 빌더 API 탭(curl 예시·키 생성), `scripts/smoke-api.sh`, `tests/integration/test_{service_api,webapp,agent_tasks,publish_snapshot}.py`.
- 검증: A7(발행 전 파일이 새 세션에 존재), A8, A9, A14. `smoke-api.sh`가 chat-messages streaming → conversations → messages → stop → agent-tasks(스키마 검증)까지 통과. 레이트 리밋 61번째 요청 429.

### M9. 로그·관리·사용량

- 생성: `app/api/console/{stats,sandboxes}.py`, `app/services/usage.py`(집계 SQL), `apps/web` 로그 화면·세션 상세·런 인스펙터·설정 하위 화면(샌드박스), `tests/integration/test_stats.py`.
- 검증: A10. 소스별 필터, 도구 출력 전체 다운로드, 샌드박스 강제 종료 후 세션 재개 동작, 집계 값이 `run.usage` 합과 일치.

### M10. 하드닝과 운영

- 생성·수정: `compose.hardened.yaml`(Linux 선택: docker-socket-proxy, gVisor 런타임 옵션), `scripts/{backup,restore}.sh`, `docs/ops/{install,operations,backup-restore,security}.md`, 보존 정리 태스크, 로그 마스킹 검증 테스트, 11장 체크리스트 각 항목의 자동 테스트 또는 수동 절차 기록, Playwright e2e 확장.
- 검증: 11장 체크리스트 전 항목에 "확인 방법·결과" 기입, A1·A11·A12·A15 재확인, Docker Desktop 데이터를 초기화한 상태에서 설치 문서만으로 설치 성공.

## 11. 보안 체크리스트

M10에서 각 항목에 확인 방법과 결과를 기입한다. "대응"은 설계상 조치이며 구현 시 그대로 적용한다.

위협 모델(ADR-0002): 사용자는 본인 1명이며 신뢰한다. 신뢰할 수 없는 것은 (1) 모델이 생성해 샌드박스에서 실행하는 코드와 명령, (2) 모델이 읽는 웹 콘텐츠·파일·도구 결과 속 지시(프롬프트 인젝션), (3) 샌드박스에 설치되는 외부 패키지, (4) 같은 PC의 다른 프로그램이 로컬 포트를 호출할 가능성이다. 따라서 샌드박스 격리, egress 허용 목록, 비밀 보호, 로그인은 유지하고, 사용자 간 격리와 역할 관련 항목은 두지 않는다.

| # | 위협 | 대응 | 확인 |
|---|---|---|---|
| S1 | 샌드박스 탈출(커널 취약점, capability 남용) | 비루트, `cap_drop ALL`, `no-new-privileges`, seccomp 강화 프로파일, 읽기 전용 루트, pids·메모리 제한, 호스트 마운트·privileged 금지(코드 리뷰 항목), 운영 시 gVisor·userns-remap 권장, 베이스 이미지 월 1회 재빌드 | M3 자동 테스트 + `docker inspect` |
| S2 | 샌드박스 간 침투, 샌드박스 → 플랫폼 내부 서비스 접근 | 컨테이너마다 무작위 Bearer 토큰, sandboxd는 토큰 없는 요청 401, 홈 볼륨 분리, 같은 계정 세션도 샌드박스 분리. sandbox-manager 7100은 app-net 주소에만 bind + sandbox-net 출발지 거부(3.8절 7). runner는 수신 포트가 없고 api·postgres·redis는 sandbox-net에 없다 | 테스트: A 샌드박스에서 B의 7000 포트 호출 시 401, sandbox-manager:7100 호출 시 연결 거부 또는 403 |
| S3 | SSRF(샌드박스 → 내부 서비스, 메타데이터 엔드포인트) | `sandbox-net` internal + egress-proxy의 사설 대역·링크로컬(169.254/16, 100.64/10) 차단 + 도메인 허용 목록 + CONNECT 443만. `http_request`도 샌드박스 안에서 실행. egress-proxy는 `egress-net`에만 양다리라 `postgres`·`redis` 이름 해석 불가 | A11, M3 네트워크 테스트 |
| S4 | SSRF(api 서버 자체) | api는 사용자가 준 URL을 서버에서 가져오는 기능이 없다(프로바이더 base_url 제외). 프로바이더 base_url은 콘솔에서만 설정하며 `http(s)://`만 허용하고 사설 대역은 경고 표시(로컬 vLLM·Ollama 허용 필요) | 코드 리뷰 |
| S5 | 비밀 노출(프로바이더 키·환경변수·API 키) | AES-256-GCM 저장, 응답에 미포함(`has_api_key`), 로그 마스킹 프로세서(키 이름 패턴 + `sk-`·`Bearer ` 값 패턴), 프로바이더 오류 본문 마스킹, 샌드박스 환경변수는 해당 에이전트 버전 것만 주입, 시스템 프롬프트에 "환경변수·키를 출력·전송하지 말 것" 명시. `ENCRYPTION_KEY` 분실 시 복구 불가 문서화 | 단위 테스트(마스킹), `psql` 확인 |
| S6 | 프롬프트 인젝션으로 위험 명령 실행 | 피해 범위를 샌드박스+허용 목록으로 구조적으로 제한, 파일·웹 내용을 데이터로 취급하라는 서문, `approval_mode`(http_writes/all_shell) 옵션, 도구 호출 전체 기록, 쓰기 가능한 외부 API를 허용 목록에 넣을 때 승인 모드 권장 안내 | 수동: 악성 지시가 든 파일을 읽게 하고 외부 전송 시도가 프록시에서 차단되는지 |
| S7 | API 키 유출 | 해시만 저장, 접두 표시, 폐기 즉시 반영, 키당 레이트 리밋, 키는 에이전트 1개 범위, `last_used_at` 기록, gateway는 127.0.0.1 바인드라 외부 노출 없음 | 테스트: 폐기 후 401 |
| S8 | 로그에 비밀 기록 | S5 마스킹 + `run_event`에 `http_request`의 `Authorization`·`Cookie`·`X-Api-Key` 헤더 값 `***` 치환 + 환경변수 값은 이벤트에 기록하지 않음 + 구조화 로그에 요청 본문 미기록(DEBUG에서도 헤더 제거) | 단위 테스트 |
| S9 | Docker 소켓 노출 | 3.8절 6개 조치 | compose 검토, hardened 프로파일 테스트 |
| S10 | 인증·세션(같은 PC의 다른 프로그램이 로컬 포트를 호출) | argon2id, 로그인 레이트 리밋, httpOnly·SameSite=Lax 쿠키, 세션 폐기, 비밀번호 정책(12자 이상), gateway 127.0.0.1 바인드, CORS 미허용(같은 출처만), `/setup`은 계정 생성 후 404 | 통합 테스트 |
| S11 | API 키 범위 이탈 | API 키는 묶인 에이전트의 세션·파일·런에만 접근(다른 에이전트 자원은 404), 모든 리포지토리 workspace 범위 강제 | 통합 테스트(다른 에이전트 세션 404) |
| S12 | 업로드 파일 | 크기 한도, zip 검증(경로 탈출·심링크·폭탄: 압축 해제 상한 500MB), 파일명 정규화, 다운로드 시 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`, SVG 등은 `application/octet-stream`으로 | 단위 테스트 |
| S13 | 자원 고갈(DoS) | 샌드박스·런 동시 수 한도, 계정당 한도, 토큰·시간 예산, 도구 출력 한도, 홈 디스크 한도, 요청 본문 한도, 이벤트 스트림 MAXLEN | M3·M6 테스트 |
| S14 | 의존성·이미지 | `uv lock`·`pnpm-lock` 고정, CI에서 `pip-audit`·`pnpm audit`, 샌드박스 이미지에 `trivy` 스캔(경고) | CI |
| S15 | 사후 추적 | 런·도구 호출 전수 기록(`run_event`), egress access.log 보존, 샌드박스 생성·종료 기록(`sandbox_instance`). 별도 감사 로그 테이블은 두지 않는다 | A10 |
| S16 | XSS(마크다운 렌더링) | react-markdown 기본(raw HTML 비허용), 링크 `rel=noopener`, 도구 출력은 텍스트 노드로만 | 수동 |

## 12. 리스크와 미결 사항

### 12.1 사용자 결정이 필요한 것

| # | 질문 | 기본 가정(결정 없으면 이대로 진행) |
|---|---|---|
| Q1 | 샌드박스 기본 자원(1 vCPU/2GB/유휴 30분/동시 3)과 PC 사양 | 위 기본값. PC 코어·메모리와 WSL2 한도에 맞춰 `SANDBOX_MAX_CONCURRENT` 조정 |
| Q2 | egress 허용 목록 초기 항목과 추가 도메인, 변경 절차(파일 편집 vs 콘솔 UI) | pypi·npm·github 기본 + 파일 편집. UI 편집은 미구현 |
| Q3 | 샌드박스 이미지에 넣을 추가 시스템 패키지·언어(Java, Go, LibreOffice, 한글 폰트 등) | 3.3절 목록만. 요청 시 이미지 재빌드 |
| Q4 | 세션 스냅샷 자동 보존(유휴 종료 시 홈 보존, 7일) 여부와 보존 기간 | 켬, 7일 |
| Q5 | 모델 가격표 입력 여부(비용 표시) | 선택 입력. 미입력 시 토큰만 표시 |
| Q6 | 기본 승인 모드(`approval_mode`) | `off` |
| Q7 | 사용자 첨부 이미지(비전) 지원 범위 | 사용자 첨부 이미지만 모델에 전달. 에이전트가 만든 이미지 파일은 다운로드만 |
| Q8 | 로그 보존 기간(런 이벤트 90일) | 90일 |
| Q9 | Dify의 "Build mode"(채팅으로 에이전트 구성)를 1단계에 넣을지 | 넣지 않음. 폼 기반 빌더만 |

해결됨(ADR-0002): 웹앱 접근은 로그인한 본인만, 초대 메일 없음, 저장소는 비공개이며 코드만 보존하고 `LICENSE`(Apache 2.0)는 M1에 포함.

### 12.2 기본값으로 가정한 것 (사용자 확인 불필요, 변경 시 ADR 갱신)

- 스택: 9장 전부. Next.js 대신 Vite SPA, Celery 대신 자체 러너, WebSocket 대신 SSE.
- 과업 API는 1단계에서 외부 호출 가능한 서비스 API로 노출(2단계 전에 수동 검증 가능하도록).
- 콘솔·웹앱이 같은 SPA, 같은 도메인.
- 프로바이더 서버측 압축·컨텍스트 편집 기능은 사용하지 않음(프로바이더 중립).
- 라이브러리 스킬은 발행 시 버전을 고정(`skill_version_ids`), 자동 최신 추적 없음.
- 단일 계정, 역할 없음, 감사 로그 없음, 엔드유저 테이블 없음(ADR-0002). 다인 사용으로 전환하면 ADR을 갱신하고 마이그레이션으로 추가한다.

### 12.3 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| Anthropic 최신 모델의 preserved thinking 검사(히스토리 편집 시 400) | 압축·초안 수정 후 요청 실패 | 4.7·4.8의 append-only 설계, `prompt_hash` 변경 시 OpaqueBlock 제거, 400 발생 시 thinking 블록 제거 후 1회 재시도 폴백을 어댑터에 구현 |
| OpenAI 호환 서버의 구현 편차(usage 미제공, strict 미지원, 병렬 도구 미지원) | 토큰 집계 오차, 도구 호출 실패 | 프로바이더 설정 플래그 + `estimated` 표시 + 연결 테스트에서 능력 탐지 |
| Docker Desktop(Windows)과 Linux(CI)의 차이로 샌드박스 테스트가 환경별로 다르게 실패 | 개발 지연 | 기본 환경인 Windows Docker Desktop 통과가 필수, CI(Linux)는 보조. 차이가 나면 3.9절 표에 기록 |
| 홈 볼륨 쿼터를 강제하지 못함 | 디스크 고갈 | sandboxd `du` 감시 + 하드 한도 종료 + VM 디스크 모니터링 문서 |
| 단일 PC·단일 runner 프로세스 | 재시작·PC 종료 시 진행 중 런 실패 | SIGTERM 시 신규 수신 중지와 진행 중 런 최대 60초 대기 + 재기동 시 2.5절 하트비트 복구. HA는 범위 밖 |
| LLM 비용 폭주(루프 반복) | 비용 | 스텝·토큰·시간 예산, 비용 상한 옵션, 사용량 화면 |
| 스냅샷 크기 증가(대용량 패키지 설치) | 발행 실패·느린 복원 | 1GB 한도와 큰 디렉터리 안내, 이미지 사전 설치 패키지 확대 |
| 프롬프트 캐시 미적중으로 비용 상승 | 비용 | 시스템 프롬프트·도구 고정, 동적 정보는 user 턴에, `usage.cache_read_tokens` 집계로 모니터링 |

## 13. 2단계 연결 지점

### 13.1 워크플로우 엔진이 쓸 내부 인터페이스

```python
# app/agent/task_service.py  (2단계 엔진은 이 모듈만 import한다. app.api 의존 금지 — import-linter로 강제)
class AgentTaskSpec(BaseModel):
    workspace_id: UUID
    agent_id: UUID
    agent_version_id: UUID | None = None        # None이면 발행 버전
    inputs: dict[str, Any]                      # 변수명 → 값 또는 {"file_id": ...}
    instruction: str                            # {{var}} 템플릿
    output_schema: dict | None = None           # JSON Schema(object). 없으면 text만
    files: list[UUID] = []                      # session_file ids
    limits: RunLimits | None = None
    session_mode: Literal["ephemeral", "reuse"] = "ephemeral"
    session_id: UUID | None = None              # reuse일 때
    external_user: str | None = None            # 선택 식별 문자열(session.external_user)
    correlation: dict = {}                      # workflow_run_id, node_id 등 (run.input.correlation에 저장)

class AgentTaskResult(BaseModel):
    run_id: UUID; session_id: UUID
    status: Literal["succeeded", "failed", "cancelled", "waiting_input"]
    result: dict | None                          # output_schema 검증 통과한 JSON
    text: str                                    # 마지막 어시스턴트 텍스트
    files: list[SessionFileRef]
    usage: Usage
    error: RunError | None
    pending_interaction: Interaction | None      # waiting_input이면 (2단계 HITL 연결)

class AgentTaskService(Protocol):
    async def start(self, spec: AgentTaskSpec) -> UUID                       # run_id. 큐에 넣고 즉시 반환
    def events(self, run_id: UUID, after: str | None = None) -> AsyncIterator[RunEvent]
    async def wait(self, run_id: UUID, timeout: float | None = None) -> AgentTaskResult
    async def provide_input(self, run_id: UUID, payload: InteractionInput) -> None
    async def cancel(self, run_id: UUID) -> None
```

서비스 API `POST /api/v1/agent-tasks`는 이 서비스의 얇은 HTTP 래퍼다. 2단계 엔진은 같은 프로세스(runner) 안에서 `AgentTaskService`를 직접 호출하거나, 엔진이 별도 서비스가 되면 내부 HTTP를 쓴다. 둘 다 동작하도록 서비스는 HTTP·FastAPI에 의존하지 않는다.

### 13.2 1단계에서 지켜야 할 제약

| 제약 | 이유 |
|---|---|
| `AgentLoop`·`AgentTaskService`·`app.llm`·`app.sandbox`는 `app.api`, `app.auth`를 import하지 않는다(import-linter 규칙) | 엔진 프로세스에서 재사용 |
| 런 이벤트 스키마(4.5)는 `packages/event-schema`에서 버전 관리하고, 필드 삭제·의미 변경은 새 `schema_version`으로만 | 2단계 캔버스가 노드 실행 뷰에 같은 이벤트를 쓴다 |
| 런은 세션과 독립적으로 주소 지정 가능(`run_id`로 이벤트·상태·결과 조회) | 노드 실행 = 런 1개 |
| `session.source=task`를 1단계 스키마에 포함 | 워크플로우 실행 로그와 채팅 로그를 같은 화면에서 구분 |
| `run.input.correlation`(jsonb)을 비워 두되 컬럼은 둔다 | 워크플로우 런·노드 id 연결 |
| 과업 결과의 `result`는 `output_schema`로 검증된 JSON, `files`는 `session_file` id 목록 | 엔진 변수 풀(타입 변수)에 바로 매핑. 파일은 id로 전달 |
| `waiting_input`은 런 상태이며 재개는 `provide_input`으로만 | 2단계 HITL(워크플로우 일시정지·폼·재개)이 같은 경로를 쓴다 |
| 스냅샷·스킬·파일 동기화는 `SandboxProvisioner` 한 곳 | 엔진이 노드마다 샌드박스를 요청할 때 같은 초기화 |
| 모델 어댑터는 에이전트 외의 호출자(2단계 LLM 노드)도 쓸 수 있게 `ChatRequest` 단위로 독립 | LLM 노드가 `app.llm`을 그대로 사용 |
| 모든 테이블의 `workspace_id`와 리포지토리 범위 강제 | 2·3단계 테이블도 같은 규칙 |

### 13.3 2단계에서 추가될 것(1단계에서 만들지 않음)

워크플로우 정의·실행 테이블, 캔버스, 노드 레지스트리, 변수 풀, `agent` 노드가 `AgentTaskService`를 호출하는 어댑터, 워크플로우 HITL 폼, 에이전트 노드 실행 뷰(1단계 `ToolCallCard`·`RunInspector` 재사용), 과업 세션 보존 정책(ephemeral 기본), 노드 간 파일 전달(`session_file` id 기반).
