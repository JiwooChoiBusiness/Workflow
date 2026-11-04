# 4단계(고도화) 선택지 문서 v1.1

- 상태: v1.1 (architect, 2026-11-02). v1.1은 design-reviewer 검토와 3단계 설계서 v2를 반영한 판이다. 2026-11-03에 사용자 지시로 8절에 Graph RAG 후보를 더했고, 2026-11-04에 안 D로 확정했다(`phase4-graphrag.md` v2, ADR-0009).
- 성격: 세부 설계서가 아니라 결정할 쟁점과 선택지를 늘어놓은 문서다. 1절만 읽어도 결정은 할 수 있고, 2절 이후는 각 선택지의 근거다. 사용자와 논의해 범위를 정한 뒤 세부 설계와 ADR을 쓴다.
- 전제: ADR-0001~0006은 유지한다(개인용 1인, Windows 11 단일 프로세스, SQLite, Gemini 전용, RSS 300MB, 샌드박스 격리, 다인 기능 없음). 2단계 끝 기준 memcheck 최대 137MB라 여유는 약 160MB다. 3단계(RAG·지식 쓰기·Text2SQL)는 `docs/design/phase3-rag.md`가 다루며 여기서는 "3단계에서 다룸"으로만 연결한다.
- 표기: [실측]은 이 저장소의 키로 실제 호출해 본 결과(`docs/research/gemini-api-2026-10.md`), [문서]는 공식 문서, [간접]은 제3자 자료, [추정]은 내 추론이다. 문서 조사와 실측이 다르면 실측을 따랐다. 3단계 설계서와 같은 표기다.

## 1. 사용자가 정할 것 (우선순위 순)

### D1. 결제 연결 여부와 키 운용
- 사실: 키 8개가 모두 무료 등급이다 [실측]. 무료 등급은 입력과 응답을 제품 개선에 쓰고 사람이 읽을 수 있으며 "민감·기밀·개인 정보를 넣지 말라"고 한다 [문서]. 이미지 생성, Google 검색 그라운딩, Gemini 4 Argon, Omni(영상), Lyria(음악), computer use, Batch는 결제 없이는 못 쓴다 [실측·문서]. TTS, 받아쓰기, 임베딩은 무료 등급에서 실제로 됐다 [실측]. Live, URL context, File Search도 무료 등급에서 쓸 수 있다고 문서에 있다 [문서].
- 선택지
  - (a) 전부 무료로 둔다. 4단계는 TTS·받아쓰기·멀티모달 입력·도구 확장까지만.
  - (b) 결제를 연결한 새 프로젝트를 하나 만들고 그 키 1개를 유료 키(`GEMINI_PAID_API_KEY`)로 따로 둔다. 기존 8개 중 하나에 결제를 붙이면 그 키의 모든 호출이 과금되면서 무료 풀에 섞여 돌므로 안 된다. 기동 때 `GEMINI_PAID_API_KEY`가 `GEMINI_API_KEYS`에 들어 있으면 거부한다. 무료 풀 8개는 그대로 둔다. 유료 전용 기능과, 사용자가 "유료 키로만"이라고 표시한 에이전트·워크플로우(3단계의 `key_tier: paid` 지식 베이스·데이터 소스를 참조하는 런 포함)만 그 키를 쓴다. 유료 등급이어도 내용은 Google로 가고, 제품 개선에 쓰이지 않을 뿐이다.
  - (c) 전부 결제 연결.
- 추천: (b). 3단계 설계서 Z1과 같은 결정이고 한 번만 내린다. 이유: 기능 문을 열면서 비용과 데이터 정책을 키 하나로 통제할 수 있다. 민감한 데이터를 다루는 에이전트는 "유료 키 전용"으로 표시해 무료 약관을 피한다. 과금 보호는 이미 있는 런당 `max_cost_usd`에 워크스페이스 월 상한(설정 하나)을 더해 유료 키 호출을 막는다. (a)를 고르면 묶음 C의 C4~C5와 Google 검색 그라운딩은 빠지고 나머지는 그대로 간다.
- 키 운용: 키 8개는 서로 다른 프로젝트로 보이고 각자 분당 한도를 가진다 [실측]. 지금 채팅 어댑터(`apps/api/app/llm/gemini_adapter.py`의 `_run_chain`)는 429·401·403에서 다음 키로 넘어가고, 5xx·연결 오류는 같은 키로 1회 재시도한 뒤 다음 모델로 간다. 503에서 키를 바꾸는 코드는 없다. Gemini의 429는 전부 한도(quota) 응답이라 "429에서 다른 키로 넘어갈지"는 곧 "다른 프로젝트의 한도를 쓸지"이고, 약관 2(d)(문서화된 한도 우회 금지. 여러 프로젝트를 직접 언급하지는 않는다)와 맞닿는다. 이 쟁점은 3단계 설계서 Z2(429 때 키 순환, 채팅·임베딩 공통)에서 사용자가 정하고, 이 문서는 따로 결론을 내지 않는다. 4단계에서 새로 생기는 호출(TTS, 받아쓰기, 영상 이해, 이미지 생성)도 그 결정을 따른다. 어느 쪽으로 정해도 한도를 늘리려고 요청을 키에 분산하는 로직은 만들지 않는다. 임베딩 일일 한도는 프로젝트당 하루 1,000건으로 실측됐고, 채팅·TTS·받아쓰기 모델의 일일 한도는 모른다(실측 목록 1번).

### D2. 네이티브 Gemini 경로: generateContent인가 Interactions인가
- 사실: OpenAI 호환 경로로 되는 것은 chat, 임베딩, 이미지 입력, `input_audio`(wav), `image_url`에 PDF 데이터 URL뿐이다. TTS·받아쓰기 전용 엔드포인트·새 이미지 모델·검색 그라운딩은 404 또는 400이다 [실측]. 네이티브 `generateContent`와 Interactions API 둘 다 이 키로 200이고, `/models`의 지원 메서드를 보면 TTS·이미지·받아쓰기·Omni·Lyria·Deep Research·computer use·Antigravity 모델이 모두 `generateContent`를 지원한다 [실측]. Interactions는 기본으로 서버에 저장하고(`store=false` 가능) background와 `previous_interaction_id`는 저장이 전제다 [문서]. Interactions가 GA인지 Beta인지는 문서끼리 다르다.
- 선택지: (a) `generateContent`를 기본으로, 필요한 기능만 Interactions / (b) Interactions를 기본으로 / (c) `google-genai` SDK.
- 추천: (a). 이유: 우리가 4단계에서 쓰려는 기능(TTS, 받아쓰기, 영상·오디오 입력, Files API)은 모두 `generateContent`로 되고, 무상태라 지금 어댑터와 같은 뜻(요청 하나에 결과 하나, 서버 저장 없음)을 유지한다. Interactions가 꼭 필요한 것은 Deep Research·computer use·`mcp_server` 도구·이미지 다중 턴 편집인데, 이 문서는 그 넷을 빼거나 뒤로 미룬다. (c)는 `google-auth`, `requests`, `tenacity` 등을 더 끌어오고 키 풀·폴백·마스킹을 또 만들게 되므로 뺀다. 전송 계층은 엔드포인트별로 나눈다(3절). 3단계는 File Search를 쓰지 않기로 했으므로(3단계 설계서 Z3) Interactions 전송은 만들 이유가 없고, 검색 서비스의 공급자 경계만 남아 있다.

### D3. 도구 확장을 무엇으로 하나
- 선택지: (a) 내장 도구만 늘린다 / (b) MCP 클라이언트(stdio + Streamable HTTP) / (c) OpenAPI 명세로 도구 만들기 / (d) 사용자 정의 파이썬 도구(샌드박스 실행). 조합 가능.
- 추천: 바탕(도구 목록 통합, 자격 증명 저장, 승인 등급)을 먼저 깔고 (b) → (d) → (c) 순서. 이유: 사용자가 원하는 것이 "선택지를 늘리는 것"이라면 MCP가 구현 하나로 가장 많은 도구를 연다. (d)는 code 노드 기계를 그대로 써서 작고, 개인용 접착 코드를 이름 붙인 도구로 만드는 데 유용하다. (c)는 의존성 없이 되지만 명세와 인증을 API마다 손봐야 해서 쓸모가 사용자의 API 목록에 달렸다. 바탕 없이 (b)부터 가면 비밀값과 승인이 또 갈라진다.

### D4. 멀티모달 중 무엇부터
- 추천 순서: C1 입력 이해(이미지는 있음. PDF·오디오·영상 추가) → C2 받아쓰기 + C3 TTS(무료, 실측 완료, 네이티브 경로의 첫 소비자) → C4 이미지 생성(D1에서 결제를 연결했을 때만) → C6 Live 음성 대화(선택) → C5 음악·영상 생성(권하지 않음).
- 이유: 입력 이해는 어댑터의 `ImageBlock`을 일반 미디어 블록으로 넓히는 일이라 OpenAI 호환 경로 안에서 대부분 끝난다. 받아쓰기·TTS는 무료이고 이미 200을 봤다. 이미지 생성은 결제가 전제다. Live는 WebSocket과 브라우저 오디오까지 들어가 크기가 크고, 음악·영상은 유료에 비싸고 개인용 쓰임이 적다.

