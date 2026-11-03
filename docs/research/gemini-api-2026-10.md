# Gemini API 조사: 임베딩, 관리형 RAG, 4단계 기능, MCP

조사 기간: 2026-10-30 ~ 11-02. 목적: 3·4단계 설계의 근거 자료. 이 PC: Windows 11, Python 3.12.8, SQLite 3.45.3. 근거는 ai.google.dev 공식 문서(2026-10-30 기준)와 이 저장소의 키로 직접 호출한 결과다. 문서가 빨리 바뀐다. 임베딩 문서만 해도 2026-10-29에 갱신됐다. 쪽별 갱신일은 8절에 있다.

표기: 무표기는 공식 문서에서 확인한 내용이다. [실측]은 2026-10-30에 이 저장소의 키 8개로 직접 호출해 확인한 결과다. 키가 모두 무료 등급이라 유료 전용 기능은 호출하지 못했다. [간접]은 포럼이나 제3자 자료, [추정]은 문서 근거 없이 추론한 것, [미확인]은 문서에 없고 호출로도 확인하지 못한 것, [내 계산]은 직접 계산한 값, [일반 지식]은 출처를 확인하지 못한 일반 상식이다. 문서에 "없음"이라고 적은 곳은 읽은 쪽 기준이다. 본문이 잘려 읽힌 쪽이 있어서 실제로는 있을 수 있다.

## 결론

- 3단계 임베딩은 `gemini-embedding-2`(GA, 2026-04-22)가 기본 후보다. task_type 대신 접두어를 붙이는 모델이라 OpenAI 호환 경로로 충분하다. task_type이 필요한 `gemini-embedding-001`은 호환 경로가 task_type을 400으로 거부해서 [실측] 네이티브 경로가 필요하다.
- 관리형 RAG(File Search)는 있다. 다만 문서 예시가 전부 Interactions API 기준이고 검색 제어와 청크 원문 반환이 없다. 직접 만들지는 검색 제어, 출처 표시, 데이터 외부 저장, 한도가 쟁점이다(2절).
- 4단계 기능은 거의 전부 OpenAI 호환 밖이다(네이티브 Interactions API 또는 WebSocket). 호출로 확인한 예외는 chat completions에 넣는 오디오·PDF 입력과 암묵적 캐시다 [실측].

## 설계에 영향이 큰 사실

1. task_type과 접두어
   - `gemini-embedding-2`는 task_type 필드를 쓸 수 없다("You cannot use the `task_type` field for the `gemini-embedding-2` model"). 대신 입력 앞에 접두어를 붙인다. 질의는 `task: search result | query: {content}`, 문서는 `title: {title} | text: {content}`(제목이 없으면 `title: none`)다. 접두어를 붙이면 벡터가 꽤 달라져서, 접두어 없는 같은 문장과의 코사인이 질의 접두어 0.85, 문서 접두어 0.80이다 [실측]. 한국어 검색에서는 붙이는 편이 나았다(`docs/research/rag-text2sql-2026-11.md` 3.5절).
   - `gemini-embedding-001`의 task_type(8종)은 네이티브 `embedContent`의 `taskType`에만 문서화돼 있다. 네이티브에서 taskType을 생략하면 `RETRIEVAL_QUERY`와 같은 벡터(코사인 1.0)가 나오고 `RETRIEVAL_DOCUMENT`와는 0.83이다 [실측].
   - OpenAI 호환 문서(2026-10-28)에는 `task_type`과 `dimensions`라는 문자열이 아예 없다. 호출하면 `dimensions`는 동작하고 `task_type`, `taskType`, `google`, `extra_body`는 모두 400 "Unknown name"이다 [실측]. 이 경로로는 task_type을 줄 수 없어서 001이 문서도 질의처럼 임베딩한다. 포럼 답변(2025-05-22)도 "task_type을 쓰려면 네이티브 Gemini API로 바꿔라"였다 [간접].
2. 모델 간 비호환과 입력 상한
   - 001과 2는 벡터 공간이 호환되지 않는다("you must re-embed all of your existing data"). 모델, 차원, 접두어 규칙은 인덱스 메타데이터로 두고, 모델을 바꾸면 전량 재임베딩한다.
   - 입력 상한을 넘으면 오류 없이 잘린다. 2는 문서에 "silently truncates"로 적혀 있고 001도 같다. 001에 3,913토큰, 2에 약 1만3천 토큰을 넣고 꼬리만 바꿔도 벡터가 같았다(코사인 1.0) [실측]. 청크 크기 검증은 우리 쪽에서 한다.
3. 여러 입력과 배치
   - 네이티브에서 2는 `contents`에 여러 입력을 직접 넣으면 집계된 임베딩 하나를 돌려준다. 항목별 벡터를 받으려면 각 입력을 `Content` 객체로 감싼다.
   - OpenAI 호환 `input=[...]`은 항목별 벡터를 돌려준다(3개를 넣으면 3개) [실측]. 한 요청에 최대 100개이고 101개부터 400 `at most 100 requests can be in one batch`다. 네이티브 `batchEmbedContents`도 같다 [실측]. 짧은 문장 100개 배치가 2.2~3.0초 걸렸다 [실측].
   - 같은 입력은 단건이든 배치든 같은 벡터다(코사인 1.0). 내용 해시로 임베딩을 캐시해도 된다 [실측].
4. usage와 토큰 수
   - OpenAI 호환 응답에는 `usage` 키 자체가 없다. 네이티브 `embedContent`는 2에서만 `usageMetadata.promptTokenCount`를 주고 001은 None이다 [실측]. `batchEmbedContents`도 API 레퍼런스(2026-10-26)에는 같은 필드가 있으나 채워지는지는 [미확인]이다.
   - `countTokens` 문서(2026-10-13, API 레퍼런스 2026-08-17)는 생성 모델 예시뿐인데 임베딩 모델에서도 호출된다. 한국어 730자가 임베딩 토크나이저로 521토큰(약 1.4자/토큰), `gemini-3.8-flash` 토크나이저로는 426토큰이다. 8,192토큰은 한국어 약 1만1천 자, 2,048토큰은 약 2,800자다 [실측].
5. API 지형
   - Interactions API(`POST /v1beta/interactions`)가 새 표준이다. 문서는 "recommended for all new projects"라고 쓰고, 새 모델과 도구와 에이전트가 "launch exclusively on the Interactions API"라고 한다. 이 저장소의 키로 호출하면 200이고 `store: false`도 된다. 응답에 `service_tier: standard`와 `usage`(thought 토큰 포함)가 온다 [실측].
   - 서버에 상호작용을 저장한다(`store=true` 기본, 보존은 무료 1일, 유료 55일). `store=false`면 background와 `previous_interaction_id`를 쓸 수 없고, Deep Research는 `background=true`가 필수라 저장이 전제다.
   - OpenAI 호환은 "still in beta"로 chat, embeddings, images.generate(문서상 구형 모델 2종), models, 부분 batch만 다룬다. 호출해 보면 `/audio/speech`, `/audio/transcriptions`, 새 이미지 모델의 `images/generations`는 404, chat의 `google_search` 도구는 400이다 [실측]. Live, computer use, Deep Research, Omni, Lyria, URL context, 코드 실행, File Search는 호환 문서에 언급이 없고 미지원이라고 단정하지 못한다.
