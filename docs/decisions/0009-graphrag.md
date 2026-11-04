# ADR-0009: GraphRAG는 KB 위의 독립 자원, LLM 없는 개념 그래프부터 단계적으로

- 상태: 채택(2026-11-03, 사용자 결정. 2026-11-04 design-reviewer 검토와 사용자의 "LLM 추출도 무료 키로" 결정을 반영). 설계는 `docs/design/phase4-graphrag.md` v2(안 D 확정판). 구현은 3단계 완료(M29) 뒤 사용자 확인을 받고 4-G1a부터 시작한다.
- 날짜: 2026-11-03
- 배경: 사용자가 2026-11-03에 GraphRAG를 4단계 후보로 올리며 셋을 요구했다. KB의 청크 모드가 아니라 따로 만드는 기능, 개체는 점·관계는 선·무리는 색·중요도는 크기인 그래프 지도(Gephi식 인물 관계망이 예시), 결과를 보고 고치는 편집(같은 개체 합치기, 잘못된 관계 지우기, 이름 바꾸기). 설계서 v1이 안 A(GraphRAG식 완전판)·B(LightRAG식 추출판)·C(LazyGraphRAG식 개념판)·D(단계판)를 비교했고 사용자가 D를 골랐다. 같은 날 밤 M27 live 시험 몇 번 뒤 무료 키 채팅이 3.8·3.7·3.6-flash 모두 일일 429였고, `_run_chain`의 `any_exhausted` 규칙(키 하나라도 일일 한도면 그 모델 전체를 건너뜀)과 합쳐 보면 프로젝트당 일일 한도가 두 자리 이하일 수 있다. 2026-11-04 사용자는 "LLM 추출도 무료 키로 할 수 있게" 하라고 정했다. 서버 RSS 예산은 300MB이고 Kiwi는 서버에 넣을 수 없지만(3단계 결정) 샌드박스 하위 프로세스는 있다. networkx 3.7(2026-09-21)에 Leiden 본체가 들어왔고 3.5·3.6은 backend 전용이었다.

## 결정

### D1. 그래프는 독립 자원이고 SQLite 표에 둔다. 원본은 KB 문서, 근거는 `kb_segment`
- 자원 `graph`(`/knowledge/graphs`)는 KB 여러 개(상한 5)의 문서를 원본으로 삼고 KB와 따로 산다. 개체·관계·언급·무리·보고서·별칭·편집·제안·질의 기록은 app.db 표(마이그레이션 0009, 모두 `workspace_id`), 개체·보고서 벡터와 bigram FTS는 index.db(스키마 버전 2, KB와 같은 `graph_index`·`gvec_`·`gfts_`)다. 그래프 DB 서버는 없다(ADR-0004).
- 근거 청크는 KB의 `kb_segment`(또는 Parent-child 부모)를 그대로 가리킨다. 파싱·청크·전문 색인·청크 화면·인용 경로를 3단계에서 재사용하고 원본을 KB 밖에 두지 않는다(파일은 Economical KB에 넣는다).
- 개체 행은 `(graph_id, name_norm)` 업서트라 재생·증분·승격 때 id가 유지되고, 별칭은 `canonical_norm`으로 묶으며, 지운 관계는 `state=deleted` 차단 키로 남는다. 사용자 편집이 진실이고 재조립이 덮지 않는다.

### D2. 1단계는 LLM 호출 0이다. Kiwi 개념 그래프, local·hybrid 검색
- 4-G1a·b·c는 샌드박스의 Kiwi로 고유명사(NNP·SL) 공기 그래프를 만들고 지도·편집·`graph_retrieval` 노드·`search_graph` 도구·인용까지 완성한다. 만들 때도 검색할 때도 채팅 호출이 0이고 임베딩도 0이다(질의 임베딩 1회는 벡터 KB의 재순위에만). global(말뭉치 전체 질문)은 4-G2(경량 map-reduce)·4-G3(보고서)에서 연다.
- 정규화 키는 NFC → 소문자 → 공백·구두점 제거 → 끝 조사 제거 → 호칭·직함 제거(1단계는 모든 NNP 구, 설정으로 끔) → 괄호 제거다. 조사 변이는 형태소 분석기가 없애지만 띄어쓰기·약칭·호칭·한영 혼용은 남아 합치기 제안과 편집으로 다룬다. df 가지치기는 1,000유닛 미만이면 끈다(M21b), 불용어·`min_count`를 쓴다, 무방향 관계는 `source_id < target_id`로 정규화한다.

