# Dify 분석 보고서

기준일: 2026-10-12. 공식 저장소, 릴리스 노트, 문서, 블로그, 로드맵, 요금 페이지를 직접 확인해 정리했다. 출처는 10장에 모아 두었다.

## 1. 한 줄 요약

Dify는 LangGenius가 개발하는 오픈소스 LLM 앱·에이전트 워크플로우 플랫폼이다. 비주얼 워크플로우 엔진을 중심에 두고, RAG 지식 파이프라인, 모델·도구 플러그인 생태계, 샌드박스 에이전트, 퍼블리싱 API를 하나의 워크스페이스에 묶었다. 2026년에는 "에이전트와 워크플로우를 팀 동료처럼 쓴다"는 방향으로 에이전트를 재설계하고 있다. 수익은 Cloud 구독, Enterprise 라이선스, 마켓플레이스에서 나온다.

## 2. 현황

| 항목 | 내용 |
|---|---|
| 개발사 | LangGenius. 2026-03 $30M Pre-A 투자 유치 |
| GitHub | 158k stars, 25k forks (2026-10-12) |
| 최신 커뮤니티 버전 | 1.17.1 (2026-09-10). 월 1회 마이너 릴리스 + 패치 |
| Enterprise 버전 | 3.13.0 LTS (2026-09-29). 6개월 LTS 주기, 별도 버전 체계 |
| 컴플라이언스 | SOC 2, ISO 27001, GDPR 2년 연속 (2026-03) |
| 마켓플레이스 | 플러그인 928개, 공식 라벨 263개 (2026-07-31 스냅샷) |
| 요금 (Cloud) | Sandbox 무료: 앱 5, 멤버 1, 50MB. Professional $590/년/워크스페이스: 앱 50, 멤버 3, 5GB. Team $1,590/년: 앱 200, 멤버 50, 20GB |
| Enterprise | Kubernetes 배포, 멀티 워크스페이스, SSO, 세션별 전용 샌드박스 컨테이너, 별도 견적 |
| 라이선스 | Apache 2.0 기반 "Dify Open Source License". 추가 조건 2개 |

### 라이선스 상세와 시사점

- 추가 조건 1: 소스를 이용해 멀티테넌트 환경을 운영하려면 LangGenius의 서면 허가가 필요하다. 테넌트 1개 = 워크스페이스 1개로 정의한다.
- 추가 조건 2: 프론트엔드를 쓸 경우 콘솔과 앱의 로고·저작권 표시를 삭제·변경할 수 없다. 프론트엔드를 쓰지 않으면 적용되지 않는다.
- 라이선스 문서는 제품의 인터랙션 디자인이 디자인 특허로 보호된다고 명시한다.
- 시사점: Dify 본체를 포크해 SaaS를 만들거나 UI를 그대로 베끼는 길은 막혀 있다. 반면 주변 프로젝트인 graphon(실행 엔진), dify-plugin-daemon, dify-sandbox는 추가 조건 없는 순수 Apache 2.0이라 재사용이 가능하다.

## 3. 제품 표면: 사용자가 하는 일

### 3.1 앱 유형

| 유형 | 설명 |
|---|---|
| Workflow | 단발성 작업. 모든 앱 유형의 기반 |
| Chatflow | 대화 턴마다 실행되는 워크플로우. 대화 변수와 메모리를 가짐 |
| Chatbot, Agent, Text Generator | 레거시 기본 앱. 같은 엔진 위에서 단순 UI로 동작 |
| New Agent (1.16+, beta) | 리눅스 샌드박스에서 명령 실행·패키지 설치·파일 관리가 가능한 에이전트. 단독 앱 또는 워크플로우 노드로 사용 |
| Knowledge Pipeline | 지식 베이스 적재 과정을 워크플로우로 편집 |

### 3.2 워크플로우와 노드

시작 노드는 User Input(수동 실행) 또는 Trigger(스케줄, 웹훅, 플러그인 트리거)다.

| 분류 | 노드 |
|---|---|
| 제어 흐름 | start, end, answer, if_else, iteration, loop |
| LLM | llm, question_classifier, parameter_extractor |
| 데이터 처리 | code, template_transform, variable_aggregator, variable_assigner, list_operator, document_extractor |
| 통합 | http_request, tool (플러그인 도구, OpenAPI 도구, MCP 서버, 워크플로우-as-tool) |
| 사람 개입 | human_input |
| 지식 | knowledge_retrieval, knowledge_index, datasource |
| 에이전트 | agent (레거시), agent_v2 (New Agent 노드) |
| 트리거 | trigger_schedule, trigger_webhook, trigger_plugin |

앞의 18종은 범용 엔진 graphon의 내장 노드이고, 지식·에이전트·트리거 노드는 Dify 본체에 있다.

### 3.3 변수 모델