6. 무료 티어와 한도
   - 무료가 없는 것: 이미지 생성, Omni, Lyria, computer use, Gemini 4 Argon, Batch, Flex. Gemini 3 이상의 Google 검색 그라운딩도 무료는 "Not available"이다. 무료에서 쓸 수 있는 것: 3.8 Flash, 임베딩 2종, 3.8 TTS, Transcribe, Live, URL context, File Search(저장 1GB), 3.8 Flash 캐시 입력.
   - 이 저장소의 키 8개는 모두 무료 등급이다. `gemini-4-argon`은 8개 키 모두 429(오류 문구에 `free_tier`)였고, 이미지 생성(`generate_content_free_tier_requests, limit: 0`)과 Google 검색 그라운딩도 무료 키로 막혔다. 이미지 생성은 결제 연결이 있어야 한다. TTS와 전사는 무료 키로 200이었다 [실측].
   - 약관(2026-04-28 갱신)상 무료 서비스는 프롬프트와 응답이 제품 개선에 쓰이고 사람 검토자가 읽을 수 있다("Do not submit sensitive, confidential, or personal information to the Unpaid Services."). 가격표의 "Used to improve our products"는 무료 Yes, 유료 No다. 유료 등급은 제품 개선에 쓰지 않고 위반 탐지용으로만 한정 기간 기록한다. 한국은 EEA, 스위스, 영국이 아니라서 이 무료 조건이 그대로 적용된다고 읽힌다 [추정].
   - 문서는 "Rate limits are applied per project, not per API key"라고 하고 일일 한도는 태평양 시간 자정에 리셋된다. 키 8개가 한 프로젝트에 속하면 순환해도 한도를 같이 쓰게 되는데, 실제로는 키마다 따로 분당 한도를 가졌다. 27초 안에 키마다 101건씩(총 808건) 보냈는데 429가 없었다 [실측]. 키들이 서로 다른 프로젝트에 속한 것으로 보인다 [추정]. 일일 한도는 키 하나로 소진 시험을 해 프로젝트당 하루 1,000건임을 확인했다(아래 한도 항목) [실측].
   - 무료 `gemini-embedding-2`는 분당 100건이고 배치 항목 하나를 1건으로 센다(`embed_content_free_tier_requests, limit: 100, model: gemini-embedding-2, description: Embed content requests per minute limit exceeded on Free Tier`) [실측]. 임베딩 모델별 RPM/TPM/RPD는 문서에 없고 AI Studio에서만 보인다.
   - Google API 약관 2(d)(2021-11-09 개정)에 "You agree to, and will not attempt to circumvent, such limitations documented with each API."가 있다. 여러 프로젝트로 한도를 늘리는 행위를 직접 언급하지는 않는다.
7. 모델 수명과 가격
   - `gemini-3.7-flash`는 `gemini-3.8-flash`로, `gemini-3.5-flash`는 `gemini-3.6-flash`로 자동 라우팅된다(2026-10-08 공지, 종료는 아님). 이 저장소의 폴백 체인과의 관계는 5절에 있다.
   - `temperature`, `top_p`, `top_k`는 3.6 Flash부터 deprecated이고 무시된다. 이후 세대는 HTTP 400이다. OpenAI 호환 chat에 `temperature`/`top_p`를 넣은 요청은 3.8, 3.7, 3.6 모두 200이었다 [실측].
   - 3.8 Flash 유료 단가는 2027-01-01에 2배가 된다(입력 $0.75→$1.50, 출력 $3.75→$7.50). 임베딩 가격표에는 인상 표기가 없다. `gemini-4-argon`은 10/13 GA, 무료 티어가 없고 $2/$10→2026-12-01부터 $4/$20이다.
   - 2026-11-17에 종료 예정인 TTS·Live 프리뷰 모델(3-1, 3-4절 표)은 문서가 "earliest possible dates"라고 한 가장 이른 시점이다.

## 1. 임베딩

### 1-1. 모델 비교
(출처: embeddings, 레퍼런스 api/embeddings, changelog, deprecations, pricing)

| 항목 | gemini-embedding-001 | gemini-embedding-2 |
|---|---|---|
| 상태 | Stable(최신 업데이트 2025-06). 종료 가능 시점 2028-05-14 이후 | Stable/GA. changelog 2026-04-22 "Released as GA", preview 출시는 2026-03-10 |
| 입력 | 텍스트 | 텍스트, 이미지, 영상, 오디오, PDF(하나의 공간) |
| 입력 토큰 상한 | 2,048 | 8,192(모든 모달 합산) |
| 멀티모달 한도 | - | 이미지 ≤6장(PNG/JPEG), 오디오 ≤180초(MP3/WAV), 영상 ≤120초(MP4/MOV, 최대 32프레임, 오디오 트랙 미처리), PDF 1개·≤6쪽(쪽당 258토큰, 1쪽 권장). 인라인 데이터 또는 Files API로 전달 |
| 차원 | 기본 3072, 128–3072 가능. 권장 768, 1536, 3072 | 동일 |
| 정규화 | 3072만 정규화됨. 그 외는 직접 L2 정규화 필수(문서 코드 `v / np.linalg.norm(v)`). 768은 노름 약 0.59, 1536은 약 0.70 [실측] | 3072 외 차원도 자동 재정규화. 모든 차원에서 노름 1.0 [실측] |
| 차원 자르기 | 768차원 벡터는 3072차원 벡터의 앞 768개와 방향이 같다(코사인 1.0) [실측] | 동일 [실측] |
| task_type | 네이티브 `taskType` 8종: SEMANTIC_SIMILARITY, CLASSIFICATION, CLUSTERING, RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY, CODE_RETRIEVAL_QUERY, QUESTION_ANSWERING, FACT_VERIFICATION. 질의에는 RETRIEVAL_QUERY, 문서에는 RETRIEVAL_DOCUMENT | 사용 불가. 접두어 사용: 질의 `task: search result \| query: {content}`, 질의응답 `task: question answering \| query: ...`, 문서 `title: {title} \| text: {content}`, 분류·클러스터링·유사도는 `task: classification \| query: ...` 식 |
| 접두어 주의 | - | 질의와 문서 형식을 일관되게 쓴다. `task: sentence similarity`는 검색에 쓰지 않는다. 멀티모달 입력의 텍스트 부분에는 접두어 비권장 |
| 다국어 | 언급 없음 | "over 100 languages". 한국어 개별 언급이나 점수 없음(Google 블로그 2026-03-10도 동일) |
| 요금(/1M, 유료) | $0.15, Batch $0.075 | 텍스트 $0.20(Batch $0.10). 이미지 $0.45(장당 $0.00012), 오디오 $6.50(초당 $0.00016), 영상 $12.00(프레임당 $0.00079). Batch는 절반 |
| 무료 티어 | 무료. 데이터가 제품 개선에 쓰임 | 동일 |

`gemini-embedding-2-preview`는 임베딩 문서에 항목이 없다. deprecations 표에는 `embedding-2-preview` 행(행 이름에 "gemini-"가 없음)이 종료 2026-08-10, 대체 `gemini-embedding-2`로 적혀 있다. 그런데 이 저장소의 키로 본 `/models`에는 `gemini-embedding-2-preview`가 남아 있고 `gemini-embedding-2`와 같은 벡터(코사인 1.0)를 낸다. 별칭이다 [실측].

한국어 품질은 공식 문서에 수치가 없다. 001 논문(arXiv 2503.07891, 2025-03)의 초록은 MMTEB(250개 이상 언어 범위)에서 최고 수준이라고 주장하지만 한국어 점수는 없고, 001 기준이라 2 모델 자체의 수치도 아니다. 한국어 합성 평가셋으로 두 모델을 직접 쟀다(`docs/research/rag-text2sql-2026-11.md` 3.5절).

### 1-2. 호출 경로

task_type, 차원, 여러 입력, usage의 경로별 차이는 위 사실 1~4번에 있다. 경로와 엔드포인트는 다음과 같다.