### D3. LLM 추출(4-G2)은 무료 키로도 돈다. 유료 키는 빠르게 하려는 선택
- 그래프의 키 등급은 원본 KB 등급을 따른다(최댓값 이상). 무료 그래프는 무료 키만, 유료 그래프는 유료 키만 쓰고 섞지 않는다(ADR-0007 D8과 같은 데이터 정책). 개체 그래프는 만들 때 "청크 전체가 채팅 모델로 간다. 무료 키면 제품 개선에 쓰일 수 있다" 확인을 받고, 개념 그래프는 데이터가 PC 밖으로 나가지 않아 확인이 없다.
- 무료 키에서 반드시 지키는 셋. (1) 대화형 보호: `ChatLimiter`의 무료 bulk 일일 몫은 그 모델의 일일 한도(설정 화면에 넣은 AI Studio 값 또는 429 본문의 `quotaValue`로 학습한 값, app.db `llm_quota`) × `LLM_BULK_SHARE`(기본 0.5)이고 고정 상수는 없다. 한도를 모르는 모델은 bulk가 돌지 않는다. (2) 429를 맞기 전에 멈추기: bulk는 몫에 닿거나 그 모델의 전체 호출이 `rpd − 1`에 닿으면 호출하지 않고 `paused(daily_quota)`로 멈춘다. 그래도 일일 429를 맞으면 그날 bulk를 끝내고 `quotaValue`를 학습한다(`any_exhausted` 때문에 bulk의 429 한 번이 대화형까지 막는다). interactive는 세되 막지 않는다. (3) 몫 분리: 추출 기본 모델을 대화형 체인(3.8 → 3.6)에 없는 Flash-Lite 계열로 두는 안이 기본안이고(id·단가·한도·한국어 품질을 조사·비교 시험 뒤 확정), 3.8 Flash가 대안이다. 추출 체인은 대화형 체인으로 떨어지지 않는다.
- 여러 날에 걸친 진행: 몫을 다 쓴 작업은 `paused(daily_quota)`로 남고 `graph.auto_resume`(기본 켬)이면 태평양 자정 리셋 뒤 그래프 레인이 자동으로 잇는다. KB 인제스트(M22)는 실제 429 뒤 남은 한도를 몰라 자동 재투입이 없지만, 그래프는 429 전에 알려진 몫에서 멈추므로 재개가 안전하고, 몇 주짜리 작업을 매일 손으로 잇는 것은 비현실적이다.
- 비용·기간 표는 무료 일수를 고정값으로 두지 않고 "일수 = 호출 수 ÷ bulk 일일 몫" 식과 실측 자리로 둔다. 비용 미리보기는 무료 키 기준 예상 일수와 유료 키 기준 금액을 함께 보여 준다. 유료 키의 추출 모델 추천은 `gemini-3.8-flash`(입력 $0.75/M·출력 $3.75/M, 2027-01-01부터 두 배)이고 Lite 비교 시험 뒤 확정한다.
- 승격(개념 → 개체)은 새 그래프가 아니라 같은 그래프의 `kind` 전환이고 사용자가 손댄 개체·별칭·차단 키를 씨앗으로 남긴다. nlp 추출 행을 남겨 되돌리기는 호출 0이다.

### D4. Kiwi·networkx·numpy(·igraph)는 샌드박스 하위 프로세스에만 둔다
- 서버는 이들을 import하지 않는다(`test_layering.py` 규칙). 설치는 `data/cache/graph-tools-<사양 해시>/`에 `pip --target`(상한 600초·1,024MB), 실행은 3단계 `kb_backend`(`python -I`, Job Object 768MB, 최소 환경변수). 토큰화 묶음은 글자 수(50만 자)로 끊고, 파서·SQL·그래프 하위 프로세스의 동시 메모리는 kb-work 게이트(`KB_WORK_MEMORY_MB` 1,536)가 묶는다.
- 무리는 기본 Louvain(networkx `louvain_communities`. Gephi의 모듈성 통계와 같은 알고리즘), 선택 Leiden(`networkx>=3.7` 고정, `metric="modularity"`)·igraph. 계층(`max_cluster_size` 10, 깊이 3)은 상한을 넘는 무리를 재귀 분할해 우리가 만든다. 중요도는 가중 PageRank 멱승법(scipy 없이), 좌표는 ForceAtlas2(증분은 이전 좌표 시드).