### D5. 웹 검색 공급자
- 사실: Google 검색 그라운딩은 Gemini 3 이상 무료 등급에서 "Not available"이고 실제로도 429였다 [실측]. 결제를 연결하면 월 5,000건 무료, 이후 1,000건당 $14이며 네이티브 경로에서만 쓸 수 있고 함수 호출과 한 요청에 섞을 수는 있으나 OpenAI 호환 경로에서는 400이다 [실측·문서]. 외부 검색 API는 별도 키가 필요하다.
- 후보(제3자 가격 요약, 공식 페이지에서 확인 필요):

| 공급자 | 무료 | 유료 | 비고 |
|---|---|---|---|
| Tavily | 월 1,000크레딧, 카드 없음 | 종량 $0.008/크레딧(기본 검색 1, 심화 2) | LLM용 결과(본문 발췌 포함). 2026년 인수 소식이 있어 가격 변동 가능 [간접] |
| Brave Search | 월 $5 크레딧(약 1,000건), 카드 필요 | $5/1,000건 | 2026-02부터 고정 무료 티어 폐지, 출처 표기 의무 [간접] |
| Serper | 가입 시 2,500건 1회 | $50/50,000건부터 | Google SERP 결과. 크레딧 만료 규정이 자료마다 다름 [간접] |
| Google 그라운딩 | 무료 등급 불가 | 월 5,000건 뒤 $14/1,000건 | 모델 안에서 검색·인용. 결제 필요, 네이티브 전용 |

- 추천: Tavily를 기본 `web_search` 내장 도구로, 공급자 인터페이스를 두어 Brave·Serper로 바꿀 수 있게. 이유: 카드 없이 월 1,000건이면 개인용에 충분하고, 결과에 본문 발췌가 있어 `web_fetch`를 한 번 덜 부른다. Google 그라운딩은 D1 (b) 이후 llm 노드의 선택 옵션(네이티브 경로)으로만 둔다. 에이전트의 함수 호출과는 섞지 않는다.

### D6. 에이전트와 워크플로우 tool 노드가 같은 도구 목록을 쓰는가
- 추천: 쓴다. 새 도구 종류(검색, MCP, OpenAPI, 사용자 파이썬, 멀티모달 도구)는 공용 레지스트리 하나에 등록하고 에이전트용·노드용 어댑터 두 개가 감싼다. 기존 에이전트 도구(셸·파일·HTTP·질문·call_agent 등)는 작업공간이 전제라 에이전트 전용으로 남기고 손대지 않는다. 기존 워크플로우 내장 4종(`current_time`, `json_parse`, `regex_extract`, `web_fetch`)은 공용 레지스트리로 옮겨 에이전트도 쓸 수 있게 한다. 3단계가 1단계 내장 도구로 먼저 만드는 `search_knowledge`, `describe_sql_source`, `query_sql`도 4-2에서 공용 레지스트리의 `kind: knowledge|sql` 항목으로 옮긴다. 이유: 도구를 두 번 만들지 않고, 승인·자격 증명·마스킹을 한 곳에서 한다.

### D7. 그 밖의 후보 중 넣을 것
- 일정 트리거: 추천(작다, 개인 자동화에 바로 쓸모). 웹훅 트리거: 127.0.0.1 한정이라 "같은 PC의 다른 프로그램이 부르는 용도"로만 가치가 있다. 작으니 일정과 묶어 선택. 다른 LLM 공급자: ADR-0003·0004를 바꾸는 일이고 지금 권하지 않는다. 워크플로우를 도구로(발행한 워크플로우를 에이전트 도구로): 도구 바탕 위에서 작게 되므로 선택. Graph RAG: 사용자가 후보로 올렸고(2026-11-03) 안 D(단계판)로 확정했다(2026-11-04, ADR-0009). 상세는 8절과 `phase4-graphrag.md`.

### D8. 관리형 에이전트(Deep Research, computer use, Antigravity)
- 추천: 셋 다 4단계에서 뺀다. Deep Research는 Interactions 전용·저장 전제·프리뷰·수 분에 고토큰이고 검색 그라운딩에 기대므로 무료 등급에서 될지도 불확실하다. computer use는 Playwright와 가상 머신 수준 격리를 권하는데 이 PC에는 컨테이너가 없다. Antigravity는 코드와 파일이 Google 쪽 Linux 샌드박스에서 도는 관리형이라, 에이전트 샌드박스를 이 PC에서 직접 돌리는 1단계 구조와 따로 논다. 대안은 7절(지휘자 에이전트 + `web_search` + `web_fetch`로 만드는 조사 에이전트).

### 1.9 선택지 한눈에 보기

| 선택지 | 추천 | 결제 | 새 의존성 | 메모리·보안 영향 | 크기(마일스톤) |
|---|---|---|---|---|---|
| 네이티브 Gemini 클라이언트(3절) | 한다. 모든 멀티모달의 전제. 3단계는 만들지 않는다 | 불필요 | 없음(httpx) | RSS 거의 0, 키는 헤더로 | 1 |
| A1 도구 목록 통합(3단계 도구 셋 이전 포함) | 한다 | 불필요 | 없음 | 없음 | 0.5 |
| A2 자격 증명 저장 | 한다 | 불필요 | 없음 | 마스킹 범위 확장이 핵심 | 0.5 |
| A3 승인 등급 | 한다 | 불필요 | 없음 | 승인 판정 한 곳 유지 | A1·A2와 함께 |
| A4 MCP 클라이언트 | 한다 | 불필요 | `mcp`와 부속 5종, `pywin32` | RSS +10~30MB [추정], 서버 프로세스는 별도, 제3자 코드 실행 | 1.5 |
| A5 OpenAPI 도구 | 선택 | 불필요 | 없음 | 거의 없음 | 0.5 |
| A6 사용자 파이썬 도구 | 한다 | 불필요 | 없음(샌드박스에만 패키지) | code 노드와 같음 | 1 |
| B1 웹 검색 | 한다 | 외부 키(무료 범위) | 없음 | 허용 목록 자동 추가, 건수 과금 | 0.5 |
| B2 변환·차트·문서 도구 | A6 뒤 | 불필요 | 샌드박스 패키지만 | 서버 RSS 0, 디스크 약 100MB | 0.5 |
| B3 Gemini 코드 실행·URL context | 지금은 안 함 | 불필요 | 없음 | 코드·URL 내용이 Google로 감 | - |
| C1 입력 이해(PDF·오디오·영상) | 한다 | 불필요 | 없음 | 큰 파일은 Files API로, 메모리 상주 없음 | 0.5 |
| C2 받아쓰기 | 한다 | 불필요 | 없음 | 음성 데이터가 Google로(무료 약관) | 0.5 (+0.5 녹음 UI) |
| C3 TTS | 한다 | 불필요 | 없음 | 오디오는 파일로만 | 0.5 |
| C4 이미지 생성 | D1 (b)일 때만 | 필요 | 없음 | 장당 과금, 월 상한 필요 | 0.5 |
| C5 음악·영상 생성 | 뺀다 | 필요 | 없음 | 고비용 | - |
| C6 Live 음성 대화 | 선택 | 불필요 | 없음(브라우저 직결) | 토큰 발급 경로 보호, 도구 없음 | 1.5 |
| D 관리형 에이전트 3종 | 뺀다 | 대부분 필요 | - | 저장 전제, 격리 불가 | - |
| E 일정·웹훅 트리거 | 일정은 한다, 웹훅은 선택 | 불필요 | 없음 | 127.0.0.1 한정 | 0.5 + 0.3 |
| E 워크플로우를 도구로·서브워크플로우 | 선택 | 불필요 | 없음 | 재귀 깊이 상한 | 0.5 |
| E 우리 앱을 MCP 서버로 | 선택 | 불필요 | `mcp`(A4와 공유) | 서비스 API 키 인증 | 0.5 |
| E 다른 LLM 공급자 | 지금은 안 함(ADR 변경) | 공급자 과금 | 없음 | 공급자별 분기 | - |

## 2. 지금 코드에 영향 (3단계 M21이 먼저 처리한다)

아래 셋(폴백 기본값, `temperature`/`top_p` 게이트, 캐시 비율 0.10)은 3단계 설계서 15장대로 M21이 처리한다. 4단계가 먼저 가면 4-1이 한다. 폴백 기본값 변경은 ADR-0004 D3의 "3.8 → 3.7 → 3.6"과 어긋나므로 ADR 한 줄 개정이 따라오고, 지금 돌아가는 채팅 경로의 기본 동작이 바뀌는 항목이라 사용자에게 보여 준다.