- 입력 변수: User Input 노드에서 정의. 실행 시작 시 고정.
- 노드 출력: 각 노드가 하나 이상의 출력을 내고 이후 노드가 참조. 불변.
- 환경 변수: API 키 등 비밀. 여러 LLM 노드가 공유하는 모델 설정도 저장 가능 (1.17.0).
- 대화 변수: Chatflow 전용. 대화 내에서 지속되며 variable_assigner로 갱신.
- 시스템 변수: sys.* 접두. 앱 유형별로 다름.
- 타입: 문자열, 숫자, 객체, 배열, 파일 등. Code 노드 출력에는 길이·중첩 깊이 제한이 있다.

### 3.4 지식 (RAG)

2025년 "RAG 2.0: pipeline orchestration" 이후 지식 적재는 워크플로우 캔버스에서 편집한다.

| 단계 | 선택지 |
|---|---|
| Data Source | File Upload, Online Document(Notion 등), Web Crawler(Jina Reader, Firecrawl), Online Drive. 데이터소스 플러그인으로 확장 |
| Doc Processor | Dify Extractor(이미지 추출 포함), Doc Extractor 노드, Unstructured 등 마켓플레이스 도구 |
| Chunker | General, Parent-child(작은 자식 청크로 매칭, 큰 부모 청크로 컨텍스트), Q&A Processor |
| Knowledge Base Node | 인덱스 방식 High Quality(임베딩) 또는 Economical(키워드 10개). 검색은 벡터, 전문, 하이브리드 + 리랭크 |

2026년 추가: 멀티모달 검색(1월), 청크별 요약 인덱스(1.12, 2월), MongoDB Atlas·Voyage AI 네이티브 지원(6월). 로드맵 "New RAG"는 에이전트 기반으로 지식 플랫폼을 재구축하는 과제다.

### 3.5 도구, 플러그인, MCP

- 플러그인 6종: Tool, Model, Agent Strategy, Extension(인바운드 HTTP 엔드포인트), Datasource, Trigger. Bundle은 여러 플러그인을 묶는 패키징 계층.
- 패키지 형식 .difypkg, 번들 .difybndl. 마켓플레이스 게재는 langgenius/dify-plugins 저장소에 PR.
- 모델 프로바이더 자체가 플러그인이다. 1.0(2025-02) 이후 모델 런타임이 코어 밖으로 나갔다.
- MCP: 외부 MCP 서버를 도구로 사용(소비)하고, 퍼블리시한 앱을 MCP 서버로 노출(제공)한다.
- 마켓플레이스 거버넌스(2026-09): 자동 스캔 + 사람 리뷰, 보안 등급 S/A/B/C/Unscanned, 버전마다 재스캔, 데이터 흐름 3유형 공개. 암호학적 서명은 아직 없다.
- Creator Center와 템플릿 마켓플레이스(2026-03, 05): 워크플로우 템플릿 공유와 제휴 수수료.

### 3.6 New Agent (1.16.0, 2026-07 beta)

- 에이전트를 "능력(Capability)"으로 한 번 정의하고, 실행마다 "과업(Task)"만 준다. 단독 채팅 앱, 서비스 API, 여러 워크플로우의 Agent 노드에서 같은 에이전트를 재사용한다.
- 샌드박스: 기본은 번들 컨테이너(local_sandbox), 설정으로 E2B 클라우드 VM 전환. 퍼블리시 시 홈 디렉터리 스냅샷을 저장하고 이후 실행은 그 상태에서 시작한다.
- Skills: 지침, 참고 자료, 스크립트를 묶은 단위. SKILL.md를 포함한 .zip 또는 .skill 패키지. 워크스페이스 공유 라이브러리 스킬(에이전트당 20개)과 에이전트 전용 임베디드 스킬.
- 확장 경로 3가지: Tools(중앙 관리 외부 기능), Skills(팀 방법론), 샌드박스 CLI 도구(일회성 작업).
- Build mode: 채팅으로 에이전트를 구성하고 Build Draft에 변경을 쌓은 뒤 Preview, Publish.
- 격리 수준: 커뮤니티 에디션은 파일 접근 제어로 위험을 줄이는 수준이며 "상호 불신 사용자 간 보안 경계가 아니다". Enterprise는 세션마다 전용 샌드박스 컨테이너를 쓴다.

### 3.7 Human-in-the-loop

- Human Input 노드(2026-03). 드롭다운, 파일 업로드가 있는 폼(1.15). Loop·Iteration 내부에서도 사용(1.17.0).
- 실행이 멈추면 SSE로 workflow_paused, human_input_required 이벤트가 오고, form_token으로 폼 API를 호출해 재개한다.
- 로드맵: Teams, Slack, DingTalk, Lark 안에서 승인 처리.

### 3.8 퍼블리싱, API, CLI