### D5. 지도는 sigma.js lazy 묶음이고 4-G1a부터 보인다
- sigma.js 4 + graphology + `@react-sigma/core` 6(MIT, 약 135KB gzip, 상한 150KB)을 lazy 묶음 `GraphMapPage`에 둔다. 첫 화면 묶음은 늘지 않는다. 점은 원(크기 = PageRank 로그 눈금), 색 = 무리 `color_index`, 선 두께 = 가중치, 이름표는 확대 수준·중요도 순. 기본 2,000노드·상한 10,000, WebGL이 없으면 표 보기.
- 4-G1a는 읽기 전용(확대·이동, 점 패널과 근거 청크, 검색·필터·범례)이고 4-G1c에서 편집·이웃 보기·다시 배치·화면 무리 짓기·인용 칩의 "지도로 가기"가 붙는다. 무리·좌표의 진실은 서버 저장값이고 브라우저 계산은 화면에만 적용된다.

### D6. 인용은 세그먼트 기반에 `graph` 선택 필드를 더한다
- 1·2단계는 `RetrieverResourceData`의 필수 필드(`dataset_id`·`document_id`·`segment_id`)를 건드리지 않는다. 개념·개체 항목은 가중치가 가장 높은 언급 청크를 segment 인용으로 내고 선택 필드 `graph{graph_id, graph_name, entity_ids[]}`만 더한다. 번호표·중복 제거·웹 칩의 키는 `segment_id` 그대로다.
- `kind: segment | entity | report`, 자원 키(`seg:`/`ent:`/`rep:`), `segment_id` nullable화, 이벤트 봉투 스키마 버전 3, 웹 타입 변경은 4-G3에서 한꺼번에 하고 검토를 붙인다.

### D7. 단계 순서와 "어느 단계에서 멈춰도 완성품" 원칙
- 4-G1a(바탕·읽기 전용 지도, 1.25) → 4-G1b(연결·편집, 0.75) → 4-G1c(화면, 1) → 4-G2(LLM 추출·리미터·승격·global 경량, 1.5) → 4-G3(보고서·보고서 global·인용 확장, 1). 합계 5.5. 데이터 모델·index.db·설정 JSON은 4-G1a에서 전부 만들고 뒤 단계는 행과 설정값만 늘린다.
- 각 조각은 완료 기준(G1~G19)을 갖고 끝나며 1단계만으로 사용자의 세 요구가 충족된다. 4-G1은 4-1·4-2에 기대지 않아 4-1보다 앞에 둘 수 있다. design-reviewer 검토는 4-G1c·4-G2·4-G3 끝에 둔다.
- 전제: 3단계 완료(M29) 뒤. Kiwi 설치 승인, 지도 라이브러리 승인, 채팅 한도의 AI Studio 확인(4-G1a 전). 유료 키(D1 (b))는 선택이다.