- 폴백 체인 `3.8 → 3.7 → 3.6`의 가운데: `gemini-3.7-flash`는 3.8로 라우팅된다 [문서]. 2026-10-30에 3.8이 79초·503, 3.7이 147초, 3.6이 2.5초였다 [실측]. 기본값을 `gemini-3.8-flash,gemini-3.6-flash`로 줄이고 `catalog.py`의 3.7 항목에는 "3.8 별칭"이라고 적는다. 파일: `apps/api/app/config.py:58-60`, `apps/api/app/llm/catalog.py:40-45`, `.env.example`.
- `temperature`·`top_p`: 3.6부터 무시되고 이후 세대는 400이라고 문서에 있다. 같은 날 세 모델 모두 200이었지만 무시 여부는 안 봤다 [실측]. `build_request_body`(`gemini_adapter.py:478-481`)에서 능력표의 `supports_sampling_params`(기본 False)가 참일 때만 보내도록 바꾸고, 스키마(`schemas/agent_version.py:52-53`)와 화면에는 남기되 "이 모델에서는 무시됨"을 표시한다. DSL 가져오기(`dify_import.py:531-536`)는 값을 받되 경고를 남긴다.
- 2026-11-17 종료 모델(TTS 프리뷰, 3.1 Flash Live 프리뷰, 2.5 네이티브 오디오 프리뷰): 지금 코드는 참조하지 않는다. 4단계는 GA 모델(`gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts`, `gemini-3.5-transcribe`, `gemini-3.8-live`)만 쓴다.
- 가격: 3.8 Flash 유료 단가가 2027-01-01에 2배가 된다 [문서]. 가격표는 사용자 입력이라 코드 변경은 없고 운영 문서에 적는다. 암묵 캐시는 OpenAI 호환 경로에서 `cached_tokens`가 실제로 온다 [실측]. 캐시 단가 기본 비율 25%(`pricing.py:9`)는 3.8 Flash 실제(0.075/0.75 = 10%)와 다르니 기본값을 0.10으로 낮춘다(M21).
- `reasoning_effort=minimal`이 3.8에서 400인 것은 이미 어댑터가 올려 준다. 변경 없음.
- httpx·httpcore 로그를 WARNING부터만 남기는 규칙(`logsetup.py:17`)은 네이티브 클라이언트에도 그대로 적용된다. 키는 URL이 아니라 `x-goog-api-key` 헤더로 보낸다.

## 3. 공통 바탕: 네이티브 Gemini 클라이언트

### 3.1 무엇을 만드나
- `app/llm/gemini_native.py`에 `GeminiNativeClient` 하나. 의존성은 이미 있는 `httpx`뿐이다(`openai` SDK는 안 거친다). 메서드는 `generate(model, parts, config) -> NativeResult`, `stream_generate(...)`(`streamGenerateContent?alt=sse`), `upload_file(path, mime) -> file_uri`(Files API, 재개 가능 업로드), `delete_file`. 요청 본문은 기능별 빌더(TTS, 받아쓰기, 이해, 이미지 생성)가 만들고 클라이언트는 전송·키·오류만 다룬다.
- 전송은 엔드포인트별 클래스(`GenerateContentTransport`, `FilesTransport`)로 나눈다. 3단계 M21이 `app/llm/embeddings.py`에 두는 `EmbeddingTransport` 프로토콜의 네이티브 구현(`embed_content`·`count_tokens`)도 여기서 더해 001 task type·정확한 토큰 수·멀티모달 임베딩을 연다(3단계 설계서 3.5·Z8). Interactions 전송은 만들지 않는다. 3단계가 File Search를 쓰지 않기로 했고(Z3) 검색 서비스에 공급자 경계만 남겼으므로, 전송 클래스를 하나 더 끼울 자리만 둔다.
- 채팅과 함수 호출은 계속 OpenAI 호환 어댑터가 맡는다. 네이티브 클라이언트는 "채팅이 아닌 호출"만 한다. 사고 서명 처리를 두 벌 만들지 않기 위해서다.

### 3.2 기존 자산 재사용
- 키 풀: `GeminiAdapter`가 가진 `KeyPool` 인스턴스를 그대로 넘겨 쓴다. `pick(model, tier=)`·`mark_rate_limited(key, model)`·`mark_auth_failed`·`redact`를 같은 뜻으로 부른다. `KeyPool.tier`(free/paid)와 `pick(model, tier=)`, 설정 `GEMINI_PAID_API_KEY`는 3단계 M21이 만든다(3단계 설계서 Z1·18장). 4-1은 능력표에 `requires_paid`(이미지·Argon·Omni·Lyria)를 더해 유료 키가 없으면 429를 돌지 않고 바로 "결제 키가 필요한 모델" 오류를 낸다.
- 유료 키 규칙(D1 (b)): 유료 키는 새 프로젝트의 키 하나다. 기동 때 `GEMINI_API_KEYS`에 같은 값이 있으면 거부하고, `Settings.secret_values()`와 `KeyPool.redact` 목록에 넣어 로그에서 가린다. 유료 키가 429·503이면 무료 키로 떨어지지 않고 기다렸다가 같은 키로 다시 시도한다(채팅 어댑터의 모델 폴백도 유료 키 안에서만 돈다). `ChatRequest`에 tier를 실을 자리가 없으므로 `ChatRequest.params.tier`를 더하고 `_run_chain`이 `pick(model, tier=)`를 쓴다. 자식 런(`call_agent`, 2단계 agent 노드)은 부모와 자기 tier 중 높은 쪽을 상속한다(`ChildRunService`). 유료 지식 베이스 내용이 들어간 지시를 받은 자식이 무료 키로 나가는 구멍을 막기 위해서다. 이 규칙은 `KeyPool.tier`와 함께 M21에 들어간다.
- 폴백: 기능마다 짧은 체인을 설정으로 둔다(TTS `3.8-flash-tts → 3.8-flash-lite-tts`, 받아쓰기는 `3.5-transcribe` 하나, 이해는 채팅 체인과 같음). 오류 분류는 M21이 `app/llm/errors.classify`로 공용화한 것을 쓴다. 5xx·연결 오류는 같은 키로 1회 재시도 뒤 다음 모델, 400은 즉시 실패, 429는 D1의 결정(3단계 설계서 1절)을 따른다. 채팅 어댑터와 같은 규칙이다.
- 비밀값: 오류 본문은 `KeyPool.redact`를 거친다. 로그는 `logsetup`의 패턴 마스킹(AIza·AQ.)이 그대로 덮는다. 결과 텍스트가 델타로 흐르면(받아쓰기 스트리밍 등) 기존 `StreamMasker`를 그대로 쓴다. 응답 바이너리(오디오·이미지)는 로그·이벤트에 싣지 않고 파일로만 남긴다.
- 사용량: `usageMetadata.promptTokenCount/candidatesTokenCount/thoughtsTokenCount`를 `Usage`로 바꾼다(사고 토큰은 출력에 더하는 지금 규칙 유지). 오디오·이미지 출력은 토큰 단가로 안 떨어지므로 `Usage`에 선택 필드 `media_out`(`{"audio_sec": 12.3}` 또는 `{"images": 1}`)을, `ModelPrice`에 `audio_output_per_sec_usd`·`per_image_usd`를 선택 필드로 더한다. 집계(`services/usage.py`)는 런 단위 합이라 바뀌지 않고, 노드·도구가 `node.usage`/`usage` 이벤트를 내는 자리만 늘어난다. 임베딩 호출의 사용량은 3단계 방식대로 KB 통계에만 남고 여기 들어오지 않는다(9절).
- 메모리: 새 import가 없어 거의 0이다. 큰 바이너리는 파일로 흘려 쓴다(3.3).

### 3.3 파일 크기 규칙
- 인라인 base64는 요청 20MB 이하 [문서]. 입력 파일이 15MB를 넘으면 Files API로 올리고 `file_uri`로 넣는다(파일당 2GB, 프로젝트 20GB, 48시간 뒤 삭제, 내려받기 불가 [문서]). 영상은 항상 Files API.
- 출력 오디오는 WAV 24kHz 16-bit mono라 분당 약 2.9MB다. 스트리밍 응답을 런 폴더(워크플로우는 `wf-workspaces/<run>/`, 에이전트는 세션 작업공간)에 바로 쓰고 메모리에 두지 않는다. 상한은 TTS 입력 20,000자·출력 10분, 받아쓰기 입력 1시간(문서 한도)으로 시작한다.

## 4. 묶음 A: 도구 확장의 바탕