- 채널: 웹앱, 임베드, 서비스 API, MCP 서버.
- 서비스 API 주요 그룹: chat-messages, completion-messages, workflows/run, 실행 상태·로그, 파일 업로드·미리보기, 음성 변환, 대화·메시지·피드백·추천 질문, 어노테이션, 휴먼 인풋 폼, 지식 베이스 CRUD·검색·파이프라인 실행. 인증은 Bearer API 키.
- 지식 API 키를 특정 지식 베이스로 한정 가능(1.17.1).
- difyctl(2026-07): 터미널과 코딩 에이전트에서 Dify 앱을 호출하는 공식 CLI. DSL export는 기본적으로 draft를 내보낸다.

### 3.9 버전, 협업, LLMOps

- 버전: Current Draft, Latest Version, Previous Versions. 유료 플랜에서 복원과 API 버전 고정 가능. 다중 환경 배포는 로드맵.
- 실시간 협업(1.14.0, 2026-04): WebSocket 서비스로 여러 멤버가 한 워크플로우를 동시 편집.
- 로그와 어노테이션: 실행 로그, 노드별 입출력, 어노테이션 응답.
- 트레이싱 연동: Langfuse, LangSmith, Opik, Arize Phoenix, MLflow, Weave, Aliyun, Tencent. OpenTelemetry 계측 내장.
- DSL: 앱 전체를 YAML로 내보내고 가져온다. 비밀 변수 포함 여부 선택. 지식 콘텐츠와 외부 키는 제외.

### 3.10 워크스페이스와 권한

- 워크스페이스 = 테넌트. 멤버 역할, RBAC(완료), 자격증명 권한, 리소스 접근 토큰(로드맵).
- 커뮤니티 에디션은 단일 워크스페이스. 멀티 워크스페이스와 SSO는 Enterprise.

## 4. 아키텍처

### 4.1 리포지토리 구성

| 저장소 | 역할 | 언어 | 라이선스 |
|---|---|---|---|
| dify | 본체 모노레포. api/, web/, docker/ | Python, TypeScript | Dify OSS License |
| graphon | 그래프 실행 엔진. 2026년 본체에서 추출. PyPI 0.8.0 (2026-09-09) | Python | Apache 2.0 |
| dify-plugin-daemon | 플러그인 생명주기 관리 | Go | Apache 2.0 |
| dify-sandbox | Code 노드용 코드 실행 샌드박스 | Go | Apache 2.0 |
| dify-plugin-sdks | 플러그인 개발 SDK | Python | Apache 2.0 |
| dify-official-plugins, dify-plugins | 공식 플러그인, 마켓플레이스 플러그인 | Python | - |
| dify-docs | 문서 | MDX | - |
| mosoo (alpha) | 코딩 에이전트(Codex, Claude Agent SDK 등)를 API 뒤에서 세션별 샌드박스로 돌리는 런타임. Cloudflare 기반 | TypeScript | Apache 2.0 |

### 4.2 런타임 서비스 (docker compose 기본 13개)

| 서비스 | 이미지 | 역할 |
|---|---|---|
| api | dify-api | 메인 API 서버 (콘솔, 서비스, 웹 API) |
| worker | dify-api | Celery 백그라운드 작업 (인덱싱, 메일, 정리 등) |
| worker_beat | dify-api | Celery 스케줄러 |
| web | dify-web | 프론트엔드 |
| redis | redis 6 | 캐시, 큐, 세션 |
| nginx | nginx | 리버스 프록시 |
| sandbox | dify-sandbox | Code, Template Transform 노드 실행. 네트워크·파일시스템·셸 차단 |
| ssrf_proxy | squid | 샌드박스 아웃바운드 제한 |
| plugin_daemon | dify-plugin-daemon | 플러그인 런타임 |
| agent_backend | dify-agent-backend | New Agent 실행 백엔드 |
| local_sandbox | dify-agent-local-sandbox | 에이전트 셸 샌드박스 |
| agent_ssrf_proxy | squid | 에이전트 샌드박스 egress 허용 목록 |
| init_permissions | busybox | 스토리지 권한 초기화 |

프로파일로 켜는 것: PostgreSQL 또는 MySQL, 벡터 스토어 17종(Weaviate, Qdrant, Milvus, pgvector, Elasticsearch, OpenSearch, Chroma 등), certbot, api_websocket(협업), unstructured.

### 4.3 백엔드 스택

- Python 3.13 고정. Flask 3 + flask-restx 포크 + fastopenapi. gunicorn + gevent. WebSocket은 gevent-websocket, python-socketio.
- PostgreSQL(psycopg2) + SQLAlchemy + flask-migrate. Celery + Redis.
- 스토리지 SDK 9종(S3, Azure Blob, GCS, OSS, COS 등). 벡터 DB 클라이언트 30종이 워크스페이스 패키지로 분리되어 있고 기본 설치에 모두 포함된다.
- OpenTelemetry 계측(Flask, Celery, httpx, Redis, SQLAlchemy). 트레이싱 백엔드 8종.
- 도구: uv, ruff, pyrefly 타입 체커, import-linter, pytest.
- 디렉터리: controllers(라우트), services(비즈니스 로직), models(ORM), core(엔진·RAG·플러그인·에이전트), tasks(Celery), repositories, events, schedule, migrations, enterprise.