## 기각한 대안
- 안 A 단독(완전판을 한 번에): 무료 키로는 문서 50개가 몇 달이고, 보고서는 전체 요약 질문에만 값을 하는데 그 비중을 모른다. D에서 추출(4-G2)과 보고서(4-G3)를 따로 켠다.
- 안 B 단독(추출판부터): 지도는 같은데 첫 결과까지 며칠 또는 유료 키가 들고 한국어 표기 흔들림을 처음부터 떠안는다.
- 안 C로 끝내기: 관계에 뜻이 없다. 승격으로 같은 그래프가 개체 그래프가 된다.
- GraphRAG·LightRAG 라이브러리를 그대로 쓰기: 서버 안은 의존성 25개·Parquet·RSS, 하위 프로세스는 자기 LLM 클라이언트·저장소를 전제해 `KeyPool`·리미터·`errors.classify`·사용량·유료 키 규칙을 우회한다. 방식만 가져오고 코드는 우리가 쓴다.
- KB의 청크 모드로 넣기(사용자 요구와 다르고 폼이 두 배), KB 밖 원본(저장·파싱·인용 두 벌), 그래프 DB 서버(ADR-0004), 서버 안 networkx·Kiwi(RSS·GIL), 브라우저만의 무리 계산(서버 쪽 무리·보고서·필터가 없어진다).
- 1단계에서 인용 필수 필드 완화와 자원 키(M24·M26 계약과 웹 타입이 바뀐다. 4-G3로 미룸), global 경량을 1단계에(무료에서 하루 몫을 비운다), 별칭의 `entity_id` FK(재생 때 끊긴다), 승격을 새 그래프로(id·인용·노드 설정이 끊긴다), bulk 무료 몫 고정 200(근거 없음)과 bulk 무료 금지(검토 A1·A2의 안. 사용자 결정 2026-11-04로 기각), 실제 429 뒤 손으로만 재개(몇 주짜리 작업에 비현실적).
- networkx Leiden 기본(3.5·3.6은 backend 전용, 3.7 본체는 첫 릴리스라 성능 미검증, 기본 `metric="cpm"`은 잘게 쪼개짐), react-force-graph·cytoscape·vis-network(주 스레드 힘 계산·WebGL 미리보기·크기), DRIFT(호출 수십 회), 보고서 전 층 기본(약 470회. 두 층 약 70회가 기본).

## 영향
- 마일스톤 4-G1a·4-G1b·4-G1c·4-G2·4-G3(설계 13장). 마이그레이션 0009(`graph_*` 11개 표, `llm_usage_day`, `llm_quota`, `agent_version.graphs`), index.db 스키마 버전 2. 4단계 선택지 문서 8절·10절은 오케스트레이터가 이 ADR에 맞춰 고친다.
- 신규 의존성: 서버 없음. 샌드박스 `kiwipiepy`(+모델)·`networkx>=3.7`·`numpy`(선택 `igraph`), 웹 `sigma`·`graphology`·`@react-sigma/core`(+louvain·forceatlas2, lazy). 새 폴더 `data/cache/graph-tools-<hash>/`, 작업 폴더 `data/kb-work/graph-<job>/`.
- 1·2·3단계 접점: 네 번째 레인 `graph`(`ShutdownCoordinator`·`/healthz`·복구·재개 검사), `KnowledgeSearchService.retrieve(candidate_unit_nos=)`, `ToolContext.graph`, `agent_tree`·`run_tier`에 그래프 등급, 문서 서비스 완료·삭제 지점의 stale 훅, `RetrieverResourceData.graph`(선택), `errors.classify`의 `quotaValue`, `ChatRequest.purpose`, 어댑터 성공 경로의 일일 카운트. 이벤트 스키마 버전 3은 4-G3에서.
- 설정 추가: `GRAPH_MAX_SOURCES`(5), `GRAPH_SUBPROCESS_MEMORY_MB`(768), `GRAPH_INSTALL_TIMEOUT_SEC`(600), `GRAPH_NLP_BATCH_CHARS`(500000), `GRAPH_CLUSTER_BACKEND`(networkx), `GRAPH_MAP_DEFAULT_NODES`(2000), `GRAPH_MAP_MAX_NODES`(10000), `GRAPH_MAP_MAX_JSON_MB`(4), `GRAPH_RETRIEVAL_CANDIDATES`(200), `GRAPH_EXTRACT_MODELS`(gemini-3.8-flash. Lite 확정 뒤 변경), `KB_WORK_MEMORY_MB`(1536), `RUNNER_MAX_CONCURRENT_GRAPH_JOBS`(1), `LLM_BULK_SHARE`(0.5), `LLM_BULK_RPM`(4), `LLM_BULK_RPM_PAID`(30), `LLM_FREE_RPD`(비움. 모델별 씨앗).
- 실측 뒤 바꿀 값: 채팅 RPD(`llm_quota`), Kiwi 사전 기본(`nlp.user_dict`), 무리 알고리즘 기본(Louvain → Leiden 또는 igraph), 추출 기본 모델(3.8 Flash → Lite), 지도 기본 노드 수.