### A1. 도구 목록 통합 (D6)
- 주는 것: 새 도구를 한 번 만들면 에이전트 빌더의 "도구" 목록과 tool 노드의 도구 고르기에 같이 나온다.
- 모양: `app/tools/registry.py`의 `ToolDefinition{id, kind: builtin|knowledge|sql|mcp|openapi|python|workflow, name, description, params_schema(JSON Schema), outputs, risk: read|write|exec, credential_refs, source}`. 에이전트 쪽은 `agent/tools/base.Tool`을 감싸는 `RegistryTool`(그룹 `extra`), 노드 쪽은 `workflow/tools/base.Tool`을 감싸는 어댑터. 실행 본체는 `kind`별 러너 하나씩.
- 에이전트 버전 설정에 `tool_ids: list[str]`(상한 40)를 더한다. 함수 이름은 `[A-Za-z0-9_-]{1,64}` 규칙에 맞춰 `<종류>__<이름>`으로 만들고 충돌은 저장 때 검증. 모델에 보내는 스키마는 기존 `model_schema()`가 `anyOf`·`$ref` 등을 떨어뜨리므로 MCP·OpenAPI 스키마는 등록 때 한 번 평탄화하고, 검증은 지금처럼 전체 스키마로 우리가 한다. Gemini 권고(활성 도구 10~20개)를 빌더 경고로 둔다.
- tool 노드 폼: 지금은 `ToolParam` 목록으로 칸을 만든다. 중첩 스키마가 오는 MCP·OpenAPI 도구는 "칸 모드"(최상위 속성만)와 "JSON 모드"(객체 하나를 템플릿으로) 둘을 둔다.
- 크기: 마일스톤 1개의 절반. 3단계와의 관계: 3단계는 A1이 없는 상태에서 `search_knowledge`(그룹 `knowledge`), `describe_sql_source`·`query_sql`(그룹 `sql`)을 1단계 내장 도구로 먼저 만들고, 매개변수·결과 모양을 `ToolDefinition{kind: knowledge|sql, risk: read}`에 맞춰 둔다(3단계 설계서 8.4·10.3). 4-2가 셋을 레지스트리 항목으로 옮겨 tool 노드에서도 같은 도구를 쓰게 한다. `knowledge_retrieval`·`sql_query` 같은 노드는 도구가 아니라 그대로다.
- 추천: 한다. 묶음 A·B·C의 전제다.

### A2. 도구 자격 증명 저장
- 주는 것: 검색 API 키, OpenAPI 인증, MCP 헤더·환경변수를 이름으로 참조하고 값은 화면·로그·DSL에 나오지 않는다.
- 모양: 새 테이블 `credential(id, workspace_id, name, kind: api_key|bearer|basic|header|dsn, value_encrypted, created_at, updated_at)`. 암호화는 `security/crypto.py`의 AES-GCM 그대로(`ENCRYPTION_KEY` 없으면 503, 기존 규칙). 콘솔 설정 화면에 목록·추가·교체·삭제. 값 읽기 API는 없다. 도구 설정은 `{"credential": "<name>"}`로 참조. DSL 내보내기는 이름만 내고 `include_secrets`는 지원하지 않는다(env와 다르게. 도구 자격 증명은 워크플로우 소유가 아니라서).
- 마스킹: 런 시작 때 그 런이 쓰는 자격 증명 값을 `Masker`/`StreamMasker`의 비밀값 집합에 넣는다. 로그 마스킹(`logsetup.secret_masker`)은 기동 때의 고정 목록이라, 런타임에 추가되는 값을 보는 `SecretRegistry`(프로세스 전역 집합)로 바꾼다. 작은 변경이지만 보안상 중요하다.
- OAuth 흐름은 만들지 않는다(브라우저 리다이렉트·갱신 토큰 관리가 커서). 필요한 API는 개인 토큰으로. 3단계가 외부 DB 데이터 소스를 붙일 때는 `kind: dsn`을 쓴다(3단계 설계서 10.1).
- 크기: 마일스톤 1개의 절반. 추천: 한다.

### A3. 승인 등급
- 지금: 승인 판정은 `agent/interactions.py`의 `requires_approval`이 도구 이름으로 한다. 워크플로우의 `code`·`http_request`·`tool` 노드는 승인 없이 돈다(설계 시점 사용자 설정이므로).
- 제안: 레지스트리 도구마다 `risk`를 두고 승인 모드와 교차한다.

| 도구 | 기본 risk | all_shell | http_writes | off |
|---|---|---|---|---|
| 내장 검색·web_fetch·변환 도구 | read | 자동 | 자동 | 자동 |
| OpenAPI GET/HEAD | read | 자동 | 자동 | 자동 |
| OpenAPI 쓰기 메서드 | write | 승인 | 승인 | 자동 |
| MCP `readOnlyHint=true` | read | 자동 | 자동 | 자동 |
| MCP 그 외(`destructiveHint` 포함, 힌트 없음) | write | 승인 | 승인 | 자동 |
| 사용자 파이썬 도구 | read(도구마다 바꿀 수 있음) | 자동 | 자동 | 자동 |
| 3단계 `search_knowledge`(knowledge) | read | 자동 | 자동 | 자동 |
| 3단계 `describe_sql_source`·`query_sql`(sql) | read | 자동 | 자동 | 자동 |

- MCP 서버는 서버 단위로 "첫 호출만 승인"·"매번 승인"·"읽기는 자동" 세 가지를 고른다. 세션 자동 승인과 부모·자식 전파는 그대로 적용된다. 워크플로우 tool 노드는 지금 규칙(승인 없음)을 유지하되, 노드 설정 `approval: true`를 켜면 `human_input`의 폼 대기 기계로 "이 호출을 실행할까요" 카드를 띄우는 선택지를 둔다(작다. 폼 종류 하나 추가).
- 승인 카드는 MCP 도구의 서버 이름·도구 설명·인자 전문을 보여 준다. 도구 설명은 서버가 주는 신뢰할 수 없는 글이므로 등록 때 스냅샷(해시)을 저장하고 바뀌면 경고 배지를 단다(도구 설명을 통한 프롬프트 인젝션 대비).
- 크기: A1·A2와 한 마일스톤. 추천: 한다.

### A4. MCP 클라이언트
- 주는 것: 외부 MCP 서버(파일시스템, GitHub, 브라우저 자동화, DB, 각종 SaaS)의 도구를 에이전트와 tool 노드에서 함수 호출로 쓴다.
- 필요한 것: `mcp` 2.3.0(v2, `Client` 하나로 stdio·Streamable HTTP) [문서]. 의존성 중 `httpx2`(이미 `openai` 3.29가 쓴다, uv.lock 2.13.1), `pydantic`·`anyio`·`starlette`·`uvicorn`·`jsonschema`·`python-multipart`·`cryptography`는 이미 있고, 새로 오는 것은 `mcp`, `mcp-types`, `sse-starlette`, `pyjwt`, `opentelemetry-api`, Windows의 `pywin32`다. 임포트 시간·메모리는 재지 않았다(실측 7번). 첫 사용 때 지연 import한다. Gemini 쪽 MCP 지원은 Interactions 전용이고 문서가 모순되므로 쓰지 않는다. 우리가 클라이언트를 직접 두고 `tools/list` 결과를 일반 함수 호출로 노출한다. 이러면 채팅 경로는 그대로 OpenAI 호환 하나다.
- 모양: 테이블 `mcp_server(id, workspace_id, name(snake), transport: stdio|http, command, args, cwd, env_refs, url, header_refs, enabled, approval_policy, tools_snapshot, snapshot_hash, created_at)`. 설정 화면에서 추가·연결 시험(`tools/list`)·도구 목록 보기·활성 도구 고르기. `MCPServerManager`가 서버 연결을 들고 있다가 유휴 5분이면 닫고, stdio 서버 동시 실행은 2개로 제한한다(설정). 서버 종료 때 전부 닫는다.
- stdio와 샌드박스: 서버 프로세스는 우리 PC에서 사용자 권한으로 돈다. 컨테이너가 없으니 격리는 (1) Job Object로 메모리·프로세스 수 상한과 트리 종료(`sandbox/jobobject_win.py` 재사용), (2) 작업 폴더를 `data/mcp/<server>/`로 고정, (3) 환경변수는 최소 목록 + 선언한 자격 증명만(`os.environ` 상속 안 함, 1단계 3.6과 같은 규칙), (4) 출력 상한과 시간 상한(`tools/call` 60초 기본, 최대 300)이다. `mcp` SDK가 프로세스를 직접 띄우므로 Job 할당은 pid로 뒤늦게 한다. 할당 틈(1단계 12.3과 같은 리스크)은 받아들이고, 할당 실패면 즉시 종료한다. SDK v2가 외부에서 띄운 프로세스의 스트림을 받는 생성자를 주는지 구현 첫날 확인한다.
- Windows 주의: `npx`는 `npx.cmd` 또는 `cmd /c`로, 파이썬 서버는 `PYTHONUTF8=1`, 콘솔 창이 뜨지 않게 `CREATE_NO_WINDOW`, Proactor 루프는 이미 보장(`runner/winconsole.py`). Node 서버는 프로세스마다 60~200MB를 쓴다 [추정]. 서버 RSS에는 안 들어가지만 PC 여유 메모리에는 들어가므로 동시 2개 제한이 중요하다. Node 24는 웹 빌드용으로 이미 있다.
- HTTP 서버: URL은 사용자가 등록하므로 등록 때 한 번 호스트 검사(공개 주소, `localhost` 거부)를 하고 허용 목록에 그 호스트를 자동으로 더한다. 요청마다 IP 고정은 하지 않는다(SDK 클라이언트를 그대로 쓰기 위해). 로컬에서 띄운 HTTP MCP 서버를 쓰고 싶으면 설정 `MCP_ALLOW_LOCAL_HTTP=true`로 연다.
- 결과 처리: MCP 결과의 text는 32KB 발췌 규칙 그대로, image·resource 블록은 파일로 저장해(에이전트는 작업공간 `mcp/<server>/<call>/`, 워크플로우는 `wf_file`) 모델에는 경로와 mime만 준다.
- 메모리·보안 영향: 서버 RSS +10~30MB [추정], 외부 프로세스는 별도. 서버 코드는 제3자 코드라 승인 등급(A3)과 자격 증명 최소 주입이 핵심이다. 도구 결과는 신뢰하지 않는 데이터로 프롬프트 규칙을 유지한다.
- 크기: 마일스톤 1.5개(백엔드 1, 화면 0.5). 3단계 관계: 없음. 추천: 한다(D3).