### 4.4 프론트엔드 스택

- Next.js + React + TypeScript. Vite 경로(vite-plus, vinext)를 병행 도입 중. React Server Components 관련 의존성 존재.
- 캔버스: reactflow + elkjs 자동 배치. 상태: Zustand(+zundo 실행취소), Jotai. 데이터: TanStack Query, oRPC, ky, socket.io-client.
- 에디터: Lexical(프롬프트), Monaco(코드). UI: Tailwind, Base UI, 사내 패키지 @langgenius/dify-ui. i18n: i18next.
- 테스트: Vitest, Testing Library, Playwright, Storybook.
- 라우트 그룹: (commonLayout) 콘솔, (shareLayout) 퍼블리시 웹앱, (humanInputLayout)/form/[token] 휴먼 인풋 폼, signin/install/oauth-callback 등.

### 4.5 실행 엔진 graphon

Dify 워크플로우 엔진을 호스트 애플리케이션에 임베드할 수 있는 라이브러리로 떼어낸 것이다. 유사 제품을 만들 때 가장 참고할 가치가 큰 부분이다.

- 진입점: graphon.dsl.loads()로 YAML DSL을 읽어 VariablePool, RuntimeState, NodeFactory, Graph, Engine을 조립. 파이썬 코드로 직접 조립도 가능.
- 실행 모델: 큐 기반 이벤트 드리븐. Engine이 루트 Frame, command processor, EventStream, 고정 워커 풀, Dispatcher를 만든다. Scheduler가 StartTask를 Shared ready queue에 넣고, Worker가 노드 제너레이터를 실행해 결과를 Dispatch queue로 보낸다. Dispatcher와 NodeEventProcessor가 결과·명령·종료 판정을 한 번에 하나씩 처리하고 Scheduler가 다음 준비된 노드를 공급한다.
- Frame과 컨테이너: iteration, loop, 서브그래프를 Frame으로 다루고 ContainerHandler로 확장. 컨테이너 상태는 영속화된다.
- 상태: RuntimeState가 변수 풀과 실행 상태를 보유. Command API로 외부에서 일시정지, 중단, 변수 변경. 스냅샷으로 일시정지·재개(HITL의 기반).
- 스트리밍: Engine.run()이 그래프 생명주기·노드·트래버설 이벤트를 yield. 응답 필터로 소비자 맞춤 가공.
- 확장: Node 서브클래스가 NodeFactory에 타입·버전으로 등록. Layer API로 관찰·제한. 모델 연동은 protocols로 교체. Import Linter로 계층 의존 규칙을 CI에서 강제.
- 미비점: ARCHITECTURE.md가 재시도 정책 설정, 검증 규칙, 분기 합류 의미론을 명시하지 않는다. "still evolving".

### 4.6 플러그인 데몬

- Go. Dify API가 HTTP로 설치 상태 조회와 호출 요청을 보낸다.
- 런타임 3종: Local(같은 머신 서브프로세스, STDIN/STDOUT), Debug(TCP 전이중, 원격 디버깅), Serverless(AWS Lambda, HTTP).
- 플러그인은 Python SDK(3.11+)로 작성. 의존성은 uv. 본체와 프로세스가 분리되어 플러그인 장애가 API를 죽이지 않는다.

### 4.7 샌드박스 2종

| 구분 | Code 노드 샌드박스 | 에이전트 샌드박스 |
|---|---|---|
| 구현 | dify-sandbox, Go + libseccomp | local_sandbox 컨테이너 또는 E2B |
| 상태 | 무상태, 실행마다 격리 | 상태 유지, 홈 디렉터리 스냅샷 |
| 네트워크 | 차단 | squid 프록시 허용 목록 경유 |
| 언어 | Python, JavaScript. 사전 설치 패키지만 | 임의 CLI, 패키지 설치 가능 |
| 격리 강도 | 시스템콜·파일시스템 제한 | CE는 소프트 격리, EE는 세션별 전용 컨테이너 |

### 4.8 데이터 모델 (api/models)

account(계정·테넌트·멤버), model(앱·대화·메시지·엔드유저 세션), workflow(정의·실행·노드 실행), dataset(지식 베이스·문서·세그먼트·메타데이터), provider(모델 프로바이더·자격증명), tools(도구 프로바이더), trigger, human_input, agent + agent_sandbox_usage(미터링), skill, oauth, resource_access_token, credential_permission, web(퍼블리시 웹앱·엔드유저), api_based_extension, task, comment, snippet, source.