- OpenAI 호환: `POST /v1beta/openai/embeddings`(`client.embeddings.create`). 멀티모달은 문서 예시가 텍스트뿐이다 [미확인].
- 네이티브 embedContent: `POST /v1beta/models/{model}:embedContent`. 001의 task_type은 `taskType`(요청 본문 직속 필드는 deprecated, `embedContentConfig.taskType` 사용 안내)이고 차원은 `output_dimensionality`다. 멀티모달은 `parts[].inline_data` 또는 Files API로 보낸다.
- 네이티브 batchEmbedContents: `POST /v1beta/models/{model}:batchEmbedContents`(`requests[]`). task_type과 차원은 요청별이고 멀티모달도 된다(문서 예시에 inline 이미지). 요청당 최대 100개, 101개는 400이다 [실측].
- batchEmbedContents의 100개 상한은 문서에 없다. LangChain 소스 docstring("max batch size of 100 strings")과 Vercel AI SDK PR #16575(2026-07-02, 상한을 2048에서 100으로 내림, 초과 시 400)도 같은 값이다 [간접].
- Batch API: SDK `client.batches.create_embeddings(model=, src={'file_name':...} 또는 {'inlined_requests':...})`, REST `asyncBatchEmbedContent`. 요청별로 task_type과 차원을 주고, 인라인은 총 요청 20MB 미만, 입력 파일은 2GB 이하다. 목표 소요 24시간, 표준 대비 50%이며 유료 전용이다(가격표에서 무료 칸이 "Not available"). 동시 배치 요청 100, 대기열 토큰은 Gemini Embedding 기준 Tier1 500,000 / Tier2 5,000,000 / Tier3 10,000,000이다. 상태값은 PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED, EXPIRED이고 48시간 넘게 실행·대기하면 EXPIRED이며, 완료 알림은 webhook으로 받을 수 있다(changelog 2026-05-04).
- OpenAI 호환 batch는 JSONL에 `url:"/v1/embeddings"`, 생성 때 `endpoint="/v1/embeddings"`, `completion_window="24h"`를 쓴다. 파일 업로드·다운로드가 호환되지 않아서 genai 클라이언트가 필요하고, 문서 예시에는 오타와 누락 변수가 있다.

### 1-3. 한도, 토큰 계산, 저장 크기

- 한도: 무료 `gemini-embedding-2`는 분당 100건이다(위 6번). 과거 문서에 Gemini Embedding 무료 100 RPM / 30,000 TPM / 1,000 RPD로 보이는 행이 있었다는 검색 결과가 있다. RPM 100은 실측과 같다. TPM 3만은 지금 걸리지 않는다. 1,004토큰짜리 청크 95개(약 9만5천 토큰)를 한 요청으로 보내 200을 받았다(2.8초) [실측, 2026-11-02]. RPD 1,000도 지금 그대로다. 키 하나로 95건씩 분당 한 번 보내자 950건 뒤 다음 묶음에서 429가 났고, 본문은 `EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier`, `quotaValue` "1000", `retryDelay` 약 4.8시간(태평양 자정까지)이었다 [실측, 2026-11-02]. 429 본문의 `QuotaFailure.violations[]`에는 `quotaId`(예: `EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier`), `quotaValue`("100"), `quotaDimensions`(model)가 오고 `RetryInfo.retryDelay`(예: "45s")가 같이 온다 [실측]. 분당 한도와 일일 한도는 이 식별자로 가를 수 있다. 등급 기준은 Free(활성 프로젝트), Tier 1(결제 계정 연결), Tier 2(누적 $100 + 첫 결제 후 3일), Tier 3(누적 $1,000 + 첫 결제 후 30일)이다.
- 토큰 수 세기: 공식 `countTokens`(`POST /v1beta/models/{model}:countTokens`, Python `client.models.count_tokens`)는 입력만 센다. 임베딩 모델에서도 된다(위 4번). 문서가 다루는 환산 규칙은 이미지 ≤384×384면 258토큰, 큰 이미지는 768×768 타일당 258토큰, 오디오는 초당 32토큰(Transcribe/TTS 가격표는 25토큰/초 기준)이고 영상은 모드에 따라 다르다.
- 벡터 저장 크기 [내 계산]: 768차원 float32는 벡터당 3,072바이트라 10만 청크가 약 307MB다. 1536차원은 약 614MB, 3072차원은 약 1.2GB다(메모리 상주 시). 서버 RSS 300MB 예산과 직결되는 숫자다.

## 2. 관리형 RAG: File Search 대 로컬 RAG
(출처: file-search 2026-10-13, pricing, changelog, 약관)