### A5. OpenAPI 명세로 도구 만들기
- 주는 것: REST API 명세(OpenAPI 3.x JSON/YAML) 하나를 올리면 작업(operation)마다 도구가 생긴다.
- 필요한 것: 새 의존성 없음(`pyyaml`·`jsonschema`·`httpx` 있음). 지원 부분집합은 `operationId`, path·query·header 파라미터, `requestBody`의 `application/json`, 응답은 텍스트·JSON. 인증은 A2의 자격 증명(apiKey·bearer·basic). 실행은 1단계 `HttpClient`(허용 목록·사설 IP 차단·리다이렉트 재검사·5MB)로만 나가고 명세의 서버 호스트를 등록 때 허용 목록에 더한다. 큰 응답은 http_request 노드와 같은 파일 규칙.
- 메모리·보안: 거의 없음. 쓰기 메서드는 A3의 write 등급.
- 크기: 마일스톤 1개의 절반(명세 파서 + 등록 화면). 추천: 선택. 사용자가 자주 쓰는 REST API가 있으면 넣고, 없으면 http_request 노드로 충분하다.

### A6. 사용자 정의 파이썬 도구
- 주는 것: `def main(**params) -> dict`를 쓰고 매개변수·출력 선언을 붙이면 이름 있는 도구가 된다. 에이전트와 tool 노드에서 같이 쓴다. 차트 만들기, 표 변환 같은 개인용 접착 코드를 재사용한다.
- 필요한 것: 새 의존성 없음. 실행은 code 노드 러너(`python -I`, 30초, 256MB, 프로세스 8, 반환 256KB)를 그대로 쓴다. 패키지가 필요한 도구는 `requirements`를 선언하고 처음 실행 때 `pip --target`으로 `data/cache/tools/<tool>/`에 받아 둔다(document_extractor의 파서 캐시와 같은 방식, 동시 설치 잠금 재사용). 에이전트에서 부를 때 작업 폴더는 그 세션 작업공간이라 파일을 읽고 쓸 수 있다. 워크플로우에서는 런 폴더.
- 보안: 코드는 사용자가 쓴 것이라 승인 기본은 자동(A3). 모델이 주는 인자는 데이터일 뿐이다. DSL이나 파일로 가져온 도구 코드는 code 노드처럼 "검토 전 실행 금지" 배지를 단다. 한계는 code 노드와 같다(네트워크·파일 접근을 OS 수준에서 막지 못함).
- 크기: 마일스톤 1개(등록·편집 화면 포함). 3단계 관계: 없음. 추천: 한다. B2의 변환·차트 도구를 이것의 "예제 도구"로 배포하면 내장 도구를 하나씩 만들 필요가 줄어든다.

## 5. 묶음 B: 내장 도구 늘리기

### B1. 웹 검색 (D5)
- 도구 `web_search{query, max_results(≤10), recency?, include_content?}` → `results[{title, url, snippet, content?}]`. 에이전트 도구와 tool 노드 둘 다. 공급자 인터페이스 `SearchProvider`(tavily, brave, serper)와 설정에서 고른 기본 공급자, 키는 A2 자격 증명. 요청은 `HttpClient`로 나가고 공급자 호스트는 자동 허용. 호출 수와 비용을 사용량 화면에 "도구 호출" 축으로 더한다(토큰이 아니라 건수).
- 크기: 마일스톤 1개의 절반. 3단계 관계: `web_search → web_fetch → knowledge_index`가 3단계 지식 쓰기의 앞단이 된다(3단계 설계서 9장). 추천: 한다. 에이전트의 "조사" 능력이 이것 하나로 크게 는다.

### B2. 파일 변환, 차트·문서 생성
- 개인용에 쓸모 있는 것: 마크다운 → docx/pdf, csv ↔ xlsx, PDF 합치기·나누기·쪽 추출, 이미지 크기 조정·형식 변환(멀티모달 입력 한도 맞추기), 표 데이터 → 차트 PNG(matplotlib), 간단한 pptx.
- 방식: 서버 프로세스에 파서·렌더러를 넣지 않고(메모리 예산) A6의 사용자 파이썬 도구 형식으로 "예제 도구 묶음"을 배포한다. 패키지는 `python-docx`, `openpyxl`, `pypdf`, `Pillow`, `matplotlib`(numpy 포함 약 60MB 디스크), `python-pptx`이고 모두 샌드박스 프로세스에서만 import된다. 사용자가 설정 화면에서 "예제 도구 설치"를 누르면 레지스트리에 들어온다.
- 에이전트는 이미 `install_packages` + `run_python`으로 같은 일을 할 수 있다. 이 묶음의 가치는 tool 노드에서 승인 없이, 이름으로 부르는 데 있다.
- 크기: A6에 얹어 마일스톤 0.5개. 추천: A6 뒤에 한다.

### B3. Gemini 서버측 도구와 우리 도구 비교
| 항목 | Gemini 코드 실행 | 우리 run_python·code 노드 | Gemini URL context | 우리 web_fetch·http_request |
|---|---|---|---|---|
| 어디서 | Google 샌드박스, 30초, 라이브러리 37종 고정, 설치 불가 | 이 PC, 세션 작업공간 지속, 어떤 패키지든, 승인 모드 적용 | Google이 가져옴, 요청당 20 URL·34MB, PDF·이미지 가능, 유료 벽·YouTube 불가 | 우리가 가져옴, 허용 목록·사설 IP 차단, 5MB, HTML→글, PDF는 document_extractor |
| 경로 | 네이티브 전용(OpenAI 호환 불가) | 있음 | 네이티브 전용, 무료 | 있음 |
| 데이터 | 코드·입력이 Google로 감 | 로컬 | 가져온 내용이 입력 토큰 | 로컬 |
- 결론: 둘 다 지금은 넣지 않는다. 함수 호출(에이전트)과 섞을 수 없고 네이티브 경로가 필요하며, 같은 일을 로컬에서 더 통제된 방식으로 이미 한다. llm 노드에 "URL context 켜기" 옵션은 D1·D2 뒤 작은 후속으로 둘 수 있다(허용 목록 밖 페이지를 Google이 대신 읽는 용도).

## 6. 묶음 C: 멀티모달

각 기능은 "노드"(워크플로우), "도구"(에이전트가 부름), "입력"(사용자 첨부) 중 어디에 두는지와 결과 파일 위치를 적는다. 결과 파일 규칙은 C7.

### C1. 이미지·PDF·오디오·영상 이해 (입력)
- 지금: 에이전트는 사용자 첨부 이미지(png/jpeg/webp)를 `ImageBlock`으로, llm 노드는 `vision.var`의 이미지를 붙인다.
- 넓힐 것: `ImageBlock`을 `MediaBlock{media_type, data_b64|file_uri, filename}`으로 일반화(어댑터 한 곳). OpenAI 호환 경로로 PDF는 `image_url`에 `data:application/pdf`로 [실측, 3.6], 오디오는 `input_audio`(wav 확인, mp3·m4a·webm은 실측 2번)로 보낸다. 영상은 OpenAI 호환 경로에 없으므로 네이티브 `generateContent`로 보내는데, 이러면 그 호출은 채팅 어댑터 밖이다. 그래서 영상 이해는 llm 노드 옵션이 아니라 별도 노드·도구 `describe_video{file, prompt}`로 둔다(3.1의 "채팅은 OpenAI 호환 하나" 원칙 유지). PDF 쪽당 258토큰, 50MB·1,000쪽 [문서].
- 에이전트: 첨부 허용 mime을 이미지 + PDF + 오디오로 넓힌다(`services/runs.py:91`). 영상 첨부는 `describe_video` 도구가 처리.
- 챗플로우 웹앱: 첨부 칸의 허용 형식도 같이 넓힌다.
- 필요한 것: 무료. 의존성 없음. 크기: 마일스톤 1개의 절반. 3단계 관계: PDF를 지식으로 색인하는 것은 3단계(여기는 "이 PDF에 대해 물어보기"). 멀티모달 임베딩 검색도 3단계. 추천: 한다.

