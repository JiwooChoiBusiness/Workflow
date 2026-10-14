# 진행 기록

마일스톤마다 무엇을 만들었고 어떻게 확인했는지 적는다. 작업 단위가 끝날 때마다 갱신하고, 커밋과는 별개로 쓴다. 설계와 결정의 근거는 `design/`과 `decisions/`에 있고 여기는 결과만 남긴다. 테스트 수는 그 시점의 전체 pytest 통과 수다.

## 1단계. 에이전트 샌드박스 (2026-10-12 ~ 10-13)

| 마일스톤 | 한 일 | 확인 | 커밋 |
|---|---|---|---|
| 설계 (10-12) | Dify 분석(`research/dify-analysis.md`), ADR 0001~0004(범위, 개인용, Gemini 전용, Docker 없는 단일 프로세스), 1단계 설계 v3 | 설계 검토 2회 반영 | 87f07a2, 2a90661, 4db0965 |
| M1 스캐폴딩 | uv 워크스페이스, FastAPI 단일 프로세스(API + 러너 + 정적 서빙), SQLite WAL 엔진, Supervisor 큐, CLI(`serve`·`doctor`·`memcheck`), smoke·memcheck 스크립트, CI | pytest 43, 유휴 RSS 78MB | b4a00ec |
| M2 백엔드 기반 | 테이블 16개와 Alembic 0001, 계정·로그인(argon2id, httpOnly 쿠키, CSRF), 리포지토리의 workspace 범위 강제, LLM 상태 API | pytest 99 | b4a00ec |
| M3 로컬 샌드박스 | 세션별 작업공간 폴더, 경로 검증(정션·UNC·예약 이름 거부), PowerShell 실행, Job Object(메모리·프로세스 상한, 트리 종료), POSIX 폴백, `python -I` + `pip --target`, 허용 목록 HTTP(사설 IP 차단), 스냅샷 zip, 디스크 측정 | pytest 340, Windows 수동 8단계(`ops/sandbox-windows-check.md`), windows-latest CI | b4a00ec |
| M4 Gemini 어댑터 | OpenAI 호환 엔드포인트, 키 8개 순환과 (키, 모델) 쿨다운, 모델 폴백, 사고 서명 보존, 사용량 보정, 가짜 어댑터 | 라이브 포함 112개, 키 8개 유효 확인 | b4a00ec |
| 통합 | M1~M4 맞물림 정리 | pytest 615, RSS 100MB | b4a00ec |
| M5 에이전트 관리 | 에이전트·버전(초안/발행), 스킬 패키지(SKILL.md 파서), 파일, 환경변수(AES-GCM), API 키 API, 마이그레이션 0002(부분 유니크 slug) | pytest 773 | a885d98 |
| M6 실행 루프 | 도구 10종, 승인·질문으로 멈추고 `/input`으로 재개, 대화 압축, 세션·런 API, SSE 링버퍼(`after=seq` 재접속), 재기동 복구, 변경 파일 수집 | pytest 950, 실제 Gemini로 승인·중지·질문·재기동 확인, 런 중 RSS 133MB | a885d98 |
| M7 웹 콘솔 1·2차 | Vite + React 19, 로그인·초기 설정, 빌더(탭 구조), 채팅 패널(승인·질문 카드, 파일), 미리보기를 세션·런 API에 연결, Playwright 스모크 | vitest 59 → 112, 초기 JS 567KB | 3fbd147 |
| M8 발행·서비스 API | 발행 스냅샷과 롤백, 웹앱 API, 서비스 API(Bearer 키, 분당 60회), 과업 모드(JSON 스키마 결과) | pytest 1,024, `smoke-api.sh` 실제 Gemini 통과 | a885d98 |
| M9 로그·작업공간·사용량 | 로그 화면(세션·런, 상세의 스텝·도구 입출력·승인 결정·원본 이벤트), 작업공간 패널(프로세스·디스크·패키지 삭제·초기화), 사용량 집계와 가격표, 허용 목록 보기, 보존 정리 | pytest 1,025, 전체 검증 뒤 푸시 | 76a98b8, a885d98, 3fbd147 |
| M9 후속 정리 | 웹앱 화면 `/apps/<slug>`, 빌더 API 탭(curl 예시, 웹앱 공개 스위치), 재개 저장·변경 파일 기준선 버그 수정, `LLM_FAKE_DELAY_SEC`, 스킬 목록 버전 요약, 코드 강조 경량화(채팅 청크 331 → 235KB), 로케일 정리 | pytest 1,039, vitest 115, e2e 7, 실제 Gemini 웹앱 대화 | 미커밋 |
| M10 하드닝·운영 | `platform backup`(온라인 백업, 복구는 폴더 지정), watchfiles 자체 리로더, `logging.py` → `logsetup.py`, 하우스키핑 4개(WAL 체크포인트 등), memcheck 런 시나리오와 CI audit, 웹 build 바꿔치기와 vitest 순차 실행, e2e 14, 운영 문서 4종(`ops/`), 보안 체크리스트 S1~S16 결과 기록 | pytest 1,114, e2e 14, memcheck 최대 116MB, 설치 문서만으로 설치 3분, 수동 A1·A11·A12·A15 통과 | 미커밋 |
| 그 밖 | VS Code ▶로 서버 기동(`.vscode/`), 사용 설명서 `manual.md`, 본문 글꼴 Pretendard(자체 번들), 빌더 채팅 패널 스크롤이 페이지로 새던 버그 수정(셸 격자 행 고정 + 채팅 목록을 absolute 요소의 기준으로) | 설명서의 화면 문구 300개 대조, 스크롤은 e2e `chat-scroll.spec.ts`로 회귀 확인 | 미커밋 |