| 항목 | File Search(문서) | 로컬 RAG(직접 구현) |
|---|---|---|
| 호출 경로 | Interactions API의 도구 `{"type":"file_search","file_search_store_names":[...],"metadata_filter":"..."}`. 스토어 관리는 `POST /v1beta/fileSearchStores` 등. 기능 자체의 GA/Preview 표기는 없고 OpenAI 호환 언급도 없음 | 임베딩만 API, 검색은 로컬 |
| 색인 흐름 | 스토어 생성 → 직접 업로드(`uploadToFileSearchStore`) 또는 Files API 업로드 후 `importFile` → 비동기 operation 폴링 → 자동 청크·임베딩·인덱싱 | 직접 구현 |
| 청크 | `chunking_config.white_space_config{max_tokens_per_chunk, max_overlap_tokens}`. 예시 200/20, 기본값 비공개 | 완전 제어 |
| 임베딩 모델 | 스토어 생성 시 `embedding_model` 지정. 이미지 검색은 `models/gemini-embedding-2` 지정 필요. 텍스트 기본은 gemini-embedding-001로 읽히나 기본값 명시는 없음 | 자유 |
| 검색 제어 | top_k, 임계값, 재순위, 하이브리드 없음. `metadata_filter`(AIP-160 문법)와 `custom_metadata`(string/numeric)만 | 자유 |
| 결과 | 응답의 `annotations[]`, type `file_citation`: `file_name`, `source`, `page_number`(PDF), `media_id`(이미지 청크), `custom_metadata`. `file_search_call/result` 단계 없음, 청크 원문 반환 없음 | 청크 원문, 점수 전부 확보 |
| 지원 형식 | `application/pdf`, JSON, Office, 코드 등 MIME 다수(text/* 150종 이상, application/* 약 30종), 이미지 PNG/JPEG(4K×4K 이하). 오디오·영상 미지원 | 파서 직접 |
| 한도 | 문서당 100MB. 프로젝트 합계 Free 1GB / Tier1 10GB / Tier2 100GB / Tier3 1TB. 스토어당 20GB 미만 권장. 용량은 입력+임베딩이라 보통 입력의 약 3배. 문서 개수 상한은 문서에 없음 | 디스크 |
| 요금 | 저장 무료. 색인은 임베딩 요금(001이면 $0.15/1M, 아니면 Embedding 2 요금), 조회 시 질의 임베딩 무료, 검색된 문서 토큰은 일반 입력 토큰. 무료 티어 무료 | 임베딩 비용+로컬 리소스 |
| 데이터 | 스토어 데이터는 삭제 전까지 무기한. 임베딩 TTL 없음(수동 삭제 또는 모델 deprecated까지). Files API 원본은 48시간 후 삭제. 저장 리전과 학습 사용 여부는 File Search 문서에 없음 [미확인]. 무료 티어면 약관상 제품 개선 사용 대상으로 읽힘 [추정] | 로컬 |
| 갱신·삭제 | 문서 list/get/delete만. 덮어쓰기나 중복 처리 문서 없음 | 자유 |
| 도구 조합 | Google Search, URL context와 같은 요청에서 동시 사용 불가. Gemini 4/3에서는 function calling 및 구조화 출력과 조합 가능. Live API 미지원 | 해당 없음 |
| 지원 모델 | Gemini 4 Argon, 3.8 Flash, 3.6 Flash, 3.5 Flash-Lite, 3.1 Pro Preview, 3.1 Flash-Lite, 3 Flash Preview | 어떤 채팅 모델이든 |
| 언어 | 한국어 언급 없음 [미확인] | 선택한 임베딩 모델에 따름 |

기능 변경 이력은 changelog 2026-05-05에 "File Search 멀티모달 검색 지원(gemini-embedding-2), 인용 메타데이터에 media_id, page_numbers 추가"가 있다.

## 3. 4단계 기능

표의 "OpenAI 호환"은 호환 문서(2026-10-28)에 근거한다. 언급이 없으면 [미확인]이지 미지원 확정이 아니다. 네이티브 경로는 따로 적지 않으면 `POST https://generativelanguage.googleapis.com/v1beta/interactions`(헤더 `x-goog-api-key`)다. SDK는 `google-genai`(현재 2.34.0, Python ≥3.11, 문서는 `< 3.0.0` 고정을 권고)이고 Interactions는 SDK ≥2.3.0, TTS 예시는 ≥2.25.0이다. 출처 열은 문서 쪽 이름이고 갱신일은 8절에 있다.

`GET /models`로 보면 TTS, 이미지, 전사, Omni, Lyria, Deep Research, computer use, Antigravity 모델은 모두 `generateContent`를 지원한다. Live 계열은 `bidiGenerateContent`만, `lyria-realtime-exp`는 `bidiGenerateMusic`, `aqa`는 `generateAnswer`다 [실측].

### 3-1. 생성 계열

| 기능 | 모델과 상태 | 엔드포인트와 형식 | OpenAI 호환 | 제한 | 요금(유료, /1M) | 출처 |
|---|---|---|---|---|---|---|
| 이미지 생성·편집 | `gemini-nano-banana-2.1` GA 10/06(권장), `gemini-3.1-flash-lite-image` GA 6/30, `gemini-3.1-flash-image` GA 5/28(10/06 지원 중단 예고, 종료일 미정), `gemini-3-pro-image` GA 5/28, `gemini-2.5-flash-image` 레거시(종료 2027-03-15). Imagen 4는 종료(2026-08-17) | `model`, `input`(문자열 또는 파트 배열), `response_format{type:image, mime_type, aspect_ratio, image_size}`, `tools:[{"type":"google_search"}]`, 멀티턴 편집은 `previous_interaction_id`. `image_size`는 "1K"처럼 대문자 K 필수 | `images.generate(model, prompt, n, size, response_format="b64_json")`. 문서 모델은 `gemini-2.5-flash-image`, `gemini-3-pro-image-preview`(후자는 deprecations상 2026-06-25 종료라 문서가 낡음). `extra_body`로 aspect_ratio, generation_config, safety_settings, tools(Gemini 3 Pro Image 전용). 나열 외 파라미터는 조용히 무시. 편집 엔드포인트 언급 없음. 호출하면 새 Gemini 이미지 모델은 `images/generations`가 404(`not supported for predict`), `gemini-2.5-flash-image`는 무료 등급 한도 0으로 429 [실측] | 참조 이미지 합계 최대 14(2.1·3.1 Flash는 객체 10+캐릭터 4, Lite는 객체 14, Pro는 객체 6+캐릭터 5+스타일 3). 해상도 0.5K(3.1 Flash만)/1K/2K/4K, Lite는 1K만. thinking 끌 수 없고 토큰 과금. SynthID. 한국어 텍스트 렌더링 개별 언급 없음 | 무료 없음(무료 키는 429 [실측]). 2.1: 입력 $1.50, 텍스트·사고 출력 $7.50, 이미지당 1K $0.0336 / 2K $0.0504 / 4K $0.113. Lite: $0.25 / $1.50 / 1K $0.0336. 3.1 Flash: $0.50 / $3 / 0.5K $0.045, 1K $0.067, 2K $0.101, 4K $0.151. Pro: $2 / $12 / 1K·2K $0.134, 4K $0.24. 2.5 Flash Image: 입력 $0.30, 장당 $0.039. Batch는 절반 | image-generation, openai, pricing, deprecations |
| TTS | `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts` GA 9/22. `gemini-3.1-flash-tts-preview`, `gemini-2.5-*-preview-tts`는 11/17 종료 | `input`의 `user_input` 텍스트(스타일은 `speech_metadata` annotation), `response_format{"type":"audio"[,mime_type,sample_rate]}`, `generation_config.speech_config`(단일 화자는 배열, 다중은 `speakers`). 결과는 base64 오디오이고 `stream:true`면 오디오 델타가 온다. 네이티브 generateContent(`gemini-3.8-flash-tts`, 음성 Kore, 한국어 문장)가 무료 등급에서 200이었고 5.5초 분량 WAV(24kHz mono)를 3.7초에 받았다. Interactions TTS도 200 [실측] | 없음. `/audio/speech`는 404 [실측] | 사전 음성 30개, 확장 음성 `GET /v1beta/voices`, 음성 디자인·복제(프로젝트당 200개, 1년 미사용 시 삭제). 언어 자동 감지, 3.8 Flash는 130개 이상, Lite는 100개 이상, 한국어 포함. 다화자는 요청당 최대 2명(사전 음성만). 태그 `<laugh>`, `<short pause>` 등. 출력: 비스트리밍 WAV(44바이트 RIFF 헤더, 24kHz mono 16-bit), 스트리밍 raw PCM `audio/l16`, mulaw/alaw, 샘플레이트 24000/16000/8000. 입력은 텍스트만. 입력 길이 상한은 문서에 없음 [미확인] | 무료 티어 있음. 3.8 Flash TTS: 입력 $0.50, 출력(오디오 25토큰/초) $9.00, 10초당 $0.00225. Lite: 출력 $6.00, 10초당 $0.0015. Batch 절반. 둘 다 2027-01-01부터 2배 | speech-generation, pricing |
| 음악 | `lyria-3.5` GA 9/03(풀 곡), `lyria-3-clip-preview`(30초 고정, 레거시), `lyria-3-pro-preview`(레거시), `lyria-realtime-exp`(experimental) | `model="lyria-3.5"`, 입력은 텍스트(가사, `[Verse]`, `[0:00 - 0:10]` 태그)+이미지 최대 10장. 결과 `output_audio.data`. 기본 MP3, WAV 선택, 44.1kHz 스테레오. Realtime은 WebSocket `client.aio.live.music.connect()`, 가중 프롬프트, 48kHz 16-bit 스테레오 PCM, 세션 제한 문서 없음 | 없음 | 단일 턴(반복 편집 미지원). 특정 아티스트·저작권 가사 차단. SynthID. 같은 프롬프트라도 결과가 달라짐 | 무료 없음. lyria-3.5 곡당 $0.08. clip $0.04 | music-generation, realtime-music-generation, pricing |
| 영상 | `gemini-omni-1.1-flash` GA 8/27. `gemini-omni-flash-preview`는 9/30 지원 중단 예정. Veo 3.1 프리뷰 3종은 10/22 종료 | `response_format{"type":"video","aspect_ratio":"9:16"}`. 동기 응답에 base64 mp4(SDK `output_video.data`). 4MB 초과는 `delivery="uri"`(Files 폴링). 편집은 `previous_interaction_id`(`store=false`면 불가) | 없음. 호환 문서에 "/v1/videos 미지원" 경고 | 360p/720p(기본)/1080p·4K(업스케일), 16:9와 9:16. 오디오 포함(끄는 파라미터 없음). 업로드 영상 ≤10초, 확장 +3–10초씩 총 40초까지. 오디오 업로드 미지원. 시스템 지시, temperature, negative prompt 미지원. 영어만 완전 지원. SynthID | 무료 없음. 입력 $1.50, 출력 텍스트 $9.00, 영상 $17.50(720p 초당 5,792토큰, 초당 약 $0.10) | omni, pricing |

### 3-2. 입력 이해

| 기능 | 모델과 상태 | 엔드포인트와 형식 | OpenAI 호환 | 제한 | 요금 | 출처 |
|---|---|---|---|---|---|---|
| 음성 받아쓰기 | `gemini-3.5-transcribe` GA 8/26(비스트리밍), `gemini-3.5-transcribe-live` GA(Live API) | `input=[{"type":"audio","uri":<Files URI>,"mime_type":...}]`, `generation_config.transcription_config{language_codes:["ko-KR"], timestamp_granularities:["word"], diarization_mode:"speaker", custom_vocabulary}`. 모드는 verbatim(기본)/smart. 결과 `output_text`. 네이티브 `gemini-3.5-transcribe` generateContent에 인라인 wav를 넣어도 200이고 결과는 `parts[].audioTranscription.text`에 온다. Interactions도 200 [실측] | audio.transcriptions 없음(`/audio/transcriptions`는 404 [실측]). 대안은 chat completions에 `input_audio`(예시는 `format:"wav"`)를 `gemini-3.8-flash`에 보내 "Transcribe this audio"라고 하는 것이고, wav는 정확히 받아썼다 [실측] | 포맷 13종(WAV, MP3, AIFF, AAC, OGG, FLAC, MPEG, M4A, L16, Opus, ALAW, MULAW, WebM). 단건 최대 1시간, 화자 분리·단어 타임스탬프를 켜면 30분. 85개 이상 로케일 자동 감지(문장 내 언어 전환 포함), 한국어 `ko-KR` 지원. 화자 최대 8명(3명 이상은 실험적). custom_vocabulary ≤1,000(100 이하 권장, 화자분리·단어 타임스탬프와 동시 불가). 문서 예시는 Files API 업로드뿐이고 인라인은 설명이 없는데, 인라인 wav는 된다 [실측]. Live판은 세션당 10분, 16kHz PCM 100ms 청크, interim/final 결과, 화자분리·단어 타임스탬프 불가 | 무료 티어 있음(무료 키로 200 [실측]). Transcribe: 입력 오디오 $2.00, 출력 텍스트 $12.00, 합산 약 분당 $0.005. Live판: $3.50 / $21.00, 합산 약 분당 $0.009 | transcribe, live-api/live-transcribe, pricing |
| 채팅 모델에 오디오·영상·PDF 직접 입력 | `gemini-3.8-flash` 등. 입력 텍스트, 이미지, 영상, 오디오, PDF, 출력 텍스트. 컨텍스트 1,048,576, 출력 65,536 | 오디오: 인라인 base64는 요청 전체 20MB 이하, 그 이상은 Files API, 프롬프트당 최대 9.5시간, 초당 32토큰. PDF: `{"type":"document","data"\|"uri","mime_type":"application/pdf"}`. 영상: 인라인, Files API, YouTube URL, 기본 1 FPS | 이미지 `image_url`(data URL)과 오디오 `input_audio`만 문서화. Files 업로드/다운로드 미지원. PDF는 `{"type":"file"}` 파트가 400 `Invalid content part type: file`이지만, `image_url`에 `data:application/pdf;base64,...`를 넣으면 3.6 Flash가 내용을 읽었다. 1쪽이 520토큰(IMAGE)이었고 네이티브 인라인도 된다 [실측]. 영상은 언급 없음 [미확인] | PDF는 파일당 50MB·1,000쪽, 쪽당 258토큰, 문서 비전은 PDF만(TXT/HTML/MD는 텍스트 추출만). 영상: 인라인 한도가 본문 20MB 미만 대 표 100MB 미만으로 문서 내 불일치. 토큰은 정적 모드 약 100(low)~300(high)/초, 1M 컨텍스트에서 low 3시간·high 1시간. YouTube는 공개만, 무료 일 8시간, 요청당 최대 10개. 에이전틱 영상 이해(`processing:"agentic"`, Gemini 4/3.8/3.6/3.5 Flash-Lite): 긴 영상 토큰 최대 88% 절감, 긴 영상은 background. Files API: 파일당 2GB, 프로젝트 20GB, 48시간 후 삭제, 무료, 올린 파일 다운로드 불가 | 모델 입력 토큰 단가(3.8 Flash $0.75, 2027-01-01부터 $1.50) | audio, document-processing, video-understanding, files, models/gemini-3.8-flash |

### 3-3. 도구, 캐싱, 함수 호출

| 기능 | 모델과 상태 | 엔드포인트와 형식 | OpenAI 호환 | 제한 | 요금 | 출처 |
|---|---|---|---|---|---|---|
| Google 검색 그라운딩 | Gemini 4 Argon, 3.8/3.6 Flash, 3.5 Flash-Lite, 3.1 Pro Preview 등. 이미지 모델도 가능(2.1·3.1 Flash는 web/image search, Lite 불가) | `tools=[{"type":"google_search"}]`. steps: `google_search_call`(queries), `google_search_result`(`search_suggestions` HTML), `model_output.content[].annotations[]`(type `url_citation`, url, title, start_index, end_index) | 채팅 방법 문서에 없음. chat에서 `{"type":"google_search"}`는 400 `Invalid tool type: google_search`, `{"google_search":{}}`는 400 Unknown name [실측]. 이미지 `extra_body.tools`는 Gemini 3 Pro Image 전용 | 프로젝트당 기본 일 1,500건(Gemini 3+). function calling, url_context, code_execution과 조합 가능, File Search와는 불가. 표시 의무 세부는 약관을 가리킴. 이미지 검색 사용 시 search_suggestions 표시 필수. 그라운딩 사용 시 프롬프트·출력 30일 저장(약관). 무료 키로는 네이티브 generateContent와 Interactions 모두 429 [실측] | Gemini 3+: 월 5,000건 무료(전 모델 공유) 후 1,000건당 $14(검색 쿼리 단위). 2.5: 일 1,500건 무료 후 프롬프트 1,000건당 $35. 3.8 Flash의 무료 티어는 "Not available" | google-search, pricing |
| URL context | 3.8/3.6 Flash, 4 Argon, 2.5 계열 등 | `tools=[{"type":"url_context"}]`, URL은 프롬프트에 직접. `url_context_result` step(상태, 안전성 실패 시 "unsafe") | 언급 없음 [미확인] | 요청당 20 URL, URL당 34MB. 공개 URL만. 텍스트·이미지·PDF 가능, 유료 벽·YouTube·Google Workspace 문서·영상·오디오 불가. 중첩 링크는 안 따라감. File Search와 동시 사용 불가 | 도구 무료(무료 티어 포함), 가져온 내용은 입력 토큰 | url-context |
| 코드 실행 | `gemini-3.8-flash` 등 | `tools=[{"type":"code_execution"}]`. steps: `code_execution_call`(`arguments.code`), `code_execution_result` | 언급 없음 [미확인] | Python만, 라이브러리 37종(numpy, pandas, scipy, scikit-learn, tensorflow, opencv, matplotlib 등), 설치 불가. 최대 실행 30초, 오류 시 재생성 최대 5회. 입력 파일은 CSV/텍스트 권장(인라인 또는 Files). 그래프는 matplotlib만. 네트워크 접근은 설명 없음 [미확인] | 추가 요금 없음. 중간 토큰은 입력 토큰으로 과금 | code-execution |
| 컨텍스트 캐싱 | 암묵적: Gemini 2.5 이후 전 모델 기본 ON. 명시적(`cachedContents`)은 Interactions API와 Gemini 4 이후 모델에서 지원되지 않음 | 최소 토큰: 4 Argon·3.8·3.6·3.1 Pro 4,096, 2.5 Flash/Pro 2,048. 접두어를 앞에 두고 짧은 시간 안에 반복해야 적중. 명시적은 Gemini 3/2.5에서 generateContent로만 | 암묵적 캐시는 호환 chat에서 적중한다. 3.6 Flash에 1만2천 토큰 접두어를 반복하면 두 번째 요청부터 `prompt_tokens_details.cached_tokens`가 10,004였다 [실측]. 명시적은 `extra_body={"google":{"cached_content":"cachedContents/<id>"}}`(문서 표). Python 예시는 base_url에 `/openai/`가 빠졌고 extra_body가 이중 중첩이라 신뢰하기 어려우며 동작은 [미확인] | Gemini 4+ 멀티턴은 새 요청이 ≥2,050토큰을 더해야 이전 요청 전체가 캐시됨. 기본/최대 TTL은 해당 문서에 없음 | 3.8 Flash: 캐시 입력 $0.075(2027-01-01부터 $0.15), 저장 $0.50/1M토큰·시간(→$1.00). 무료 티어 캐시 입력 무료 | caching, pricing |
| 함수 호출 | 전 모델 | 한 턴에 여러 함수(병렬)와 순차(compositional) 지원. 모드 auto/any/none/validated. 내장 도구와 함수 호출을 한 요청에서 조합 가능(changelog 2026-03-18, Gemini 3 이상, `previous_interaction_id`가 내장 도구 컨텍스트를 순환) | 지원(이미 사용 중) | 개수 하드 상한은 문서에 없음. Best practices에 "Keep active set to 10-20 tools maximum". OpenAPI 부분집합만 지원, `any` 모드는 크고 깊은 스키마를 거부할 수 있음. Gemini 3는 멀티모달 함수 응답 가능. thought signature 보존 필요(SDK 자동, stateless는 직접). 3.6 이후: 마지막 model 턴(prefill)은 400, `candidate_count` 미지원. 2026-07-21~10-28 사이 도구 개수·병렬 호출 관련 공지는 changelog에 없다. Gemini 3.8 Live는 기본이 비동기 함수 호출이다(changelog 2026-09-15). Interactions의 Python 자동 함수 호출은 "not yet available" | 모델 토큰 요금 | function-calling, changelog, whats-new-gemini-3.6 |

### 3-4. 실시간과 에이전트

| 기능 | 모델과 상태 | 엔드포인트와 형식 | OpenAI 호환 | 제한 | 요금 | 출처 |
|---|---|---|---|---|---|---|
| Live API | `gemini-3.8-live`, `-extended-thinking` GA 9/15(기본). `gemini-3.5-live-translate-preview` Preview. `gemini-3.5-transcribe-live` GA. `gemini-3.1-flash-live-preview`와 `gemini-2.5-flash-native-audio-preview-12-2025`는 11/17 종료 | WebSocket `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=...`. 첫 메시지는 `setup`(모델 `models/gemini-3.8-live`, `responseModalities:["AUDIO"]`, `systemInstruction`)이고, 입력은 `realtimeInput.audio`(base64, `audio/pcm;rate=16000`), 출력은 24kHz 16-bit PCM이다. 영상은 JPEG ≤1FPS. 입력·출력 전사 지원. 클라이언트-투-서버는 에페메럴 토큰(`BidiGenerateContentConstrained?access_token=`). SDK `client.aio.live.connect`도 있음 | 없음 | 압축 없이 오디오 15분, 오디오+영상 2분. 연결 약 10분 후 GoAway(`timeLeft`). `contextWindowCompression`(sliding window)으로 연장, `sessionResumption` 토큰 2시간 유효. 도구는 function calling과 Google Search를 쓸 수 있고 File Search는 미지원. 70개 언어 대화(한국어 개별 명시는 확인 못 함). 동시 세션 수 문서에 없음 | 무료 티어 있음. 입력 텍스트 $0.75, 오디오 $3.00(분당 $0.005), 영상 $1.00(분당 $0.002). 출력 텍스트 $4.50, 오디오 $12.00(분당 $0.018). Live Translate 합산 약 분당 $0.0368 | live-api, live-api/session-management, live-api/get-started-websocket, pricing |
| Computer use | 도구 `computer_use`, Preview. 권장 `gemini-3.8-flash`, 그 밖에 4 Argon, 3.5 Flash-Lite 등. `gemini-2.5-computer-use-preview-10-2025`는 deprecations상 2026-07-28 종료(대체 3.8 Flash) | Interactions API. 루프: 스크린샷 → `function_call`(액션+intent) → 클라이언트 실행(Playwright 등) → 새 스크린샷을 `function_result`로 → `previous_interaction_id` | 없음 | 액션 브라우저 20개 / 모바일 10개 / 데스크톱 17개. 좌표는 0–999 정규화. `safety_decision`(require_confirmation, blocked), 정책 7종, `enable_prompt_injection_detection`(기본 false, 3.5 Flash-Lite 이상). 샌드박스 VM/컨테이너 필수 권고 | 무료 없음. 별도 단가 없이 모델 토큰 요금 | computer-use, deprecations |
| Deep Research | `deep-research-preview-04-2026`(속도), `deep-research-max-preview-04-2026`(포괄성), 둘 다 Preview. `deep-research-pro-preview-12-2025`는 2026-10-23 종료 | Interactions API 전용. `client.interactions.create(agent="deep-research-preview-04-2026", input=..., background=True)` 후 `interactions.get`으로 폴링(예시 10초) 또는 `stream=True`+`background=True`(+`agent_config.thinking_summaries:"auto"`). 스트림은 약 600초 후 끊길 수 있어 `last_event_id`로 재연결 | 없음 | 완료까지 수 분. 도구 기본값은 Google Search, URL Context, Code Execution. `mcp_server`와 `file_search` 추가 가능, 사용자 정의 함수 호출은 없음. 파일은 이미지·PDF를 uri로. 계획 승인(`collaborative_planning`) 지원. 시각화 기본 auto | 별도 단가 없음. 에이전트 루프 중 추론 토큰까지 표준 요율, 도구 사용료 별도 | deep-research, interactions, pricing |

그 밖의 모델(출처: models)
- `gemini-3.1-pro-preview-customtools`는 bash와 커스텀 도구를 섞는 에이전트용 별도 엔드포인트이고 가격은 3.1 Pro와 같다 [간접].
- `antigravity-preview-09-2026`은 격리된 Linux 샌드박스의 관리형 에이전트(Interactions API)다. 프리뷰 기간에는 환경 컴퓨트를 과금하지 않는다.
- `aqa`, `gemma-4-*`, `nano-banana-pro-preview`는 문서에서 찾지 못했다(`aqa`는 `GET /models`에 있다) [미확인].

## 4. MCP
(출처: PyPI, py.sdk.modelcontextprotocol.io whats-new/migration, modelcontextprotocol.io 사양, Gemini function-calling/interactions 문서, python-genai README)

| 항목 | 내용 |
|---|---|
| `mcp` 패키지 버전 | 최신 2.3.0(PyPI 업로드 2026-10-02). 2.0.0은 2026-07-28. v1.x도 유지(보안 패치)되고 최신 1.30.0이 2026-09-07. 공식 문서: "v2, the current stable release line" |
| Python 요건 | PyPI 메타데이터 `>=3.10`, 공식 문서·README는 "Python 3.11+"(불일치). 이 저장소는 Python ≥3.12라 영향 없음 |
| 전송 방식 | stdio, Streamable HTTP(주력), SSE 코드도 남아 있음(폐기 여부는 확인 못 함). WebSocket은 v2에서 제거 |
| 클라이언트 API | v1의 `ClientSession`+`stdio_client`+`streamablehttp_client` 3단 구조가 v2의 `Client` 하나로 통합. URL이면 Streamable HTTP, `StdioServerParameters`면 stdio. 헤더·인증은 미리 구성한 `httpx2` 클라이언트를 `http_client`로 넘김. `streamablehttp_client`는 `streamable_http_client`로 개명. 타임아웃은 float 초 |
| 프로토콜 | 사양 최신 리비전 2026-07-28, 이전 2025-11-25. v2는 둘 다 동시 지원, 기본 협상은 2026-07-28이고 구버전 서버면 `initialize` 폴백 |
| 의존성(2.3.0) | `httpx2>=2.10.0`(httpx 대체, 예외 클래스가 다르고 TLS는 OS 신뢰 저장소), `mcp-types==2.3.0`, `pydantic>=2.12.0`, `anyio`, `starlette`, `uvicorn`, `sse-starlette`, `jsonschema`, `pyjwt[crypto]`, `python-multipart`, `opentelemetry-api`, Windows에서 `pywin32>=311` |
| Windows | mcp 소스에 `src/mcp/os/win32/` 폴더가 있고 `pywin32`를 의존하므로 지원 구조다. v2 문서에는 Windows 안내가 없다 [미확인]. 과거 이슈: `npx`는 `npx.cmd`나 `cmd /c`가 필요한 사례, stdio 초기화 hang(python-sdk issue #552, #359) [간접]. asyncio 서브프로세스는 Windows에서 Proactor 루프가 필요하며 이 저장소는 이미 보장한다(5절) |

Gemini API에서 MCP를 쓰는 경로는 다음과 같다.
- Interactions API: `tools:[{"type":"mcp_server","name":...,"url":...,"headers":{...},"allowed_tools":[...]}]`. 원격 Streamable HTTP만 가능하고 SSE 서버는 불가하며, 서버 이름에 `-`를 쓸 수 없다(snake_case). 문서 예시 모델은 `gemini-3.8-flash`다. 같은 Interactions 개요 문서에는 "Gemini 3 does not support remote MCP, this is coming soon"이 있어 서로 모순이다(6절).
- google-genai SDK 내장 MCP: 열어 둔 세션을 `tools=[session]`으로 `GenerateContentConfig`에 전달하면 자동 함수 호출로 MCP 도구를 호출한다. README가 "experimental"이라고 쓴다.
- Deep Research도 `mcp_server`를 지원한다(2026-04-21).
- OpenAI 호환 경로에는 MCP 언급이 없다. 그래서 우리가 MCP 클라이언트를 직접 두고 도구 목록을 일반 함수 호출로 노출하는 방식이 OpenAI 호환 경로와 맞는다 [추정].

## 5. 기존 구현에 영향을 주는 부수 발견

- 폴백 체인(`apps/api/app/config.py:58-60`, `apps/api/app/llm/catalog.py:40-45`)
  - 체인은 3.8 → 3.7 → 3.6인데, `gemini-3.7-flash`는 문서상 `gemini-3.8-flash`로 자동 라우팅된다. 3.7은 AI Studio에서 선택할 수 없고 엔드포인트만 유지된다.
  - ADR-0003(`docs/decisions/0003-llm-provider-gemini.md`)에는 2026-10-12 호출 테스트에서 3.8과 3.7이 함께 503이었고 3.6만 정상이었다는 기록이 있다. 이 라우팅(공지 2026-10-08)과 맞는다 [추정]. 두 번째 단계가 사실상 첫 번째와 같은 백엔드일 가능성이 있다.
  - 2026-10-30에도 비슷했다. `gemini-3.8-flash`는 한 번에 79초가 걸린 뒤 503 `high demand`가 이어졌고, `gemini-3.7-flash`는 147초, `gemini-3.6-flash`는 2.5초였다. 3.7 응답의 `model` 필드는 3.7 그대로라 별칭 여부는 응답만으로 가릴 수 없지만, 3.8과 같이 느렸다 [실측].
- `temperature`와 `top_p`(`apps/api/app/llm/gemini_adapter.py:478-481`)
  - 어댑터가 두 값을 보낸다. 3.6 Flash 이후 모델에서는 deprecated이고 무시되며, 이후 세대에서는 400이 된다고 문서에 있다.
  - 이 값을 흘려보내는 곳은 `apps/api/app/schemas/agent_version.py:52-53`, `apps/api/app/workflow/dsl/dify_import.py:531-536`, `apps/api/app/workflow/nodes/llm.py:123-124`, `apps/api/app/services/run_executor.py:790-791`이다.
  - OpenAI 호환 경로에서 3.8 Flash에 이 값을 넣은 요청은 200이다(3.7, 3.6도 같다) [실측]. 무시되는지, 값에 따라 출력이 달라지는지는 확인하지 못했다.
- `reasoning_effort="minimal"`(`apps/api/app/llm/catalog.py:27-28`): 3.8에서 400이라는 기록은 모델 페이지와 일치한다("`minimal` is not supported and returns an error", 지원값은 low/medium/high). 주석에는 3.8·3.7이 400, 3.6은 정상으로 적혀 있다. OpenAI 호환 문서의 reasoning_effort 매핑 표에는 3.8/3.6이 없다.
- Windows 이벤트 루프(`apps/api/app/runner/winconsole.py:54-62`): 이미 Proactor 루프 정책을 보장한다. MCP stdio 클라이언트가 자식 프로세스를 띄울 때 필요한 조건이라 새로 만들 것은 없다.
- 의존성(`apps/api/pyproject.toml`): `httpx>=0.28.1`과 `openai`가 이미 있어서 네이티브 REST는 `httpx`로 직접 부를 수 있다. Live API를 직접 구현한다면 문서 예시가 쓰는 `websockets`를 `uvicorn[standard]`가 보통 끌어온다 [일반 지식, 설치 목록 미확인]. `google-genai` 2.34.0의 의존성은 `google-auth[requests]`, `requests`, `httpx`, `websockets`, `pydantic`, `tenacity`, `anyio`, `distro`, `sniffio`, `typing-extensions`이고 메모리와 임포트 시간은 측정하지 않았다.

## 6. 문서 간 불일치

1. Interactions API 상태: HTML 페이지(2026-10-15)는 "As of June 2026, it is Generally Available and recommended for all new projects"인데, 원문 마크다운 변형(.md.txt)은 "currently in Beta"이고 "generateContent is the recommended path for stable deployments"라고 쓴다. 낡은 캐시로 추정되어 HTML 쪽을 따른다.
2. 원격 MCP와 Gemini 3: 함수 호출 문서(2026-09-23)는 Interactions API가 원격 MCP를 지원한다며 `gemini-3.8-flash` 예시를 든다. Interactions 개요(2026-10-15)는 "Gemini 3 does not support remote MCP, this is coming soon"이라고 쓴다.
3. 영상 이해(3-2절 표): 초당 토큰이 본문 263과 표 100/300으로, Files API 한도가 영상 문서("무료 2GB, 유료 20GB")와 Files 문서("파일당 2GB, 프로젝트당 20GB")로 갈린다.
4. File Search 문서: 설명은 `generateContent` 호출이라는데 예시는 전부 `interactions`다.
5. 임베딩 별칭: deprecations 표는 `embedding-2-preview`(접두어 "gemini-" 없음)를 종료로 적는데, `gemini-embedding-2-preview`는 계속 응답하고 `gemini-embedding-2`와 같은 벡터를 낸다. 표의 행과 이 이름의 대응은 불명확하나 호출 결과로는 별칭이다 [실측].

## 7. 남은 확인 항목

호출로 확인하지 못한 것이다. 유료 키가 필요하거나 비용이 큰 것은 뒤에 둔다.

- 임베딩
  - 멀티모달 임베딩: 네이티브 `embedContent`에 PNG, PDF(1~6쪽), 오디오(≤180초)를 인라인으로 보내 차원과 토큰 사용량을 본다. 호환 경로에서 멀티모달 입력 형태가 되는지도 본다.
  - 무료 키로 `batches.create_embeddings`가 되는지(가격표는 무료 Not available). 되면 10건 인라인 요청의 소요 시간.
  - `batchEmbedContents`의 `usageMetadata.promptTokenCount`, `GET /v1beta/models/gemini-embedding-2`의 `supportedGenerationMethods`와 `inputTokenLimit`.
  - AI Studio에서 키 8개가 속한 프로젝트 수, `gemini-embedding-2` 무료 TPM의 정확한 값과 001의 한도, 유료 등급의 임베딩 한도.
- File Search: 한국어 .md/.pdf 2~3개로 스토어를 만들고 `interactions.create` + `file_search`를 호출한다. 실제 `annotations` 필드, 청크 원문 노출 여부, 한국어 질의 품질, 색인 완료까지 시간, 삭제 후 반영, 무료 키 허용 여부를 본다. 호환 chat에 file_search를 줄 수 있는지도 본다(400 예상).
- 4단계
  - 이미지 생성: 결제 연결 키로 네이티브 Interactions를 모델 4종(`gemini-nano-banana-2.1`, `gemini-3.1-flash-lite-image`, `gemini-2.5-flash-image`, `gemini-3-pro-image`)에 호출한다. 호환 경로는 404라 네이티브만 남는다.
  - TTS 스트리밍과 긴 텍스트(약 3,000자)의 입력·오디오 길이 한계. 전사는 Files API로 올린 한국어 음성, 1시간 한계, 화자 분리·단어 타임스탬프, `input_audio`에 wav 외 형식(mp3, m4a, ogg)과 20MB 근처 크기.
  - 호환 chat에 영상 입력, URL context·코드 실행 도구(`extra_body` 변형). 유료 키로 3.8 Flash 검색 그라운딩.
  - Interactions `mcp_server` + `gemini-3.8-flash`(6절의 문서 모순). 명시적 캐시를 네이티브로 만든 뒤 `extra_body.google.cached_content`가 먹는지.
  - 종료 모델 응답 여부: `gemini-3-pro-image-preview`, `nano-banana-pro-preview`, `gemini-2.5-computer-use-preview-10-2025`, `gemini-2.5-flash-preview-tts`. `gemini-3.7-flash`가 3.8과 같은 백엔드인지는 `model` 필드로 가를 수 없어 다른 방법이 필요하다. 3.8 Flash의 `temperature`/`top_p` 값에 따른 출력 차이.
  - Live: 짧은 WebSocket 세션(`gemini-3.8-live`)에서 한국어 음성, 무료 키 동시 세션 수, GoAway 시점.
  - Omni와 Deep Research는 유료·고비용(Omni는 무료 불가, Deep Research는 수 분에 고토큰)이라 맨 마지막에, 비용 상한을 두고 한다.

문서에서 확인하지 못한 것
- 임베딩의 한국어 품질 공식 수치, 이미지 종횡비 전체 표, Deep Research의 할당량과 데이터 보존, Live API 3.8의 한국어 명시 여부(문서는 "70개 언어"만)와 동시 세션 수, `aqa`·`gemma-4-*`·`nano-banana-pro-preview` 문서, `google-genai` 2.34.0 업로드일, MCP v2의 Windows 동작 문서. 이미지 생성, Omni, Deep Research 문서는 본문이 잘렸거나 날짜 표기가 없어 갱신일을 적지 못했다.
- Interactions API의 REST 원본 JSON 샘플은 일부만 봤고 요청 형식은 SDK 예시 기준이다. TTS 음성 수와 언어 수, 전사 한도, Omni 해상도, Lyria clip 가격, 컴퓨터 사용 액션 수, 코드 실행 라이브러리 수는 교차 확인하지 못했다. 표와 코드 블록이 요약된 형태로 읽혔을 수 있어서 설계에 쓰기 전에 원문을 다시 본다.

## 8. 출처

갱신일은 문서 하단 "Last updated"(UTC)다.

공식 문서 (https://ai.google.dev/gemini-api/docs/ 아래)
- `embeddings` 2026-10-29 / `file-search` 2026-10-13 / `openai` 2026-10-28 / `pricing` (HTML 2026-10-28, 원문은 날짜 없음) / `rate-limits` 2026-10-28
- `batch-api` 2026-09-17 / `tokens` 2026-10-13 / `changelog` 2026-10-28 / `deprecations` 2026-10-22 / `interactions` 2026-10-15
- `image-generation` (갱신일 확인 못 함) / `speech-generation` 2026-10-28 / `audio` 2026-09-23 / `transcribe` 2026-10-23 / `function-calling` 2026-09-23
- `live-api` 2026-10-06 / `live-api/session-management` 2026-09-15 / `live-api/get-started-websocket` 2026-09-15 / `live-api/live-transcribe` 2026-10-29
- `computer-use` 2026-10-22 / `deep-research` (갱신일 확인 못 함) / `omni` (갱신일 확인 못 함) / `music-generation` 2026-10-01 / `realtime-music-generation` 2026-09-17
- `caching` 2026-10-23 / `google-search` 2026-10-28 / `url-context` 2026-10-13 / `code-execution` 2026-10-13
- `document-processing` 2026-10-08 / `video-understanding` 2026-10-13 / `files` 2026-10-26
- `models` 2026-10-28 / `models/gemini-3.8-flash` 2026-10-29 / `whats-new-gemini-3.6` 2026-10-13
- API 레퍼런스: https://ai.google.dev/api/embeddings (2026-10-26), https://ai.google.dev/api/tokens (2026-08-17), https://ai.google.dev/api/models (2026-10-29)
- 약관: https://ai.google.dev/gemini-api/terms (2026-04-28, 시행 2026-03-23)
- Google 블로그: https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-embedding-2/ (2026-03-10)

MCP·SDK
- https://pypi.org/pypi/mcp/json (2.3.0, 2026-10-02) / https://pypi.org/pypi/google-genai/json (2.34.0)
- https://py.sdk.modelcontextprotocol.io/ , /whats-new/ , /migration/ (문서 날짜 없음)
- https://modelcontextprotocol.io/specification/latest (리비전 2026-07-28)
- https://github.com/modelcontextprotocol/python-sdk , https://github.com/googleapis/python-genai (README, 날짜 없음)

간접 자료
- 포럼 https://discuss.ai.google.dev/t/use-task-type-when-generating-embeddings-with-openai-library/74906 (2025-03-25 질문, 2025-05-22 답변)
- 포럼 https://discuss.ai.google.dev/t/gemini-embedding-2-preview-appears-to-ignore-task-type-for-text-and-image-embeddings/134720 (2026-03-20, 답변 2026-03-31)
- https://github.com/vercel/ai/pull/16575 (2026-07-02 병합)
- LangChain 소스 https://raw.githubusercontent.com/langchain-ai/langchain-google/main/libs/genai/langchain_google_genai/embeddings.py
- https://arxiv.org/abs/2503.07891 (2025-03-10)