### C2. 받아쓰기
- 모델 `gemini-3.5-transcribe`(GA), 네이티브 `generateContent`에 인라인 wav 200, 결과는 `parts[].audioTranscription.text` [실측]. 한국어 `ko-KR`, 포맷 13종(WebM 포함), 1시간, 화자 분리·단어 타임스탬프(켜면 30분) [문서]. 무료.
- 두는 곳: 노드 `transcribe{file_var, language?, diarize?, timestamps?}` → `text`, `segments[]`; 에이전트 도구 `transcribe_audio{path}` → 글(작업공간 파일). 챗플로우·웹앱의 마이크 녹음 버튼(브라우저 `MediaRecorder`가 주는 webm/opus를 그대로 보냄)은 선택이며, 지원 형식에 WebM이 있어서 변환 없이 된다.
- 비용 참고(유료 등급 기준): 합산 약 분당 $0.005. 무료 등급 한도는 모른다.
- 크기: 노드·도구는 마일스톤 0.5개, 마이크 녹음 UI는 +0.5. 추천: 한다.

### C3. TTS
- 모델 `gemini-3.8-flash-tts`(GA), 네이티브 `generateContent` 200, 한국어 5.5초 WAV를 3.7초에 받음 [실측]. 음성 30개, 다화자 2명, 스타일 태그, 스트리밍은 raw PCM [문서]. 입력 길이 상한은 문서에 없다(실측 4번). 무료.
- 두는 곳: 노드 `text_to_speech{text, voice, style?, format: wav|mp3?}` → `files[audio]`; 에이전트 도구 `speak{text, voice}` → 작업공간에 파일을 쓰고 `file.changed`. 챗플로우 답을 자동으로 읽어 주는 "답 읽기" 옵션은 answer 뒤에 노드를 붙이면 되므로 따로 만들지 않는다.
- 출력은 WAV뿐이다. mp3가 필요하면 B2의 변환 도구(샌드박스)로.
- 크기: 마일스톤 0.5개(C2와 합쳐 1개). 추천: 한다. 4-3이 네이티브 클라이언트의 첫 소비자가 된다.

### C4. 이미지 생성·편집
- 모델 `gemini-nano-banana-2.1`(GA). OpenAI 호환 `images/generations`는 새 모델에 404, 네이티브 `generateContent`는 `/models`가 지원한다고 하지만 무료 등급은 한도 0으로 429다 [실측]. 결제 뒤 장당 1K $0.0336, 2K $0.0504 [문서]. 참조 이미지 최대 14장, 편집은 참조 이미지로(다중 턴 편집은 Interactions 전제라 안 쓴다).
- 두는 곳: 노드 `generate_image{prompt, references[], aspect_ratio, size}` → `files[image]`; 에이전트 도구 `generate_image`. 유료 키 전용(`requires_paid`).
- 크기: 마일스톤 0.5개. 추천: D1 (b)를 고른 경우에만, 4-3 뒤 작은 후속으로.

### C5. 음악·영상 생성
- Lyria 3.5 곡당 $0.08, Omni 1.1 Flash 영상 초당 약 $0.10, 둘 다 무료 없음, 영상은 영어만 완전 지원 [문서]. 결과가 크고(영상 4MB 넘으면 Files 폴링), 개인용 플랫폼에서 반복해 쓸 일이 적다.
- 추천: 뺀다. 나중에 원하면 C4와 같은 노드 모양으로 더할 수 있게 네이티브 클라이언트에 `response_modalities`만 열어 둔다.

### C6. Live 음성 대화
- `gemini-3.8-live`(GA), WebSocket, 16kHz PCM 입력·24kHz 출력, 세션 약 10분 뒤 GoAway와 재개 토큰, 에페메럴 토큰으로 브라우저가 직접 붙을 수 있음, 무료 등급 가능 [문서]. 실측은 안 했다(실측 10번).
- 선택지: (a) 서버 중계(브라우저 ↔ FastAPI WebSocket ↔ Gemini). `websockets`는 이미 있다. 오디오가 파이썬을 지나므로 메모리·지연이 늘고 도구 호출을 서버가 처리할 수 있다. (b) 브라우저 직결(서버는 에페메럴 토큰만 발급, 전사 결과만 서버로 보내 기록). 가볍지만 도구 호출은 못 한다(브라우저가 실행 주체가 되므로).
- 추천: 선택, 하려면 (b)로 "도구 없는 음성 대화"에 한정하고 에이전트 지시문·웹앱 설정만 잇는다. 크기: 마일스톤 1.5개(브라우저 오디오 캡처·재생, 토큰 발급, 전사 기록). 11/17 종료 대상은 프리뷰 모델이라 GA 모델을 쓰면 무관하다. 무료 등급 입력·응답은 약관상 제품 개선에 쓰인다는 점을 화면에 적는다.

### C7. 결과 파일과 화면
- 워크플로우: 노드가 런 폴더에 쓰고 `FilePort.save_output`으로 `wf_file(kind=output)`에 옮긴다(http_request 노드와 같은 길). 내려받기는 기존 `GET /wf-runs/{rid}/files/{fid}/download`와 웹앱 경로 그대로. 실행 패널·런 상세·웹앱 결과 카드에서 mime이 `audio/*`면 `<audio>` 재생기, `image/*`면 인라인 미리보기를 붙인다(우리 서버가 주는 같은 출처 파일만. 원격 이미지는 지금처럼 링크만 둔다, 1단계 M7 결정 유지).
- 에이전트: 도구가 작업공간에 파일을 쓰면 기존 `file.changed` → `session_file` → 내려받기 경로를 그대로 탄다. 채팅의 파일 칩에 같은 재생기·미리보기를 단다.
- 크기: 화면 0.5개. 추천: C2·C3과 같은 마일스톤.

## 7. 묶음 D: 관리형 에이전트 (D8)

| 기능 | 상태·경로 | 뺀 이유 | 대안 |
|---|---|---|---|
| Deep Research | 프리뷰, Interactions 전용, `background=true`(저장 전제), 완료까지 수 분, 검색 그라운딩 사용 | 무료 등급 가능 여부 불명, 저장 전제, 비용 상한을 우리가 못 건다 | 지휘자 에이전트(ADR-0006) + `web_search` + `web_fetch` + 결과 파일. 이미 있는 기계로 "조사 에이전트" 템플릿을 만든다 |
| computer use | 프리뷰, Interactions, 클라이언트가 Playwright 등으로 실행, 샌드박스 VM 권고 | 컨테이너 없는 이 PC에서 브라우저를 모델이 조작하는 위험 | 브라우저 자동화 MCP 서버(playwright-mcp 등)를 A4로 붙이고 A3 승인 등급으로 통제 |
| Antigravity | 프리뷰, Google의 Linux 샌드박스 | 코드와 파일이 Google 샌드박스에서 돌아 1단계 로컬 샌드박스와 따로 논다, 프리뷰 뒤 과금 미정 | 없음 |

## 8. 묶음 E: 그 밖의 후보