### 4.9 API 표면

| API | 대상 | 용도 |
|---|---|---|
| Console API | 스튜디오 UI | 앱 편집, 지식 관리, 워크스페이스 설정 |
| Service API | 외부 개발자 | Backend-as-a-Service. Bearer 키. SSE 스트리밍 |
| Web API | 퍼블리시 웹앱 | 엔드유저 세션 |
| MCP | 에이전트 클라이언트 | 앱을 MCP 서버로 노출 |

### 4.10 Enterprise 배포

Kubernetes 전제. PostgreSQL 14+, Redis 6+, 오브젝트 스토리지, 플러그인 컨테이너 레지스트리, 벡터 DB는 Qdrant 권장(운영 3노드). 운영 권장 사양은 워커 6노드 × 8코어/32GB로 약 3,000 DAU 기준이다.

## 5. 2026년 릴리스 궤적과 로드맵

| 버전 | 시점 | 핵심 |
|---|---|---|
| 1.12 | 2026-02 | 청크 요약 인덱스 |
| 1.13 | 2026-03 | Human Input 노드 (블로그 기준) |
| 1.14.0 | 2026-04-29 | 실시간 협업, HITL 서비스 API, dify-ui 패키지 분리, graphon으로 엔진 패키징 교체, Quota v3 |
| 1.14.1~2 | 2026-05 | 보안 강화(기본 키 제거, IDOR, 테넌트 검사), WebSocket 서비스 분리 |
| 1.15.0 | 2026-06-25 | difyctl CLI, 추론 과정 패널, HITL 폼 확장, 느린 모델 폴링, Excel 이미지 추출 |
| 1.16.0 | 2026-07-17 | New Agent beta, Agent App 기본 활성, MCP 2025-06-18 지원, OpenAI Responses 기본화 |
| 1.16.1 | 2026-07-28 | 에이전트 샌드박스 전용 squid 프록시, Jinja2 샌드박스, 다중 선택 입력 |
| 1.17.0 | 2026-08-25 | E2B 샌드박스, 홈 디렉터리 스냅샷, 워크스페이스 Skills 관리, Loop·Iteration 내 HITL, 공유 LLM 설정, Turnstile, Azure Key Vault |
| 1.17.1 | 2026-09-10 | 지식 API 키 범위 제한, Weaviate 12단계 수동 업그레이드 필요 |

로드맵 In Progress(2026-10): Skill Management(309표), New RAG, HITL IM 채널 연동, 서브그래프 캔버스 확장, Dify App Builder(자연어로 워크플로우 생성·진단), App 다중 환경 배포, 워크스페이스 Access Token. 완료 상위: RAG 2.0 파이프라인(2.2k표), RBAC(1.7k), MCP(1.2k), 역할 관리, HITL, 노드 간 파일 전달, Loop·Elif·문서 추출 노드, 커스텀 브랜딩, 비동기 실행, 버전 관리.

전략 메시지(2026-08 블로그): "에이전트는 어떻게 할지를 정하고, 워크플로우는 언제 할지를 정한다." 단독 에이전트 제품이 아니라 에이전트·기존 시스템·사람을 한 워크플로우에 엮는 것이 목표. 외부 에이전트 프레임워크 연결과 에이전트·사람 협업 강화를 탐색 중. 별도 프로젝트 mosoo는 Codex, Claude Agent SDK 등 코딩 에이전트를 API 뒤에 두는 런타임으로, 이 방향의 실험으로 보인다.

## 6. 강점, 약점, 기술 부채

### 강점
- 범위가 넓고 통합되어 있다. 워크플로우, RAG, 모델, 도구, 에이전트, 퍼블리싱, 로그가 한 제품에 있다.
- 워크플로우 엔진이 모든 앱 유형의 공통 기반이다. 지식 파이프라인까지 같은 캔버스에서 다룬다.
- 플러그인 생태계와 마켓플레이스, 프로세스 분리 런타임.
- 노드별 입출력 인스펙터, 버전 히스토리, DSL 이식성은 사용자 평가가 좋다.
- 기업 체크리스트를 꾸준히 채운다. RBAC, SSO, HITL, 컴플라이언스, 감사 가능한 에이전트 액션.
- 릴리스 속도가 빠르고 투자와 조직이 뒷받침된다.

