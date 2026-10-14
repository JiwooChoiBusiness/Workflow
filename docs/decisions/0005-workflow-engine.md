# ADR-0005: 워크플로우 엔진과 캔버스는 1단계 프로세스 안의 자체 구현

- 상태: 제안 v2 (architect, 2026-10-14, design-reviewer 검토 반영). 사용자 결정이 없으면 채택으로 간주하고 M11부터 진행한다.
- 날짜: 2026-10-14
- 배경: 1단계(에이전트 샌드박스, M1~M10)가 끝났고 2단계는 Dify의 Workflow·Chatflow를 기능적으로 따라가는 그래프 엔진과 캔버스다. ADR-0001 D3(기능 복제, 소스·디자인 비복제)·D4(Dify 패키지 비의존)·D5(단계 순서), ADR-0002(본인 1명), ADR-0003(Gemini), ADR-0004(단일 프로세스, SQLite, 300MB)는 유지한다. 상세는 `docs/design/phase2-workflow.md`.

## 결정

### D1. 엔진은 `app/workflow` 자체 구현, 같은 프로세스·같은 큐·같은 이벤트 버스
- graphon 등 Dify 패키지를 쓰지 않는다. asyncio 스케줄러(준비 집합 + 노드별 Task + 세마포어 4)로 노드를 돌리고, 분기·합류·skipped 의미론을 설계 3.4로 고정한다. `end`는 런을 멈추지 않고 실행된 `end`들의 출력을 합친다(Dify 문서의 Output 노드와 같다). 런은 ready 노드와 실행 중 Task가 없을 때 끝난다.
- 1단계 `Supervisor`를 두 번째 인스턴스(워크플로우 레인, 동시 1)로 하나 더 띄운다. `agent` 노드의 자식 런은 에이전트 레인에서 돌아 교착이 없다. 종료 훅은 Supervisor가 아니라 `app/runner/shutdown.py`의 코디네이터가 두 레인이 모두 끝난 뒤 돌린다.
- 이벤트는 1단계 EventBus·링버퍼·SSE를 그대로 쓰고 봉투 스키마를 v2로 올린다(`session_id` nullable, `conversation_id` 추가, `workflow.*`·`node.*`·`answer.*` 타입, 종료 이벤트를 단일 상수에서 집합 `TERMINAL_EVENT_TYPES`로). 영속 테이블만 `wf_run_event`다. 이벤트에 싣는 노드 입출력은 4KB 미리보기뿐이다.

### D2. 변수 참조 문법과 렌더러는 자체 정의
- 선택자 `"<node_id>.<name>"`, 템플릿 `{{ node_id.name }}`. 예약 범위 `sys`·`env`·`conv`. 노드 id는 사용자가 바꿀 수 있는 slug다.
- 모든 텍스트 필드는 Jinja2 `ImmutableSandboxedEnvironment`로 렌더한다. 기동 시 `lenient`(2단계 노드)·`strict`(1단계 과업 지시, 동작 동일) 두 벌을 캐시한다.

### D3. 입력 대기와 재개는 1단계 경로를 그대로 잇는다
- `agent` 노드의 승인·질문은 워크플로우 런을 `waiting_input`으로 끝내 슬롯을 반납하고, 체크포인트(`wf_run.state`)에서 재개한다. 체크포인트는 대기 전환 직전·실패 시에만 쓴다(노드마다 쓰지 않는다). 재개는 자식 런 `provide_input` → 워크플로우 런 재투입 순서다.
- 대기 만료는 자식 런 만료 처리에서 `wf_node_run.child_run_id`로 역조회해 워크플로우 런을 재투입한다. 재기동 시 `running` 런은 1단계와 같이 `failed(shutdown)`이고 그 런의 `queued`·`waiting_input` 자식 런은 고아로 취소한다. 체크포인트 자동 이어가기는 하지 않는다.
- `code`·`http_request`·`template`·`tool` 노드는 승인 대상이 아니다. 설계 시점에 사용자가 쓴 코드·설정이기 때문이다. 단, 가져온 DSL의 `code` 노드는 사용자가 검토하기 전까지 실행·발행을 막는다.

### D4. 데이터 모델은 `wf_` 접두의 새 테이블, 챗플로우 대화는 1단계 세션과 분리
- `workflow`, `wf_version`, `wf_run`, `wf_node_run`, `wf_run_event`, `wf_file`, `wf_conversation`, `wf_message`. 마이그레이션 0003~0005.
- 1단계 테이블 중 바꾸는 것은 `api_key`뿐이다(`agent_id` nullable + `workflow_id`, 정확히 하나). `/api/v1/chat-messages`는 키 대상에 따라 에이전트·챗플로우로 분기한다.
- `code` 노드 작업 폴더는 `LocalWorkspaceBackend` 두 번째 인스턴스(`data/wf-workspaces/`)에 둔다. 1단계 하우스키핑이 세션 없는 작업공간 폴더를 지우기 때문이다.

### D5. 캔버스는 `@xyflow/react` 12, DSL은 우리 YAML
- `@xyflow/react`(MIT, React 19 지원)를 편집기 라우트에서만 lazy 로드한다. 화면 구성과 노드 카드 디자인은 우리 토큰으로 만든다.
- DSL은 저장 형식(`wf_version.graph`)과 같은 구조의 YAML이다(`dsl_version: 1`). Dify DSL은 지원 노드만 부분 가져오기하고 나머지는 `unsupported` 자리로 들어온다. Dify 형식 내보내기는 없다.

### D6. 한도와 완료 기준
- 노드 100·엣지 300, 변수 풀 16MB, 노드 런 기록 64KB(iteration 하위 8KB), iteration 1,000(중첩 금지, 병렬 4), loop 100, code 노드 30초·256MB·동시 1, 런 1800초·토큰 2,000,000. 서버 RSS 300MB 유지.
- 2단계 완료 기준은 M18(DSL)과 M20(하드닝)까지다. M19(2차 노드)는 범위에 들어가지만 완료 기준에서 제외한다.

## 영향
- 2단계 설계와 마일스톤(M11~M20)은 `docs/design/phase2-workflow.md`. 1단계 접점 변경(종료 이벤트 집합 여섯 곳, 종료 코디네이터, 두 번째 백엔드 인스턴스, 복구 순서, 대기 만료 역조회 등)은 그 문서 12장에 파일·행 단위로 적었고 1단계 문서는 고치지 않는다.
- 3단계 RAG는 `knowledge_retrieval` 노드 타입 이름만 예약해 둔다.
- 신규 의존성: `pyyaml`(서버), `@xyflow/react`(화면). 2차에 `@dagrejs/dagre`. jinja2는 지연 import에서 기동 import로 바뀐다.