- 일정 트리거: 발행한 워크플로우를 cron 식으로 돌린다. 프로세스 안 asyncio 스케줄러(의존성 없음), 테이블 `wf_trigger(id, workspace_id, workflow_id, kind: schedule|webhook, spec, inputs, enabled, last_run_at, next_run_at)`, 런은 `trigger=schedule`로 기존 실행기에 넣는다. 서버가 꺼져 있던 시간의 회차는 건너뛰고 기록만 남긴다(PC라서). 크기: 마일스톤 0.5개. 추천: 한다.
- 웹훅 트리거: `POST /api/hooks/<token>`이 127.0.0.1에서만 열린다. 같은 PC의 스크립트·Windows 작업 스케줄러·다른 로컬 앱이 부르는 용도. 외부에서 받으려면 터널이 필요한데 범위 밖이다. 크기: 0.3개. 추천: 일정과 묶어 선택.
- 워크플로우를 도구로: 발행한 워크플로우를 레지스트리 `kind: workflow` 도구로 노출해 에이전트가 부른다. start 필드가 매개변수, end 출력이 결과. 이미 `/api/v1/workflows/run`과 blocking 모드가 있어 서비스 계층 호출로 작게 된다. 재귀(워크플로우 안 agent 노드가 다시 같은 워크플로우를 도구로)는 깊이 상한으로 막는다. 크기: 0.5개. 추천: A1 뒤 선택.
- 서브워크플로우 노드: 워크플로우 안에서 다른 워크플로우를 노드로. "워크플로우를 도구로"와 같은 서비스 호출이라 함께 하면 작다. 선택.
- 우리 앱을 MCP 서버로 노출: Claude Desktop 같은 로컬 클라이언트가 우리 에이전트·워크플로우를 도구로 부르는 쓰임이 있다. stdio 서버 하나를 `platform mcp-serve`로 띄우고 서비스 API 키로 인증. 크기: 0.5개. 추천: 사용자가 그런 클라이언트를 쓰면 선택.
- 다른 LLM 공급자: 어댑터가 OpenAI 호환 SDK + base_url이라 OpenRouter·다른 OpenAI 호환 엔드포인트를 더하는 기술적 비용은 작다(사고 서명·usage 보정이 Gemini 가정이라 공급자별 분기 필요). 그러나 ADR-0003·0004("Gemini만")를 바꾸는 결정이고, 로컬 모델은 이 PC 메모리(15.6GB, 확인 시점 여유 1.5GB)로는 어렵다. 추천: 지금은 안 한다. D1에서 결제를 안 하기로 하고 이미지 생성이 꼭 필요할 때만 다시 논의.
- Dify의 플러그인 마켓·트리거 플러그인·Datasource 플러그인: 만들지 않는다(1인용, Dify 패키지 비의존).
- Graph RAG(사용자 지시 2026-11-03 후보, 2026-11-04 안 D로 확정)
  - 무엇: LLM이 문서에서 개체(사람·조직·개념 등)와 관계를 뽑아 지식 그래프를 만들고, 검색할 때 그 관계를 따라간다. "A와 B는 어떤 관계인가"처럼 여러 문서에 흩어진 연결을 묻거나, 문서 묶음 전체의 주제를 요약하는 질문에 강하다. 3단계의 청크 모드(일반·부모-자식·Q&A)는 조각을 찾는 방식이라 이것을 대신하지 못한다.
  - 사용자 요청: 청크 모드의 한 선택지가 아니라 따로 만들 수 있는 기능이어야 한다. 원본(KB 문서나 파일)을 고르고, 뽑을 개체·관계 종류를 정해 그래프를 만들고, 결과를 보고 고치는(같은 개체 합치기, 잘못 뽑힌 관계 지우기) 별도 화면과 작업이 필요하다. 쓰는 쪽은 그래프 검색 노드나 에이전트 도구가 된다.
  - 미리 보이는 제약 [추정]: 그래프를 만들 때 청크마다 채팅 호출이 들어 무료 키의 일일 한도로는 작은 문서 묶음만 된다(D1과 엮인다). 저장은 ADR-0004대로 SQLite 표(개체·관계·요약)로 하고 그래프 DB 서버는 두지 않는다. 방식(커뮤니티 요약을 만드는 Microsoft GraphRAG식, 개체·관계 키워드로 찾는 LightRAG식 등)과 라이브러리를 쓸지는 4단계 논의에서 조사해 정한다.
  - 그래프 지도(사용자 요청 2026-11-03): 개체는 점, 관계는 선으로 그리고, 무리(커뮤니티)는 색, 중요도는 점 크기로 보이는 지도가 필요하다. 확대·이동이 되고, 점을 누르면 그 개체의 설명과 근거가 된 문서 조각을 본다. 사용자가 보여 준 예시는 인물 관계망 그림이고, 디자인은 그와 같지 않아도 된다.
  - 확정(2026-11-04): 안 D(단계판). 설계는 `docs/design/phase4-graphrag.md` v2, 결정은 ADR-0009다.
    - 1단계(4-G1a·b·c)는 LLM 호출 없이 형태소 분석기(Kiwi, 샌드박스)로 개념 그래프를 만들고, 지도·편집·local/hybrid 검색·노드·도구까지 낸다. 지도는 4-G1a부터 읽기 전용으로 보인다.
    - 2단계(4-G2)는 LLM 추출이다. 사용자 결정(2026-11-04)으로 무료 키로도 돌고, 유료 키는 빠르게 하려는 선택이다. 무료 키에서는 bulk 일일 몫(모델 한도 × 비율), 429 전에 스스로 멈추기, 대화형과 다른 모델로 몫 나누기, 리셋 뒤 자동 재개를 둔다. 위의 "미리 보이는 제약"은 이렇게 풀었다.
    - 3단계(4-G3)는 무리 보고서와 전역 검색이다.
  - 크기: 합계 5.5(4-G1a 1.25, 4-G1b 0.75, 4-G1c 1, 4-G2 1.5, 4-G3 1). 마일스톤과 완료 기준은 `phase4-graphrag.md` 13장에 있다.

## 9. 3단계와의 관계

3단계 설계서(`docs/design/phase3-rag.md`) v2와 맞춘 것이다. 3단계가 먼저 끝난다는 전제로 쓴다.

- 네이티브 Gemini 클라이언트는 4-1이 만들고 3단계는 만들지 않는다(3단계 Z8). 3단계 1차(`gemini-embedding-2`, OpenAI 호환 경로)는 네이티브가 없어도 돌고, 네이티브가 필요한 것은 `gemini-embedding-001`의 task type(OpenAI 호환 경로는 모르는 필드를 400으로 거부한다 [실측]), `countTokens`(임베딩 모델에서도 된다 [실측]), 멀티모달 임베딩뿐이다. 3단계 M21은 `EmbeddingTransport` 프로토콜과 OpenAI 호환 전송만 만들고, 4-1이 `gemini_native.py`에 `embed_content`·`count_tokens` 전송을 더해 그 프로토콜을 구현한다. 두 벌은 없다.
- 3단계 M21이 4단계 바탕에서 먼저 가져가는 것: 2절의 코드 영향 3건(폴백 기본값, `temperature`/`top_p` 게이트, 캐시 비율 0.10), `KeyPool.tier`와 `pick(model, tier=)`와 설정 `GEMINI_PAID_API_KEY`, `_classify`의 `app/llm/errors.classify` 공용화, `EmbeddingTransport` 프로토콜. 4-1은 그 위에 얹는다.
- 4단계를 먼저 하는 경우(3단계 Z8 (b)·(c)): 위 넷을 4-1이 대신 하고 M21은 그것을 전제로 줄어든다.
- File Search(관리형 RAG)는 3단계가 쓰지 않는다(Z3). 검색 서비스에 공급자 경계(`KnowledgeProvider`)만 남겼고 Interactions 전송은 어느 쪽도 만들지 않는다.
- 3단계가 1단계 내장 도구로 먼저 만드는 `search_knowledge`, `describe_sql_source`, `query_sql`은 4-2에서 레지스트리 `kind: knowledge|sql`(risk read)로 옮긴다(A1·A3). 외부 DB 데이터 소스는 A2의 `credential(kind: dsn)`과 `SecretRegistry`가 들어온 뒤의 3단계 후속이다.
- 멀티모달 검색(이미지·PDF·오디오 임베딩), 지식 파이프라인의 PDF 파싱과 스캔 PDF 모델 읽기, Text2SQL은 3단계에서 다룬다. 4단계 C1은 "첨부 파일에 대해 바로 묻기"까지만이고, 3단계의 이미지 임베딩은 C1의 `MediaBlock` 뒤에 얹는다(Z9).
- `web_search`(B1)와 `web_fetch`는 3단계 `knowledge_index` 노드의 앞단이 된다(3단계 9장).
- 사용량: 임베딩 호출은 OpenAI 호환 응답에 `usage`가 없어 `usage` 이벤트 없이 추정 토큰(`estimated=true`)을 KB 통계(`knowledge_base.embedded_tokens_estimated`)에만 더한다(3단계 6.6). 4단계 3.2의 `media_out`은 오디오·이미지 출력 전용이라 자리를 다투지 않는다.

## 10. 추천 순서와 마일스톤 묶음 초안

번호는 3단계가 M21~M29를 쓰므로 "4-n"으로 둔다. 3단계가 먼저 끝난 뒤 시작한다는 전제다(3단계 설계서 Z8). 각 마일스톤은 지금처럼 implementer 1명 단위이고 pytest·ruff·pyright·vitest·e2e 전체 통과가 끝 조건이다. 병렬은 PC 메모리 때문에 최대 2개.