1단계 완료 기준은 M10까지이며 2026-10-13에 끝났다. 사용자가 직접 확인할 것으로 남긴 것: Git Bash에서 Ctrl+C 종료, 푸시 뒤 CI와 clone 명령.

## 2단계. 워크플로우 엔진·캔버스 (2026-10-14 ~)

| 마일스톤 | 한 일 | 확인 | 커밋 |
|---|---|---|---|
| 설계 (10-14) | 2단계 설계 v2(`design/phase2-workflow.md`), ADR-0005. 검토 지적 10건 반영, Dify 문서로 end·answer·분류기 동작 확인 | 설계 검토 1회 | 미커밋 |
| M11 엔진 코어 | `app/workflow` 패키지: 그래프 모델·검증, 변수 풀, 템플릿 렌더러, 병렬 가지 스케줄러, 제어 노드 7종(start, end, answer, template, if_else, 변수 집계·할당), 가짜 포트 테스트, 계층 규칙 | pytest 1,313(+199), 100노드 검증 2~22ms | 미커밋 |
| M12 저장·실행 서비스·콘솔 API | 마이그레이션 0003(워크플로우 테이블 6개), 워크플로우 레인(두 번째 Supervisor)과 종료 코디네이터, 실행기(노드 런 기록·체크포인트·재개·자식 런 정리), 기동 복구(running → failed(shutdown), 고아 자식 cancelled(orphaned)), 자식 응답·만료·중지 때 부모 재투입, 이벤트 스키마 v2(종료 이벤트 집합, session_id nullable), 콘솔 API(워크플로우·초안·검증·노드 타입·미리보기 런·발행·파일·런·노드 런·SSE·중지·입력), 보존 정리 | pytest 1,365(+51), vitest 117, e2e 14, 8501 수동(가짜 agent 노드로 대기·재개, 종료 로그 순서, 강제 종료 뒤 고아 취소, 만료 뒤 failed(input_timeout)) | 미커밋 |
| 오케스트레이션 설계 (10-14) | 지휘자 에이전트가 다른 에이전트를 `call_agent` 도구로 부르는 모드(`design/agent-orchestration.md` v2, ADR-0006). 결과는 글·파일·JSON 중 선택. 1단계 승인 대기·재개 기계 재사용, 자식 승인을 부모 카드로, 트리 전체 호출 상한 | 설계 검토 1회(지적 7건 반영) | 미커밋 |
| O1 오케스트레이션 백엔드 | 마이그레이션 0004(`run.parent_run_id`, `agent_version.sub_agents`), `call_agent` 도구와 하위 에이전트 프롬프트 절, 자식 런 서비스(버전 해석, 깊이·순환·트리 호출 상한, 파일 스테이징, 결과 파일 복사), 실행기의 대기 분기와 두 단계 종료 훅(`claim_parent` → `launch_parent`), 하위 트리 중지와 고아 취소, 자동 승인 양방향 전파, 부모 사슬 범위, 기동 복구·만료·reconcile, 빌더 `sub_agents` 검증과 경고, 로그 `parent_run_id` 필터와 `child_count`, 이벤트 `child_run.started`·`child_run.completed` | pytest 1,420(+55), vitest 118, e2e 14, 8501 수동(지휘자가 둘을 차례로 호출, 자식 승인의 위·아래 전파, 하위 트리 중지, JSON·파일 결과, 손자 한도, 서비스 API blocking, 멈추는 스텝의 중지, 자식 queued·running 중 재기동, 재기동 뒤 부모 세션 스트림, 만료), 실제 Gemini로 `output_schema` 인자 1건 | 미커밋 |
| O2 오케스트레이션 화면 | 하위 에이전트 카드(상태·스텝·경과, 자식만 중지, 카드 안 승인, 끝난 뒤 결과 파일과 JSON 뷰어)와 인스펙터(자식 세션을 채팅 컨트롤러 하나 더로 열어 대화 목록 재사용, 손자까지 재귀), 상태 표시줄의 하위 에이전트 표시, 새로 고침·재기동 복원(기록에서 그린 카드는 부모 런 이벤트를 다시 받아 자식을 찾는다), 빌더의 하위 에이전트 묶음과 호출 설정(결과 형식·스키마·파일 복사·승인, 경고 배지, 10개 상한), 한도 칸 `max_child_calls`, 발행 차단 사유와 경고 표시, 로그의 하위 런 탭·하위 포함 합계·부모 링크·하위 배지·부모 런 필터, 웹앱 같은 흐름, 재생 때 글이 도구 카드 뒤로 가던 순서 수정 | vitest 133(+15), e2e 14 + orchestration 1(각본 서버에서 따로), 8502 수동(빌더 경고, 카드 승인과 자동 승인 전파, 결과 파일·JSON, 새로 고침, 재기동 뒤 연결 경고 없음, 손자 카드, 카드 중지·부모 중지, 웹앱, 로그 트리), 청크 합 +36KB(gzip +9KB), 새 라우트 없음 | 미커밋 |
| M13 모델·HTTP·코드 노드 | llm(델타를 `node.text_delta`로, 호출마다 `node.usage`, json_mode는 시스템 지시 + JSON 추출 + 1회 재질의, vision), http_request(1단계 HttpClient 허용 목록, 1MB 넘거나 바이너리인 응답은 런 폴더 `wf-workspaces/<run>/downloads`에서 `wf_file`로 옮겨 `files`), code(런 폴더에서 `python -I` 래퍼, 시간·256MB·프로세스 8·반환 256KB·print 1MB 상한마다 오류 코드), question_classifier(1회 재질의 뒤 첫 클래스 폴백과 `classifier_fallback` 경고), parameter_extractor(강제 도구 호출 → prompt 모드), 엔진 포트에 파일 포트와 노드 설정(가격표, `WF_CODE_TIMEOUT_SEC`), 사용량에 `wf_run.usage`와 `group_by=workflow`, chat_basic 템플릿, memcheck 워크플로우 구간 | pytest 1,478(+58), windows 15(+5), 실제 Gemini로 llm·분류·추출 한 런, memcheck 최대 128.9MB(워크플로우 런 중 128.2), 8501 수동(허용 목록 밖 거부, 3.7MB 응답이 런 폴더·`wf_file`로, 5MB 넘는 응답 거부, code 시간·메모리·출력·비 dict, 실행 중 강제 하우스키핑에도 런 폴더 유지, 가짜 모델로 분류 폴백) | 미커밋 |
| M14 iteration·agent 노드와 단일 노드 실행 | iteration(원소마다 하위 그래프 프레임, 순차·병렬 최대 4에서 결과는 index 순, error_mode terminate·continue·remove, 1,000개 상한, 반복 안 입력 대기는 `@index` 체크포인트로 멈춘 반복부터 재개), agent(자식 과업 런을 `ChildRunService.start_node_child`로 만들어 기다림, 렌더한 지시·입력·파일 변수, 결과 형식 text·json, 10초마다 `node.progress`, 승인 대기에서 같은 런 재개, 자식이 먼저 끝났거나 다시 멈춘 경우, 병렬 대기 순차 처리, 런이 멈추면 자식 취소, 초안 런은 미발행 에이전트의 초안), 단일 노드 실행 API(`POST .../draft/nodes/{node_id}/runs`), 과업 서비스가 FileRef(wf_file·session_file)를 id로 다시 찾아 task/files/로, 기동 복구가 고아 자식의 손자까지 닫음, 이벤트 v2에 자식 세션 id | pytest 1,544(+64), windows 15, 8501 수동(배열 10개 순차·병렬 2의 순서, error_mode 세 가지, all_shell 승인 대기 → `/wf-runs/{id}/input` 뒤 같은 런 완료, 자식이 먼저 끝난 뒤 재투입, 병렬 agent 둘이 두 번 멈춤, 거절, 대기 중 stop이 자식도 취소, 단일 실행 agent·template·if_else, 재기동 뒤 대기 유지와 재개) | 미커밋 |
| M15 캔버스 편집기 | 워크플로우 목록(검색·종류 필터·만들기·지우기)과 `/workflows/:id` 편집기(`@xyflow/react` 12, 편집기 묶음에서만 로드, 레일 접힘, `?node=` 공유), 팔레트(끌어 놓기, 누르면 고른 노드 뒤에 이어 붙임, 반복 틀 안에 놓기), 노드 카드(요약 한 줄·분기·실패 핸들·검증 표시), 타입별 속성 폼 14종(react-hook-form + zod, 서버 node-types 계약 테스트), 변수 선택기와 프롬프트 편집기(`{{` 팝업·변수 강조), 오류 처리·재시도, id 변경 때 참조 일괄 치환, 실행 취소 50, 자동 저장 1.5초·Ctrl+S·409 충돌 안내·이탈 전 저장, 환경·대화 변수, 한도·승인·마스킹·웹앱 설정, 발행·버전·롤백, API 탭(키 패널·호출 예시), 1단계 PublishPanel·ApiKeyPanel·ApiExamples·EnvVarEditor·ModelPicker props 일반화 | vitest 198(+65), e2e 14 + chat-scroll + workflows 1(orchestration은 각본 서버라 건너뜀), 8502 수동(전 노드 폼·연결·삭제·실행 취소·id 치환·새로 고침 유지·발행·롤백·충돌·이탈·반복 틀 크기, 100노드 그래프 170ms), 첫 화면 묶음 +6.0KB(gzip +1.9KB), 편집기 묶음 319KB(gzip 95KB). 워크플로우 API 키는 서버 경로가 없어(M17 키 대상 변경) 만들지 못함 | 미커밋 |
| M16 실행 패널·로그·웹앱(워크플로우) | 편집기 실행 탭(start 필드로 만든 입력 폼과 파일 업로드, 실행 전 초안 저장, 상태 한 줄, 결과는 글이면 마크다운·객체면 JSON·파일이면 이름과 크기, 노드 런 카드 타임라인(반복은 회차별로 접힘, 펼치면 전문 조회, 건너뜀·분기·재시도 표시), agent 노드 카드 안에서 자식 런 대화와 승인 카드(O2 인스펙터 재사용, 답하면 같은 런을 이어 받음)), 캔버스 카드의 실행 상태 점·소요(누르면 그 노드 런으로), 노드 하나 실행 대화상자(참조 변수마다 입력칸, 노드별로 마지막 값 기억), 이벤트 리듀서 스토어와 스트림 컨트롤러(콘솔·웹앱 공용), `/workflows/:id/logs`와 런 상세(노드 타임라인·자식 과업 런 인스펙터·입력·출력·원본 이벤트 표·승인·중지), `/logs`의 워크플로우 런 탭, 사용량 워크플로우별 축, 웹앱 `/flows/:slug`(입력 폼·진행·결과·파일 내려받기·승인·내 실행 기록)와 웹앱 API(`/api/web/flows`, `/api/web/wf-runs`), API 키 대상 추상화 준비(열 변경 없음) | pytest 1,627(이 단위 +8), vitest 234(+36), e2e 17 + 건너뜀 1(+1, orchestration은 각본 서버라 건너뜀), 8502 화면 확인(선형·분기·반복·실패·agent 승인과 자식 대화·llm 단일 실행·실행 기록·이벤트 표·사용량·웹앱 파일 올리기와 내려받기·웹앱 승인), 첫 화면 묶음 +2.4KB(gzip +0.7KB), 웹앱 화면 묶음 12KB(gzip 4KB). 콘솔에서 돈 런(초안·단일 노드)의 파일은 받을 경로가 없어 이름·크기만 보임 | 미커밋 |
| M18 DSL(백엔드) | 우리 YAML 내보내기·가져오기(`POST /workflows/{id}/export?version=draft·published·<id>&include_secrets`, `POST /workflows/import`에 JSON `{yaml}`이나 multipart, `?into=`면 그 초안만 덮어씀), DSL·노드 버전 변환기 틀(낮으면 변환, 높으면 거부), agent 노드의 `agent_slug`로 id 복원과 검증 오류 `agent_not_found`(지운 에이전트도 저장·실행·발행 검증에서 같은 오류), 가져온 code 노드의 `ui.imported_code`와 `imported_code_unreviewed`(초안 저장에서 플래그를 지우면 풀림), 승인 모드 off는 inherit로, YAML은 safe 로더에 병합 키·별칭 부풀리기·깊은 중첩 거부와 2MB 상한, Dify DSL 부분 가져오기(1차 13종 매핑, 나머지는 `unsupported` 노드로 두고 엣지 유지, id `n<id>`·변수 참조·모델 교체와 경고), 값 없이 들어온 환경 변수는 초안 저장에서 빈 채로 허용 | pytest 1,627(이 단위 +70), 8501 curl(한글 이름·이모지 아이콘 내보내기·가져오기에서 그래프 동일, include_secrets 경고 헤더와 평문, Dify 예제, 2MB·깨진 YAML 422) | 미커밋 |

예정 순서: M12 → O1(오케스트레이션 백엔드) ∥ M13(모델·HTTP·코드 노드) → O2(오케스트레이션 화면) ∥ M14(iteration·agent 노드) → M15(캔버스 편집기) → M16(실행 패널·로그) → M17(챗플로우) ∥ M18(DSL) → M19(2차 노드) ∥ M20(하드닝). 2단계 완료 기준은 M18과 M20까지다.

## 3단계. RAG

아직 시작하지 않았다.