### 약점과 기술 부채
- 백엔드가 Flask에 타입 안전성이 약하다. FastAPI 전환 로드맵 이슈(#30269)가 2025-12부터 열려 있고 진행이 느리다. 그래프 엔진이 전역 DB 세션에 결합되어 세션 누수를 낳았고, 이를 graphon 분리와 리포지토리 패턴으로 풀고 있다.
- 업그레이드 비용이 크다. 매 릴리스마다 compose 파일 변경, 환경 변수 개명, 수동 데이터 마이그레이션, 1.17.1의 Weaviate 12단계 수동 업그레이드 같은 함정이 있다.
- 보안 패치가 잦다. SQL 인젝션, SSRF 우회, 오픈 리다이렉트, IDOR, 공개된 기본 키가 2026년에도 수정되었다. 테넌트 검사 누락이 패치로 메워지는 패턴은 멀티테넌시가 후반에 덧붙여진 구조를 시사한다.
- 커뮤니티 에디션의 에이전트 샌드박스는 공유 샌드박스였고 현재도 소프트 격리다.
- 기본 설치가 무겁다. 벡터 DB 클라이언트 30종과 트레이싱 백엔드 8종이 기본 포함되고, 기본 서비스가 13개다.
- UX 한계: 큰 캔버스의 가독성, "Node execution failed" 같은 모호한 에러, 제한적인 코드 노드, if-then 이상의 로직 표현력, 모바일 편집 불가.
- 라이선스 추가 조건이 SaaS·화이트라벨 수요를 Langflow(MIT), Flowise, Coze Studio(Apache 2.0)로 밀어낸다.
- CE와 EE의 두 갈래 로드맵. 버전 복원, 로그 아카이브, 멀티 워크스페이스, 강한 격리는 유료다. 다중 환경 배포와 CI/CD, 평가(eval)는 아직 없다.

## 7. 경쟁 구도

| 제품 | 라이선스 | 스택 | 포지셔닝 | Dify 대비 |
|---|---|---|---|---|
| n8n | Fair-code (Sustainable Use) | Node.js | 범용 자동화. 400+ 노드 | 통합 폭은 넓지만 RAG·LLMOps는 얕음. 호스팅 SaaS 금지 |
| Langflow | MIT | Python, LangGraph 기반 | 개발자용 비주얼 파이프라인 | 확장성·API 유연성 우위. 팀 기능·권한·버전 약함. DataStax 호스팅 종료(2026-04) |
| Flowise | Apache 2.0 | Node.js, LangChain | 빠른 챗봇 프로토타입 | 가볍고 빠르나 운영 기능 부족 |
| Coze Studio | Apache 2.0 | Go + React | ByteDance의 에이전트 플랫폼 | 플러그인 강점. 셀프호스팅 복잡, 커뮤니티 초기 |
| RAGFlow | Apache 2.0 | Python | 심층 문서 이해 RAG 엔진 | RAG 품질 집중. 엔드유저 UI 없음 |
| FastGPT | Apache 2.0 + 조건 | TypeScript | 지식 베이스 Q&A | 중국 메신저 연동. SaaS 제한 |
| LangGraph Platform | MIT(라이브러리) | Python/TS | 코드 우선 에이전트 오케스트레이션 | 비주얼 빌더 없음. 개발자 전용 |

참고로 OpenAI, Google, Microsoft도 2025년 이후 자체 에이전트 빌더를 내놓고 있어, 범용 비주얼 빌더 시장은 대형 벤더 압력이 크다. 오픈소스·셀프호스팅·모델 중립이 Dify류의 방어선이다.

## 8. 유사 제품 개발 시사점

### 8.1 Dify가 증명한 것 (따라갈 설계)
1. 워크플로우 엔진을 단일 기반으로 두고 챗플로우, 에이전트, 지식 파이프라인을 모두 그 위에 올린다.
2. 모델 추상화는 LLM, 임베딩, 리랭크, TTS/STT, 모더레이션까지 포함한다.
3. 노드별 입출력을 기록하고 바로 볼 수 있게 한다. 이것이 디버깅과 LLMOps의 기초다.
4. 앱 전체를 YAML DSL로 내보내고 들여온다. 이식성, 템플릿, 공유, GitOps의 전제다.
5. 퍼블리싱은 API(SSE 스트리밍), 웹앱, MCP 서버 세 채널이 기본이다.
6. HITL은 "실행 일시정지 + 스냅샷 + 폼 토큰 + 재개 이벤트"로 구현한다.
7. 코드 실행과 에이전트 실행은 별도 샌드박스로 분리하고 egress를 프록시로 통제한다.

### 8.2 Dify의 빈틈 (차별화 기회)
- 타입 안전한 백엔드와 처음부터 분리된 엔진. Dify는 지금 비용을 내며 되돌리고 있다.
- 테넌트 격리와 세션별 샌드박스 격리를 커뮤니티 버전부터 기본으로.
- 가벼운 설치. 서비스 4~5개, Postgres+pgvector 하나로 시작.
- 다중 환경(dev/staging/prod)과 DSL의 Git 연동, 배포 승격, 평가·회귀 테스트. Dify는 로드맵 단계다.
- 큰 캔버스 UX: 서브그래프 접기·펴기, 명확한 에러, 표현력 있는 로직 노드.
- 코드 우선 + 비주얼 양방향(DSL, 캔버스, SDK) 개발자 경험.
- 허용적 라이선스(Apache 2.0, MIT)로 SaaS·화이트라벨 수요 흡수.
- 한국 시장 특화: 한국어 모델·온프레미스 LLM(vLLM) 연동, 카카오워크·네이버웍스·슬랙 HITL 승인, 국내 규제 대응 감사 로그.

### 8.3 핵심 설계 결정 (설계 단계에서 확정할 것)

| # | 결정 | 권고 기본안 |
|---|---|---|
| 1 | 포지셔닝과 타깃 | 범용 클론 대신 하나의 쐐기를 정한다. 사내 플랫폼, 특정 산업, 또는 개발자 우선 |
| 2 | 배포 모델 | 셀프호스팅 우선 + 선택적 SaaS. 테넌트 모델은 첫날부터 |
| 3 | 실행 엔진 | 독립 라이브러리로 설계. 타입 변수 풀, 컨테이너(반복·루프·서브그래프), 이벤트 스트림, 스냅샷 기반 일시정지·재개, 명령 채널, 버전 있는 노드 레지스트리, DSL. graphon(Apache 2.0) 채택 여부를 초기에 평가 |
| 4 | 내구성 실행 | 자체 엔진 + Postgres 스냅샷으로 시작. 며칠 단위 HITL 대기가 핵심이면 Temporal 검토 |
| 5 | 모델 런타임 | 프로세스 내 어댑터 + OpenAI 호환 엔드포인트 우선. 인터페이스를 안정화해 나중에 플러그인화 |
| 6 | 도구 전략 | OpenAPI 임포트 + MCP 클라이언트 + 워크플로우-as-tool. 별도 플러그인 데몬은 마켓플레이스가 목표일 때만 |
| 7 | RAG | 적재를 워크플로우로. 청킹 3종, 하이브리드 검색 + 리랭크, 메타데이터 필터. 벡터 DB는 pgvector 하나로 시작 |
| 8 | 샌드박스 | 코드 노드는 무네트워크 무상태 샌드박스, 에이전트는 세션별 컨테이너(gVisor 등) 또는 E2B류. egress 프록시 |
| 9 | 멀티테넌시·권한 | 모든 테이블에 tenant_id, 리포지토리 계층에서 강제. RBAC, 범위 있는 API 키, 엔드유저 식별, SSO 확장점 |
| 10 | 관측성 | 실행·노드 실행 레코드, 토큰·비용 계정, OpenTelemetry 트레이스, 외부 트레이싱 export |
| 11 | 버전·환경 | draft/published/버전 고정 + 다중 환경 승격. DSL을 Git에 두는 GitOps 흐름 |
| 12 | 프론트엔드 | React + @xyflow/react, 실행취소, 타입 변수 선택기, 노드 인스펙터. Dify UI를 그대로 복제하지 않음(디자인 특허) |

### 8.4 추천 스택 (기본안, 팀 역량에 따라 조정)

| 계층 | 선택 | 이유 |
|---|---|---|
| 백엔드 | Python 3.12+, FastAPI, SQLAlchemy 2 async, Pydantic v2 | LLM 생태계가 가장 두텁고 graphon 재사용 가능. Dify가 가려는 목적지를 처음부터 |
| 워커 | Celery 또는 arq + Redis | 인덱싱, 트리거, 장기 실행 |
| DB | PostgreSQL 16 + pgvector | 메타데이터·벡터·전문 검색을 한 DB로 |
| 캐시·큐 | Redis | 세션, 큐, 스트리밍 버퍼 |
| 프론트 | TypeScript, Next.js 또는 Vite+React, @xyflow/react, Zustand, TanStack Query, Tailwind, Monaco | Dify와 동일 계열. 검증된 조합 |
| 샌드박스 | gVisor 기반 컨테이너 또는 E2B/Daytona류 | 세션별 격리 |
| 관측 | OpenTelemetry + Langfuse 호환 export | 표준 기반 |
| 패키징 | docker compose(개발·소규모) + Helm(운영) | 설치 부담 최소화 |

### 8.5 단계별 범위 (제안)

| 단계 | 범위 | 비고 |
|---|---|---|
| 1. 기반 | 워크스페이스·멤버·최소 RBAC, 모델 프로바이더 3~4종, 엔진 + 캔버스 + 노드 12종, 실행 로그·노드 인스펙터, 서비스 API(SSE) + 웹앱, DSL, draft/published | Dify의 Workflow 앱에 해당 |
| 2. 지식·도구·대화 | Chatflow + 대화 변수·메모리, 지식 파이프라인(pgvector, 청킹 3종, 하이브리드+리랭크), OpenAPI 도구 + MCP 클라이언트 + 워크플로우-as-tool, HITL 노드·폼·재개, 스케줄·웹훅 트리거 | Dify 1.0~1.13 수준 |
| 3. 에이전트·운영 | 샌드박스 에이전트 + 스킬, 다중 환경 배포, 비용·트레이싱, 실시간 협업, 플러그인 SDK, SSO·감사 로그 | Dify 1.14~1.17 + 로드맵 |

규모 감각: Dify는 2023년 시작, 기여자 290명 이상, 3년 반이 걸렸다. 3~5명 팀이 1단계를 약 3개월, 2단계를 추가 3개월에 끝내는 것이 현실적인 목표다. 이 수치는 추정이다.

### 8.6 법적·전략적 주의
- Dify 본체 코드를 포크해 멀티테넌트 서비스를 운영하거나 로고를 제거하는 것은 라이선스 위반이다. 참고만 하고 직접 구현하거나, 순수 Apache 2.0인 graphon, dify-sandbox, dify-plugin-daemon만 재사용한다.
- UI 인터랙션 디자인은 디자인 특허가 주장되므로 화면 구성을 1:1로 복제하지 않는다.
- Dify DSL 호환 임포트는 사용자 이전을 쉽게 하는 전략적 선택지다. 포맷 자체는 YAML 스키마라 호환 구현에 법적 장애는 없어 보이나, 확인이 필요하다.

## 9. 설계 착수 전 결정이 필요한 질문

1. 누가 쓰는가. 사내 개발자·운영팀, 외부 기업 고객, 일반 SaaS 사용자 중 무엇인가.
2. 배포 형태. 온프레미스 단일 테넌트, 멀티테넌트 SaaS, 둘 다 중 무엇을 먼저 하는가.
3. 차별화 쐐기. 한국 시장 특화, 특정 산업, 개발자 경험, 격리·보안, 가벼움 중 무엇인가.
4. 처음부터 만들 것인가, graphon·dify-sandbox를 재사용할 것인가.
5. 팀의 주력 언어. Python 백엔드 + TS 프론트인가, TypeScript 단일 스택인가.
6. 1단계 범위에서 반드시 들어가야 하는 노드와 모델 프로바이더는 무엇인가.
7. 에이전트(샌드박스)를 1단계에 넣을 것인가, 워크플로우·RAG를 먼저 안정화할 것인가.

## 10. 출처

공식
- https://github.com/langgenius/dify (README, api/, web/, docker/, api/core, api/models, api/openapi)
- https://github.com/langgenius/dify/releases
- https://raw.githubusercontent.com/langgenius/dify/main/LICENSE
- https://raw.githubusercontent.com/langgenius/dify/main/docker/docker-compose.yaml
- https://raw.githubusercontent.com/langgenius/dify/main/api/pyproject.toml
- https://raw.githubusercontent.com/langgenius/dify/main/web/package.json
- https://github.com/langgenius/graphon 및 ARCHITECTURE.md, https://pypi.org/project/graphon/
- https://github.com/langgenius/dify-plugin-daemon
- https://github.com/langgenius/dify-sandbox
- https://github.com/langgenius/mosoo
- https://github.com/orgs/langgenius/repositories
- https://github.com/langgenius/dify/issues/30269
- https://docs.dify.ai/en/learn/key-concepts
- https://docs.dify.ai/en/self-host/use-dify/build/new-agent/overview
- https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration
- https://docs.dify.ai/en/cloud/use-dify/build/version-control
- https://docs.dify.ai/en/cloud/use-dify/workspace/app-management
- https://docs.dify.ai/en/cloud/use-dify/nodes/code
- https://docs.dify.ai/en/develop-plugin/getting-started/choose-plugin-type
- https://docs.dify.ai/en/develop-plugin/features-and-specs/advanced-development/bundle
- https://enterprise-docs.dify.ai/ , https://ee.dify.ai/
- https://dify.ai/pricing
- https://roadmap.dify.ai/roadmap
- https://dify.ai/blog/introducing-new-dify-agent
- https://dify.ai/blog/why-we-redesigned-dify-agent
- https://dify.ai/blog/trust-is-a-feature-how-dify-is-governing-a-growing-plugin-ecosystem
- https://dify.ai/blog/dify-raises-30m-tomorrow-s-organizations-will-be-built-by-people-and-agents
- https://dify.ai/blog (2026년 게시물 목록)

제3자
- https://jimmysong.io/blog/open-source-ai-agent-workflow-comparison/
- https://dev.to/nova_gg/dify-review-2026-i-used-it-for-6-months-to-build-ai-agents-honest-verdict-2d25
- https://futureagi.com/blog/dify-vs-flowise-vs-langflow-2026/
- https://toolhalla.ai/blog/dify-vs-flowise-vs-langflow-2026
- https://www.sim.ai/library/dify-alternatives
- https://www.surfercloud.com/blog/n8n-vs-dify-vs-coze-a-detailed-comparison-of-automation-and-ai-platforms
- https://thegtmdirectory.com/compare/dify-vs-langflow