| 마일스톤 | 내용 | 크기 | 전제 |
|---|---|---|---|
| 4-1 네이티브 클라이언트 | `gemini_native.py`(generateContent·스트리밍·Files API)와 M21의 `EmbeddingTransport` 네이티브 구현(`embed_content`·`count_tokens`), 능력표 `requires_paid`, 사용량 `media_out`과 가격표 확장, live 테스트 2건(TTS·받아쓰기 200) | 1 | 3단계 M21(`KeyPool.tier`, `errors.classify`, `EmbeddingTransport`, 2절 3건) |
| 4-2 도구 바탕 + 웹 검색 | A1 레지스트리와 두 어댑터, 기존 워크플로우 내장 4종 이전, 3단계 내장 도구 셋(`search_knowledge`, `describe_sql_source`, `query_sql`)을 `kind: knowledge|sql`로 이전, A2 자격 증명 테이블·화면·`SecretRegistry`, A3 승인 등급, B1 `web_search`(Tavily 기본, 공급자 인터페이스), 에이전트 빌더 "도구" 탭과 tool 노드 폼 확장 | 1.5 | 3단계 M24·M27(옮길 도구가 있어야 한다. 없으면 그 항목만 뒤로) |
| 4-3 멀티모달 기본 | C1 `MediaBlock`과 PDF·오디오 입력, C2 받아쓰기 노드·도구, C3 TTS 노드·도구, C7 재생기·미리보기, `describe_video` 노드·도구, memcheck에 오디오 구간 추가 | 1.5 | 4-1 |
| 4-4 MCP 클라이언트 | A4 전부. 서버 등록·연결 시험·도구 선택 화면, Job Object·유휴 종료·동시 2, 승인 정책, 결과 파일 처리, Windows 수동 점검표(npx 서버 1개, 파이썬 서버 1개, HTTP 서버 1개) | 1.5 | 4-2 |
| 4-5 사용자 파이썬 도구와 예제 묶음 | A6 등록·편집·실행, 패키지 캐시, B2 예제 도구(차트, csv↔xlsx, md→docx, PDF 쪽 추출, 이미지 변환), 선택으로 A5 OpenAPI | 1 (+0.5) | 4-2 |
| 4-6 트리거와 워크플로우 도구 (선택) | 일정 트리거, 웹훅 트리거, 워크플로우를 도구로·서브워크플로우 노드 | 1 | 4-2(도구 부분만) |
| 4-7 유료·선택 기능 (선택) | C4 이미지 생성(결제 뒤), llm 노드의 Google 그라운딩·URL context 옵션, C6 Live 음성 대화(브라우저 직결) | 0.5 + 0.5 + 1.5 | D1 (b), 4-1, 4-3 |
| 4-8 하드닝·문서 | 보안 확인표 X1~Xn(MCP 서버 격리, 자격 증명 마스킹, 승인 등급, 파일 미리보기 XSS), memcheck 갱신, 설명서·운영 문서, progress 표 | 0.5 | 전부 |

- 순서: 4-1 → 4-2 ∥ 4-3 → 4-4 ∥ 4-5 → 4-6 ∥ 4-7 → 4-8. 핵심은 4-1~4-5(약 6.5)이고 4-6·4-7은 사용자가 고른다.
- Graph RAG는 위 표와 따로 4-G1a·4-G1b·4-G1c·4-G2·4-G3(합계 5.5)로 둔다(`phase4-graphrag.md` 13장). 4-G1은 4-1·4-2에 기대지 않아 4-1보다 앞에 둘 수 있고, 제안 순서는 3단계 → 4-G1a → 4-G1b → 4-G1c → 4-1 → 4-2 ∥ 4-G2 → 4-3 ∥ 4-4 → 4-5 → 4-G3 ∥ 4-6·4-7 → 4-8이다. 순서는 4단계를 시작할 때 사용자와 정한다.
- 3단계(M21~M29)가 먼저 끝난 뒤 4-1부터 시작한다(3단계 설계서 Z8 (a)). 4단계를 먼저 하기로 바뀌면(Z8 (b)·(c)) 2절 3건과 `KeyPool.tier`·`errors.classify`·`EmbeddingTransport`를 4-1이 맡고 M21이 그만큼 줄어든다.

## 11. 실측 필요 (실제 키로 확인할 것, 비용 적은 순)

1. 무료 등급 한도: AI Studio에서 3.8 Flash·TTS·Transcribe 모델별 RPM/RPD와 키 8개의 프로젝트 수. 임베딩 분당 토큰 한도는 1,004토큰 청크 95개(약 9만5천 토큰)를 한 요청으로 보내 200을 받았으므로 그 이상이고, 429 본문의 `quotaId`(예: `...PerMinutePerUserPerProjectPerModel-FreeTier`)와 `retryDelay`로 분당·일일 한도를 가를 수 있다 [실측]. 임베딩 일일 한도는 프로젝트당 하루 1,000건이고 식별자는 `...PerDayPerUserPerProjectPerModel-FreeTier`다 [실측]. 남은 것은 3.8 Flash·TTS·받아쓰기의 분당·일일 한도다. 키 운용(D1)의 전제다.
2. OpenAI 호환 chat에서 PDF `image_url` 데이터 URL이 3.8에서도 되는지(3.6만 실측), `input_audio`에 mp3·m4a·webm·ogg, 20MB 근처 크기.
3. 네이티브 `streamGenerateContent?alt=sse`의 청크 형식과 `usageMetadata`(thoughtsTokenCount 포함) 위치.
4. TTS: 3,000자·20,000자 입력의 상한과 오디오 길이, 스트리밍 PCM 조각 크기, Lite 모델 한국어 품질, 다화자 2명.
5. 받아쓰기: webm/opus 인라인, 30분·1시간 파일의 Files API 재개 업로드 시간, 화자 분리 한국어, 단어 타임스탬프 응답 모양.
6. Files API: 무료 키 업로드 한도와 48시간 삭제, 영상 업로드 뒤 `generateContent` 이해 1건(짧은 mp4).
7. `mcp` 2.3.0: `uv add` 설치 크기, import 뒤 RSS 증가, Windows에서 `npx.cmd` stdio 서버와 파이썬 stdio 서버 연결, 외부에서 띄운 프로세스를 넘길 수 있는지, Streamable HTTP 헤더 인증.
8. Tavily·Brave·Serper 공식 가격 페이지와 응답 스키마, 한국어 질의 품질 비교(질의 10개).
9. `temperature`/`top_p`를 넣은 3.8 요청의 출력이 값에 따라 달라지는지(무시 확인), `top_k`.
10. Live: `gemini-3.8-live` 무료 키에서 에페메럴 토큰 발급 가능 여부, 한국어 짧은 세션, GoAway 시점, 동시 세션 수(C6를 고를 때만).
11. 이미지 생성: 결제 연결 뒤 네이티브 `generateContent` + `responseModalities: ["IMAGE"]`로 nano-banana 2.1 1장, 비용 확인(D1 (b)일 때만).
12. 검색 그라운딩: 결제 연결 뒤 네이티브에서 함수 호출과 함께 1건(4-7을 고를 때만).
13. Deep Research가 무료 키에서 되는지는 D8대로 뺐으므로 확인하지 않는다. 사용자가 되살리면 비용 상한을 두고 1건.
14. 4-3 뒤 memcheck: TTS 1분·받아쓰기 10분 파일 구간의 최대 RSS.

## 12. 리스크와 가정

- 가정: 사용자 지시(2026-10-14)대로 3·4단계는 사용자와 논의해 범위를 정하기 전에는 시작하지 않는다. 이 문서의 추천은 논의의 출발점이다. D1은 결제라서 사용자만 정할 수 있다.
- Gemini 문서가 빨리 바뀌고 서로 모순된다(Interactions GA/Beta, 원격 MCP 지원). 설계는 실측을 우선하고, 전송을 분리해 둔 것이 완화책이다.
- 무료 등급 과부하: 2026-10-30에 3.8이 79초·503이었다. 4단계 기능이 늘어도 체인과 키 순환은 같고, 느리면 3.6을 기본으로 바꾸는 설정 변경으로 대응한다.
- MCP 서버 프로세스의 메모리(Node 서버당 수십~수백 MB)와 제3자 코드 실행. 동시 2개·Job Object·최소 환경변수·승인 등급으로 묶지만 컨테이너 수준은 아니다. 1단계 3.11의 한계가 그대로 적용된다.
- 자격 증명이 늘어난다(검색 키, MCP 토큰, OpenAPI 키). 런타임 비밀값 마스킹(`SecretRegistry`)을 4-2에서 먼저 넣지 않으면 로그에 샐 수 있다.
- 도구 설명을 통한 프롬프트 인젝션(MCP tool poisoning). 등록 때 스냅샷과 변경 경고, 승인 카드에 설명 표시로 완화하고 자동으로 막지는 못한다.
- 결과 파일 미리보기(이미지·오디오 인라인)는 XSS 면적이 는다. 같은 출처 파일만, `Content-Disposition: attachment` 대신 inline으로 줄 때 mime 허용 목록(png/jpeg/webp/wav/mp3)과 `nosniff`를 지킨다.
- 네이티브 클라이언트가 두 벌 생길 위험은 3단계 Z8(3단계는 만들지 않음)과 `EmbeddingTransport` 프로토콜로 닫았다. 남은 위험은 M21이 만드는 프로토콜 모양과 4-1의 전송이 어긋나는 것이라 4-1 첫날 M21의 `app/llm/embeddings.py`를 읽고 시작한다.
- 4단계의 네이티브 호출도 429 키 순환 결정(3단계 설계서 Z2)을 그대로 따른다. (a)면 지금 코드 그대로, (b)면 일일 한도 429에서 순환을 멈추는 ADR-0003 한 줄 개정, (c)면 429마다 기다리는 ADR-0003 개정이 따라온다. 어느 쪽이든 3.2의 오류 분류 한 곳만 바뀐다.
- 약관 변화: 무료 등급의 데이터 사용 조건과 한도 정책이 바뀌면 D1을 다시 본다.
