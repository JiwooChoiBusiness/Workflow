# 3단계 RAG·Text2SQL 설계 입력 조사

조사 기간: 2026-10-30 ~ 11-02. 목적: 3·4단계 설계의 근거 자료이며, 이 문서는 RAG와 Text2SQL을 다룬다. 이 PC: Windows 11, Core Ultra 7 155H, 여유 RAM 1.8~2.4GB, Python 3.12.8, SQLite 3.45.3. Dify 문서 페이지에는 날짜가 없어서 문서 버전은 Dify 1.18.0(2026-10-23) 시점으로 본다. 출처는 각 절 끝과 6절에 있다. 문서 속 '추천'은 설계 입력이지 확정된 결정이 아니다.

표기: [문서]는 공식 문서·API 명세, [릴리스]는 Dify GitHub 릴리스 노트, [실측]은 이 PC에서 직접 돌린 결과, [추정]은 문서 근거 없이 추론한 것, [일반 지식]은 출처를 확인하지 못한 일반 상식이다. Gemini 임베딩의 [실측]은 이 저장소의 키로 호출한 결과이고 `docs/research/gemini-api-2026-10.md`에 자세히 있다.

## 설계에 영향이 큰 결론

1. 벡터 저장소는 sqlite-vec 0.1.9(vec0, 브루트포스)를 별도 파일 `vectors.db`에 두는 안을 추천한다(2절). 768차원 코사인 k=10 지연은 1만 개 약 12ms, 10만 개 약 110ms(PC가 바쁘면 380ms)다 [실측]. 20만 청크를 넘으면 bit 양자화로 후보를 뽑고 float로 재순위한다. pre-1.0이라 인터페이스로 감싸 usearch나 numpy로 바꿀 수 있게 둔다.
2. 한국어 전문 검색은 FTS5만으로 풀린다(3절). KorQuAD(문단 10,621개, 질문 1,000개) MRR@10은 unicode61 원문 0.771, 음절 bigram bag 0.907, unicode61 prefix 색인 + 질의 쪽 어미 잘라내기 0.901, Kiwi 0.906이다 [실측]. Kiwi는 로드만으로 RSS가 +177~309MB 늘어 서버에 넣지 않는다.
3. 하이브리드 결합은 근거가 엇갈린다(3.4, 3.5절). 가중 RRF(k=60)를 기본 후보로 추천했으나, 한국어 합성 평가에서는 벡터 0.7~0.8의 점수 가중합(벡터 점수를 후보 안에서 min-max로 맞춘 경우. 코사인 원값을 그대로 쓰면 나빠진다)이 RRF보다 나았고 정규화 가중합이 낫다고 보고한 논문(arXiv 2210.11934)도 있다. 평가셋이 작으므로 설계에서 기본값을 정하되 실제 문서로 다시 재는 장치를 둔다.
4. Text2SQL은 샌드박스 하위 프로세스에서 읽기 전용으로 실행하고 서버는 검증만 한다(4절). SQLite `mode=ro`만으로는 `VACUUM INTO`(DB 전체를 다른 파일로 복사)와 `ATTACH`(다른 DB 파일 읽기)가 통과한다 [실측]. `query_only`, `setlimit(SQLITE_LIMIT_ATTACHED, 0)`, authorizer, 진행 핸들러를 겹쳐야 막힌다. sqlglot의 SELECT 전용 검사는 첫 관문일 뿐 유일한 방어가 될 수 없다.
5. Dify는 지금 지식 베이스가 두 종류다. 클래식은 기존 방식이고, Agent Knowledge Base는 1.18.0(2026-10-23) 베타로 별도 서비스(KnowledgeFS)와 DB가 필요하다. 3단계는 클래식을 따라 만들고 Agent KB는 범위 밖으로 두기를 권한다.
6. Dify 공식 문서에 없는 값이 많다. 청크 구분자·최대 길이·겹침 기본값, 가중치 점수 공식, 파이프라인에서 문서 하나가 실패했을 때의 처리, `knowledge_retrieval`의 `result` 항목 필드는 우리 쪽에서 정해야 한다.
7. Gemini 임베딩 주의점(자세한 내용은 `docs/research/gemini-api-2026-10.md`)
   - `gemini-embedding-2`는 `task_type`을 지원하지 않고 텍스트 접두(`task: search result | query: ...`, `title: ... | text: ...`)를 쓴다 [문서, https://ai.google.dev/gemini-api/docs/embeddings]. 한국어 합성 평가에서는 접두를 붙인 쪽이 나았다(3.5절). 입력 한도는 -2가 8,192토큰, -001이 2,048토큰이고 [문서] 넘으면 오류 없이 잘린다 [실측].
   - OpenAI 호환 `/embeddings`의 `input=[...]`은 항목별 벡터를 돌려주고 한 요청에 100개까지 된다 [실측]. 네이티브에서 -2에 여러 입력을 직접 넣으면 하나로 집계된다는 문서 설명은 호환 경로에 해당하지 않으므로, 한 입력에 한 요청으로 보낼 필요가 없다.
   - 호환 경로는 `dimensions`를 받지만 모르는 필드(`task_type` 등)는 400으로 거부한다. -001은 3072가 아닌 차원을 직접 L2 정규화해야 하고 -2는 모든 차원에서 노름이 1.0이다 [실측]. 두 모델의 벡터 공간이 달라 모델을 바꾸면 전부 재임베딩해야 한다 [문서]. 무료 등급 한도는 키(프로젝트)당 분당 100건(배치 항목 단위)이고 키 8개가 각자 따로 갖는다 [실측].

## 1. Dify 지식(RAG) 동작

각 절은 Dify의 동작을 먼저 적고, 우리 쪽 선택지는 '추천'으로 구분했다.

### 1.1 지식 베이스 설정, 색인, 검색

- KB 필드 [문서, API 명세 `POST /datasets`]: `name` 1~40자(워크스페이스 내 유일, 중복이면 409), `description` ≤400자, `indexing_technique`(`high_quality`|`economy`, 첫 문서를 넣을 때 확정), `embedding_model`(+provider), `retrieval_model`, `summary_index_setting{enable, model_name, model_provider_name, summary_prompt}`, `provider`(`vendor`|`external`). 응답에는 `doc_form`, `built_in_field_enabled`, `is_multimodal`, `enable_api` 등이 있다.
- 청크 모드(`doc_form`: `text_model`|`hierarchical_model`|`qa_model`)는 KB 생성 후 바꿀 수 없다. 구분자·최대 길이는 언제든 바꿀 수 있다.
- High Quality는 임베딩을 쓰고 Economical로 되돌릴 수 없다. Economical은 청크당 키워드 10개를 쓰고 토큰 소비가 0이며, 검색은 inverted index뿐이고 Top K만 설정된다. 나중에 High Quality로 올릴 수 있다.
- 검색 방식(High Quality): `semantic_search`(벡터), `full_text_search`, `hybrid_search`. API에는 Economical용 `keyword_search`도 있다.
- 기본값 [문서]: Top K 3, 점수 문턱 0.5, 리랭크 꺼짐. 허용 최대 Top K는 환경변수 `TOP_K_MAX_VALUE`=10이다. 점수 문턱이 기본으로 켜져 있는지는 문서에 불명이다. API는 `score_threshold_enabled`를 필수 필드로 둔다.
- 하이브리드는 두 검색을 동시에 돌려 결합한 뒤 둘 중 하나를 고른다(API `reranking_mode`: `weighted_score`|`reranking_model`).
  - Weight Settings: semantic 1이면 벡터만, keyword 1이면 전문 검색만, 사이 값은 비율 조절이다. API는 `weights{weight_type: semantic_first|keyword_first|customized, vector_setting{vector_weight}, keyword_setting{keyword_weight}}`다. 외부 키가 필요 없다.
  - Rerank Model: 외부 모델(Cohere, Jina 등)이다. 토큰이 소비되고, 있으면 Top K가 모델 입력 한도에 맞춰 자동 조정된다.
- 가중치 기본값은 문서에 없다. 공개 DSL 예제(version 0.1.5)는 `reranking_mode: weighted_score`, vector_weight 0.7, keyword_weight 0.3, top_k 6, score_threshold null을 쓴다. UI 기본값으로 보이지만 근거 문서는 없다 [추정].
- 두 층으로 동작한다. KB 단계 설정이 초기 후보 집합을 정하고, 노드/앱 단계 설정(Weighted Score·Rerank·Top K·문턱)이 여러 KB 결과를 합친 뒤 재정렬·절단한다. 복수 KB는 multi-path로 모두 동시에 검색해 합치고(N-to-1은 현행 문서에 없다), Weighted Score는 모든 KB가 High Quality이고 임베딩 모델이 같을 때만 쓸 수 있다. KB가 하나뿐이면 리랭크를 건너뛴다 [릴리스 1.11.3 PR 제목].
- 환경변수 [문서, https://docs.dify.ai/en/self-host/configuration/environments.md]: `INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH`=4000, `UPLOAD_FILE_SIZE_LIMIT`=15(MB), `UPLOAD_FILE_BATCH_LIMIT`=5, `ETL_TYPE`=dify(txt, md, pdf, html, xlsx, docx, csv. Unstructured를 켜면 doc, ppt, pptx, eml, epub 등 추가), `TENANT_ISOLATED_TASK_CONCURRENCY`=1(테넌트당 동시 색인 수).
- 임베딩 모델을 바꾸면 모든 청크를 다시 임베딩한다 [문서].

추천: 1인용이므로 `permission`·`partial_members`와 권한·태그는 빼고, `retrieval_model` 구조와 API 필드명은 Dify와 같게 둔다. 그래야 DSL 가져오기가 쉽다. 우리 환경에서 Economical은 "벡터 없이 FTS5(BM25)만 쓰는 색인"으로 구현하는 편이 낫다. Dify의 키워드 10개 추출 방식은 한국어에 맞지 않고 FTS5가 이를 대체한다 [추정]. 리랭크 모델은 Gemini 단독 환경에 없으므로 기본은 `weighted_score`로 하고 LLM 리랭크는 선택 기능으로 둔다 [추정].

출처: https://docs.dify.ai/en/self-host/use-dify/knowledge/create-knowledge/setting-indexing-methods.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/integrate-knowledge-within-application.md, https://docs.dify.ai/en/api-reference/knowledge-bases/create-an-empty-knowledge-base.md, 공개 DSL 예제 https://raw.githubusercontent.com/svcvit/Awesome-Dify-Workflow/main/DSL/Deep%20Researcher%20On%20Dify%20.yml (DSL version 0.1.5, 2025-02)

### 1.2 청크 (General, Parent-child, Q&A, 요약 인덱스, 멀티모달)

- General: 설정은 구분자, 최대 길이, 겹침(General만)이다. 구분자는 청크에서 제거되고, 최대 길이를 넘으면 구분자와 상관없이 강제로 자른다. 기본값은 현행 문서에 없다(예시로 `\n\n`, `\n`, 겹침 50자가 나올 뿐이다). API 기본값은 `separator`가 개행, `chunk_overlap` 0이다. 구문서 요약에는 500토큰이 있었으나 현행 문서에는 없고, 상한은 4000토큰(환경변수)이다. 길이 단위는 UI 문서가 문자, API 문서가 토큰이라고 적어 어긋난다.
- 전처리 규칙: ①연속 공백·개행·탭 정리(3개 이상 개행→2개, 여러 공백→1개, 탭·특수 공백→공백), ②URL·이메일 삭제(Full Doc에서는 무시). API에는 `remove_extra_spaces`, `remove_urls_emails`, `remove_stopwords`가 있다.
- Parent-child: High Quality 전용이다. 자식 청크로 매칭하고 부모 청크를 돌려준다. 부모 모드는 `paragraph`(구분자·최대 길이로 여러 부모)와 `full_doc`(문서 전체가 부모 1개, 앞 10,000토큰만 처리)다. 자식은 각 부모를 별도 구분자·최대 길이로 다시 자르며 기본값은 문서에 없다. 부모와 자식 구분자를 서로의 부분집합으로 두지 말라는 경고가 있다. 자식 편집은 부모를 갱신하지 않고, Full Doc 부모는 편집할 수 없다.
- Q&A: High Quality 전용이다. LLM으로 Q/A 쌍을 만들고(`doc_language` 기본 English) 질문↔질문으로 매칭한다. API는 `answer`가 필수다. 파이프라인의 "QA Chunk"는 스프레드시트의 질문 열·답 열 번호를 지정한다.
- 청크 요약 인덱스(1.12.0, 2026-02-03) [릴리스, 문서]: High Quality 전용이며 General·Parent-child 모두에서 쓴다. 청크마다 LLM이 요약을 만들어 따로 임베딩하고(벡터 메타에 `is_summary`, `original_chunk_id`), 요약이 맞으면 해당 청크를 돌려준다. 켜면 신규 문서·청크에만 적용되고 기존 문서는 "Generate Summary"를 실행하며 기존 요약을 덮어쓴다. 요약은 사람이 수정·삭제할 수 있고 여러 청크에 같은 요약을 주면 함께 반환된다. 비전 LLM이면 이미지까지 요약에 반영한다.
- 멀티모달(1.11.0, 2025-12-11) [릴리스, 문서]: 문서의 이미지를 자동 추출해 해당 텍스트 청크에 붙인다(JPG/PNG/GIF ≤2MB, 청크당 ≤10개, Markdown 이미지 링크 포함). 멀티모달 임베딩 모델(Vision 태그)이면 텍스트와 이미지를 함께 벡터화하고, 리랭크도 Vision 모델이 필요하다(아니면 이미지 결과가 탈락한다). 텍스트 전용 모델이면 이미지는 붙기만 하고 Vision LLM의 프롬프트에 들어간다. 노드의 질의 이미지는 KB 중 하나 이상이 멀티모달이어야 하며 기본 2MB 제한이다.

추천: 기본 프리셋은 문서에 없으므로 우리 값을 정한다. General은 구분자 `\n\n`, 최대 길이 1024토큰, 겹침 50. Parent-child는 부모 1024, 자식 구분자 `\n`·최대 길이 256 안팎 [추정]. Gemini 입력 한도(-001은 2,048토큰) 안에서 정하고, 한국어는 임베딩 토크나이저로 약 1.4자/토큰이라 토큰 수가 늘어나는 점을 고려한다 [실측]. 1차 범위는 General + Parent-child로 하고, Q&A와 요약 인덱스는 같은 인덱스 구조의 확장으로 두면 된다. Gemini embedding-2가 멀티모달이라 이미지 색인은 나중에 얹기 쉽다.

출처: https://docs.dify.ai/en/self-host/use-dify/knowledge/create-knowledge/chunking-and-cleaning-text.md, https://docs.dify.ai/en/api-reference/documents/create-document-by-text.md, 릴리스 https://github.com/langgenius/dify/releases (1.11.0 2025-12-11, 1.12.0 2026-02-03, 1.13.1 2026-03-17)

### 1.3 문서 상태, 청크 편집, 재색인, 검색 시험

- 문서 색인 상태 `indexing_status` [문서, API]: `waiting`→`parsing`→`cleaning`→`splitting`→`indexing`→`completed`, 또는 `error`, 또는 수동 `paused`. 단계별 시각(`processing_started_at`, `parsing_completed_at`, `completed_at` 등)과 `paused_at`, `stopped_at`, `error`, `completed_segments`/`total_segments` 필드가 있다.
- 목록의 표시 상태 `display_status`(필터 값): `queuing`, `indexing`, `paused`, `error`, `available`, `disabled`, `archived`. 문서는 "`indexing_status`와 `enabled`에서 파생"이라고만 적는다. 매핑은 waiting→queuing, parsing~indexing→indexing, completed & enabled→available, completed & !enabled→disabled, `archived`→archived로 보면 된다 [추정].
- 문서 조작: enable/disable/archive/unarchive(`PATCH /datasets/{id}/documents/status/{action}`, 본문 `document_ids`, 아직 색인 중이면 400). Archive는 읽기 전용으로 만들고 언제든 복원할 수 있다. 삭제는 되돌릴 수 없고 Rename도 가능하다.
- 청크 조작: 수동 추가(일괄 추가 최대 1000개/요청), 편집(편집됨 표시), 켜기/끄기(꺼진 청크는 편집 불가, 검색에서 제외), 삭제, Parent-child의 자식 추가·수정·삭제(API에서 만든 자식은 `customized`, 색인이 만든 자식은 `automatic`). Economical은 청크 키워드를 직접 편집할 수 있다(≤10개). 청크 다시 켜기는 벡터를 복원하며 색인이 끝나지 않은 청크는 거부한다 [릴리스 1.18.0].
- 재색인 트리거: 임베딩 모델 변경(전체 재임베딩), 문서 업데이트(`update-by-text`/`update-by-file`, 색인 재시작), 문서별 청크 설정 수정, 요약 재생성. 재색인 버튼의 정확한 동작은 클래식 문서에 없다.
- 검색 시험(hit testing): 화면에서 검색 설정을 임시로 바꿔 시험한다(저장되지 않음). Records에 시험과 실제 앱 호출이 모두 기록된다. 시험과 일반 검색은 같은 API `POST /datasets/{id}/retrieve`를 쓰고 질의는 최대 250자다. 응답 `records[]`에는 `score`, `segment`(내용, `answer`, `word_count`, `tokens`, `keywords`, `hit_count`, `enabled`, `status`, `document`), `child_chunks[]`, `files[]`, `summary`, `tsne_position`이 있다.

추천: 상태를 Dify와 같은 이름으로 저장하고 파생 규칙은 코드로 만든다. 문서 단위로 독립 상태를 갖게 해야 한 문서가 실패해도 나머지에 영향이 없다. 청크 켜기/끄기는 벡터 쪽에서도 삭제·복원하는 편이 낫다(2.3절 실측: 메타데이터 열 필터는 속도 이득이 없다).

출처: https://docs.dify.ai/en/api-reference/documents/list-documents.md, https://docs.dify.ai/en/api-reference/documents/get-document-indexing-status.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/manage-knowledge/maintain-knowledge-documents.md, https://docs.dify.ai/en/api-reference/knowledge-bases/retrieve-chunks-from-a-knowledge-base-test-retrieval.md, https://docs.dify.ai/en/api-reference/chunks/create-chunks.md

### 1.4 메타데이터와 `knowledge_retrieval`의 메타데이터 필터 [문서]

- 필드 타입은 String, Number, Time이다. 필드 이름 규칙은 소문자·숫자·밑줄이다. 필드는 KB 단위이고 값은 문서 단위다. 필드 타입은 바꿀 수 없고 이름만 바꿀 수 있다. 필드를 지우면 모든 문서에서 값이 사라진다.
- 기본(built-in) 필드 5개는 시스템이 채운다. `document_name`(string), `uploader`(string), `upload_date`(time), `last_update_date`(time), `source`(string). 기본은 꺼짐이고, 켜면 기존·신규 문서에 적용되며 수정할 수 없다.
- 필터 모드는 Disabled(기본), Automatic, Manual이다. Automatic은 모델을 선택해야 하고, 질의 변수로부터 모델이 조건을 만들며 결정하지 못하면 필터 없이 검색한다. Manual은 조건 목록이다. 값은 변수 또는 상수(Time은 상수만), 조건 사이는 AND/OR이다. 복수 KB에서는 모든 KB에 같은 이름·타입으로 있는 필드만 쓸 수 있다. 값 비교는 대소문자를 구분하고 정확 일치다.
- 연산자(UI): String은 is, is not, contains, not contains, starts with, ends with, is empty, is not empty. Number는 =, ≠, >, <, ≥, ≤, is empty, is not empty. Time은 is, before, after, is empty, is not empty. API(`comparison_operator`)는 `in`, `not in`이 더해지고 `logical_operator` 기본은 `and`이며, `empty`/`not empty`는 value를 생략한다.
- 메타데이터는 외부 시스템 ID를 문서에 붙여 두는 안정 키로도 쓸 수 있다고 Dify 가이드가 설명한다.

추천: 조건 모델을 위 연산자 집합으로 만들고 Time 필드는 ISO 문자열로 저장해 비교한다. Automatic 모드는 질의→조건 JSON을 LLM이 내는 구조화 출력으로 구현하면 된다(2단계 `parameter_extractor` 재사용).

출처: https://docs.dify.ai/en/self-host/use-dify/knowledge/metadata.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/integrate-knowledge-within-application.md, https://docs.dify.ai/en/cloud/use-dify/nodes/knowledge-retrieval-v2.md

### 1.5 Knowledge Pipeline

- GA는 1.9.0(2025-09-22)이다 [릴리스]. 구조는 Data Source → Data Processing(Extractor, Chunker) → Knowledge Base 노드 → User Input Field → Test & Publish다. 기존 KB를 파이프라인 구조로 한 번에 옮기는 기능도 1.9.0에 있다.
- 데이터소스는 File Upload(확장자 선택 가능, 업로드 기본 20개, 파일당 15MB), Online Document(Notion 등), Web Crawler(Jina Reader, Firecrawl), Online Drive(Google Drive, Dropbox, OneDrive) 4종이다 [문서]. Extractor는 Doc Extractor, Dify Extractor(이미지 추출·URL 반환), Unstructured(마켓플레이스)이고, Chunker는 General, Parent-child, QA Chunk(질문·답 열 번호)다.
- Knowledge Base 노드: 입력 변수(청크 구조에 따라 `Array[Chunk]`, `Array[ParentChunk]`, `Array[QAChunk]`), Chunk Structure, Index Method(High Quality: 임베딩 모델 / Economical: 키워드 수 기본 10), Retrieval Settings, Summary Auto-Gen. Parent-child와 Q&A는 High Quality만 가능하고, 게시 후에는 청크 구조를 바꿀 수 없다. 청크 구조가 멀티모달이면 `multimodal-General`/`multimodal-Parent-Child` 방언이 있다.
- 내장 템플릿은 General Mode-ECO, Parent-child-HQ, Simple Q&A, LLM Generated Q&A, Convert to Markdown(Office 파일용, PDF 비권장) 5종이고 게시한 파이프라인은 Customized 템플릿이 된다 [문서]. 게시 전에는 Test Run을 파일 1개로 돌리고, 게시 후에는 "Add Documents"에서 "Save & Process"로 처리한다. 서비스 API는 `POST /datasets/{id}/pipeline/run`(본문 `inputs`, `datasource_type`(`local_file`|`online_document`|`website_crawl`|`online_drive`), `datasource_info_list`, `start_node_id`, `is_published`, `response_mode`)이고 파일은 `POST /datasets/pipeline/file-upload`로 먼저 올린다.
- 문서 하나가 실패했을 때의 처리, 파이프라인 재실행 방식은 문서에 없다. 클래식 문서 색인은 배치 상태 API가 문서별 `indexing_status`/`error`를 따로 돌려준다.

추천: 2단계 워크플로우 엔진 위에 "파이프라인 그래프"를 얹는 구성이 가장 일관된다. 노드 타입은 `ds_file`, `extract`, `chunk_general`, `chunk_parent_child`, `kb_write`처럼 두고, 문서 1개를 1 런으로 돌려 상태·오류·재시도를 문서 단위로 둔다 [추정]. 청크 구조는 KB 생성 시 고정하고, 게시 뒤 변경은 새 KB 생성으로 안내한다(Dify와 같다). 캔버스 편집은 뒤로 미루고 고정 5단계 프리셋(추출→정리→청크→임베딩→색인)부터 시작해도 된다.

출처: https://docs.dify.ai/en/self-host/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/knowledge-pipeline/create-knowledge-pipeline.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/knowledge-pipeline/publish-knowledge-pipeline.md, https://docs.dify.ai/en/api-reference/knowledge-pipeline/run-pipeline.md, 릴리스 1.9.0

### 1.6 `knowledge_retrieval` 노드, LLM 컨텍스트, 인용

- 입력 [문서]: Query Text(텍스트 변수, 챗플로우는 `sys.query`), Query Images(선택), 지식 베이스 여러 개, 검색 설정(Weighted Score/Rerank/Top K/점수 문턱), 메타데이터 필터.
- 출력은 변수 `result` 하나다. "검색된 문서 청크 배열"이며 항목마다 content, metadata, title 등이 있다고만 문서에 적혀 있다. 이미지가 있으면 `files`가 추가된다. 항목 필드의 정확한 이름은 문서에 없다.
- 정확한 필드 구성은 인용 API의 `retriever_resources` 스키마가 가장 가깝다 [문서 `RetrieverResource`, 20개 필드]: `id`, `message_id`, `position`, `dataset_id`, `dataset_name`, `document_id`, `document_name`, `data_source_type`, `segment_id`, `segment_position`, `score`, `hit_count`, `word_count`, `index_node_hash`, `content`, `summary`, `title`(외부 KB), `doc_metadata`, `files`, `created_at`.
- LLM 노드에서는 "Context" 필드에 `result`를 고르고 시스템 지시에 Context 변수를 넣는다. 결과가 비었을 때 답변을 막으려면 IF/ELSE로 `result` 비어 있음을 검사하라고 Dify가 안내한다(Agent KB 문서).
- 인용 표시: 챗플로우는 기본으로 켜져 있다(Features → "Citation and Attributions"). API는 블로킹 응답의 `metadata.retriever_resources`, 스트리밍의 `message_end` 이벤트 `metadata`, 대화 목록 메시지 항목의 `retriever_resources`에 `RetrieverResource` 배열을 싣고, `/parameters` 응답에는 `retriever_resource.enabled`가 있다.
- 공개 DSL 예제(version 0.1.0, 2024-07 / 0.1.5, 2025-02)의 `knowledge-retrieval` 노드는 `dataset_ids`, `retrieval_mode`(single|multiple), `single_retrieval_config.model`, `multiple_retrieval_config{top_k, score_threshold, reranking_*, weights}`, `query_variable_selector`를 쓴다. 메타데이터 필터 필드(`metadata_filtering_*`)와 `query_attachment_selector`는 공개 예제에서 확인하지 못했다.

추천: 우리 `knowledge_retrieval` 출력은 `result: array[{content, title, url?, icon?, metadata{score, dataset_id, dataset_name, document_id, document_name, data_source_type, segment_id, segment_position, hit_count, word_count, doc_metadata, summary?, files?}}]`로 하고, 보조로 `result_text`(content를 이어 붙인 문자열)를 둔다 [추정]. `RetrieverResource`의 필드명을 그대로 쓰면 챗플로우 인용 API와 맞는다. DSL 가져오기에서는 `dataset_ids`를 이 PC의 KB로 연결할 수 없으므로(1.7절) 노드는 `kb_ids` 빈 값으로 들여오고 경고를 남긴다. 저장소 접점으로 `docs/design/phase2-workflow.md:110`과 `:152`가 `knowledge_retrieval` 자리를 예약했고, `:499`는 Dify `knowledge-retrieval`을 `unsupported`로 가져온다.

출처: https://docs.dify.ai/en/self-host/use-dify/nodes/knowledge-retrieval.md, https://docs.dify.ai/en/api-reference/chat-messages/send-chat-message.md, https://docs.dify.ai/en/api-reference/openapi_service.json, 공개 DSL 예제 https://raw.githubusercontent.com/svcvit/Awesome-Dify-Workflow/main/DSL/Document_chat_template.yml

### 1.7 에이전트 앱, 서비스 API, External Knowledge, DSL

- 에이전트 앱 연결: 레거시 Agent 앱·Chatbot은 앱 설정의 Context에서 KB를 추가한다. Agent가 KB를 도구처럼 부르는지는 문서에 없다. 검색 설정은 앱 단위로 둔다. 새 Agent는 "Agent Knowledge Base"를 붙이면 에이전트가 KB 이름·설명을 보고 스스로 검색 시점을 정하고, 깊이는 항상 Fast다 [문서].
- 서비스 API(클래식) [문서 OpenAPI, 경로]: `/datasets` 아래에 KB(`GET/POST /datasets`, `GET/PATCH/DELETE /datasets/{id}`), 검색(`POST /datasets/{id}/retrieve`), 문서(`document/create-by-text|create-by-file`, `documents/{doc}/update-by-text|update-by-file`, `documents/{batch}/indexing-status`, `documents/status/{action}`), 청크(`documents/{doc}/segments`, `.../segments/{seg}`, `.../child_chunks`), 메타데이터(`/metadata`, `/metadata/built-in`, `/metadata/built-in/{action}`, `documents/metadata`), 태그(`/datasets/tags*`), 파이프라인(`pipeline/run`, `pipeline/datasource-plugins`)을 둔다. 인증은 Bearer 키이며, 1.17.1부터 KB 단위로 범위를 제한할 수 있다(그전에는 워크스페이스 전체). 워크스페이스당 키 10개, 범위는 나중에 바꿀 수 없다.
- External Knowledge API는 Dify가 `POST {endpoint}/retrieval`을 호출하는 방식이다. 요청은 `knowledge_id`, `query`, `retrieval_setting{top_k, score_threshold}`, 응답은 `{"records":[{"content","score"(0~1),"title","metadata"}]}`이고, 호출은 SSRF 프록시를 거친다.
- DSL에서 지식이 빠지는 규칙 [문서]: 앱 내보내기에 연결된 KB 자체는 포함되지 않는다. `DSL_EXPORT_ENCRYPT_DATASET_ID=true`(기본)이면 내보낸 DSL의 dataset ID를 난독화하고, false면 원본 ID를 써서 환경 간 이동이 쉬워진다. 서비스 API 키, 모델·도구 자격증명, 로그·주석·과거 버전도 제외된다. 앱 패키지(`.ifpkg`)는 1.18.0 이상에서만 가져올 수 있다.

추천: 우리 DSL(`dsl_version`)에도 KB 참조는 `kb_slug`로 쓰고, 가져올 때 slug로 찾는다. 2단계 `agent_slug` 규칙(`docs/design/phase2-workflow.md:487`)과 같은 방식이다.

출처: https://docs.dify.ai/en/api-reference/openapi_service.json, https://docs.dify.ai/en/api-reference/guides/knowledge-base-api.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/external-knowledge-api.md, https://docs.dify.ai/en/self-host/use-dify/knowledge/connect-external-knowledge-base.md, https://docs.dify.ai/en/cloud/use-dify/workspace/export-import-apps.md, https://docs.dify.ai/en/cloud/use-dify/knowledge-base/use-knowledge-in-apps.md

## 2. 로컬 벡터 검색 (Windows, 단일 프로세스, RSS 300MB)

### 2.1 sqlite-vec 현황 [문서·릴리스·PyPI]

- 안정판 v0.1.9(2026-03-31, DELETE 버그 수정). 알파 0.1.10-alpha.4(2026-05-18). GitHub 마지막 push 2026-05-18, 열린 이슈 245개. 라이선스 Apache-2.0/MIT 이중.
- README에 "pre-v1, 호환성 깨짐을 예상하라"가 있다. v0.1.7(2026-03-17) 노트는 "오랜 중단 끝에 개발을 재개했고 Mozilla가 후원한다"고 적는다. 직전 안정판은 0.1.6(2024-11)이었다. 0.1.7에서 DELETE 시 공간 회수와 거리 열의 `>`/`<` 제약이 들어갔고, 0.1.10 알파에 ANN 인덱스 `rescore`, `ivf`(실험, 기본 빌드에서 꺼짐), `diskann`이 있다. 모두 알파 단계다.
- Windows x64 휠: `sqlite_vec-0.1.9-py3-none-win_amd64.whl` 0.29MB(`vec0.dll` 289KB), 로드는 `enable_load_extension` + `sqlite_vec.load`.
- sqlite-vec 저장소의 Makefile은 Darwin/Linux에만 AVX·NEON 플래그를 주고 Windows에는 SIMD 분기가 없다. [실측] `vec_debug()`의 `Build flags:`가 빈 값이다. 즉 Windows 휠은 스칼라 빌드다.

### 2.2 vec0 기능 [문서]

- 벡터 타입 `float[N]`, `int8[N]`, `bit[N]`. 거리 `distance_metric=cosine` 또는 기본 L2.
- 열 종류(v0.1.6): 메타데이터 열(최대 16개, boolean/integer/float/text, 연산자 `=`, `!=`, `>`, `>=`, `<`, `<=`, INTEGER·TEXT는 `IN`도 가능(SQLite ≥3.38)), 파티션 키(최대 4개, TEXT/INTEGER, `=`로 사전 필터, `IN`은 미지원, 값마다 수백 개 이상 벡터를 권장), 보조 열(`+col`, 비색인, KNN WHERE에서 사용 불가).
- KNN 문법 `where emb match ? and k = 10`. [실측] k 상한은 4096이다(4097부터 확장 자체가 오류를 낸다). `rowid IN (서브쿼리)`로 KNN을 거는 것도 동작한다(10만 개에서 10% 후보, 78ms vs 필터 없음 110ms).
- 양자화 [문서·릴리스 PR]: `vec_quantize_int8`, `vec_quantize_binary`. 0.1.10 알파의 `rescore` 인덱스(bit 또는 int8로 거칠게 찾고 float로 재순위)가 이 방식을 SQL에 내장한 것이다.

### 2.3 성능과 메모리

공개 수치 [문서·블로그]
- 안정판 블로그(2024-08): 10만 개 float 1024·768차원이 75ms 미만, 3072차원 214ms. 100만 개는 float가 100ms 목표를 넘고(3072차원 8.52s) bit는 124ms다.
- ANN PR(2026-03-31, MacBook Pro M4, 뉴욕타임스 헤드라인 100만 개, 1024차원):

| 구성 | 질의 | recall@10 |
|---|---|---|
| flat | 590ms | 1.0 |
| rescore int8 | 225ms | 1.0 |
| rescore bit | 92~101ms | 0.962~0.988 |
| IVF | 56ms | 0.978~0.990 |
| DiskANN | 4.6~34ms | 0.954~0.980 |

[실측] 이 PC, sqlite-vec 0.1.9 Windows 휠, 768차원 float32, 코사인, k=10, 미리 만든 파일, mmap 끔:

| 규모·형식 | 질의 중앙값 | 파일 | 비고 |
|---|---|---|---|
| 1만 float, 64KB 페이지 | 약 12ms | 32MB | 4KB 페이지에서는 약 90ms |
| 10만 float, 64KB 페이지 | 109ms (PC가 바쁠 때 380ms) | 312MB | 4KB 페이지 234ms |
| 10만 float, mmap 켬 | 67~225ms | | RSS 347~390MB |
| 10만 int8 | 99~105ms | 81MB | float와 큰 차이 없음 |
| 10만 bit | 3.3ms | 16MB | |
| 10만 + 파티션 키(kb 5값) 1/5 필터 | 72ms (필터 없음 384ms) | | 삽입 속도 1/3(1.4k vs 4.2k vec/s) |
| 10만 + boolean 메타데이터 필터 | 필터 없음과 같음(370 vs 375ms) | | 속도 이득 없음 |

- 서버 RSS 영향: mmap을 끄면 측정 RSS가 76MB(numpy 포함)로 바깥 베이스라인과 거의 같다. mmap을 켜면 파일 크기만큼 늘어난다(Windows RSS는 작업 집합이라 매핑된 페이지가 들어간다).
- 삽입 처리량은 벡터만 있는 vec0에서 1만 개 기준 약 1.9만 vec/s, 10만 개에서는 4~5k vec/s다. 실제 병목은 Gemini 임베딩 호출 속도일 것이다. 무료 등급 한도가 키당 분당 100건이다 [실측].
- 50만 개와 100만 개는 RAM 여유 때문에 직접 재지 않았다. 선형으로 늘리면 50만 float는 약 0.55s(조용할 때)~1.9s(부하 때) [추정], bit는 약 17ms다.
- 용량 계산: 768차원 float32는 3,072B/벡터라 10만 개 307MB, 50만 개 1.5GB다. 3072차원(Gemini 기본)이면 4배다. `dimensions: 768` 유지를 권한다(gemini-embedding-001 MTEB: 768 → 67.99, 1536 → 68.17). 한국어 합성 평가에서도 768과 3072의 차이는 R@1 한 건이었다(3.5절).

### 2.4 같은 DB 파일 vs 따로

- 따로 두는 쪽을 추천한다(`data/vectors.db`). 이유:
  - 페이지 크기는 파일 속성이다. vec0는 64KB가 빠르지만 일반 테이블에는 4KB가 낫고, 기존 app.db를 바꾸려면 VACUUM이 필요하다.
  - 쓰기 잠금이 분리되어 대량 색인 중에도 app.db(런, 메시지)가 막히지 않는다. 임베딩 모델을 바꾸면 이 파일만 새로 만들면 된다.
  - 확장 미로드 연결도 app.db를 문제없이 연다. vec0 테이블이 있는 DB를 확장 없이 열면 그 테이블 조회만 `no such module: vec0`로 실패한다 [실측]. Alembic이 app.db를 건드릴 때 걸리지 않는다.
  - [문서] WAL에서는 긴 읽기 트랜잭션이 체크포인트를 막고 WAL이 커진다(https://www.sqlite.org/wal.html). 벡터 DB로 격리하면 KNN 스캔(100~400ms)이 app.db WAL에 영향을 주지 않는다.
- 같은 파일에 두면 한 트랜잭션으로 묶을 수 있다는 장점이 있다. [문서] 여러 ATTACH 파일에 걸친 WAL 트랜잭션은 파일별로는 원자적이지만 전체로는 원자적이지 않다. 따로 두면 청크 삭제·벡터 삭제 사이에 어긋날 수 있어 청크에 `index_state`를 두고 고아 정리 작업이 필요하다.
- 백업 [실측]: `sqlite3.Connection.backup()`과 `VACUUM INTO` 모두 확장을 로드하지 않은 채로 동작했고 복사본에서 KNN도 정상이었다. 백업은 사용 중에도 가능하지만 쓰기가 끼면 다시 시작될 수 있다 [문서 https://www.sqlite.org/backup.html]. 벡터는 파생 데이터지만 재생성에 API 호출이 들므로 백업 대상에 넣는다.
- SQLAlchemy 2 async + aiosqlite(2.1.6, 0.22.1)에서는 `event.listens_for(engine.sync_engine, "connect")` 안에서 `dbapi_conn.run_async(lambda c: c.enable_load_extension(True))` → `c.load_extension(sqlite_vec.loadable_path())` → `enable_load_extension(False)`로 로드했다 [실측].

### 2.5 대안 (Windows 3.12, 이 PC 기준)

| 후보 | 크기·의존성 | 메모리·동작 | 판단 |
|---|---|---|---|
| sqlite-vec | 휠 0.29MB, 의존성 없음 | 파일 기반 스캔, RSS 증가 거의 없음 | 추천 |
| usearch 2.26.6 | 휠 0.35MB(numpy, tqdm, numkong) | HNSW. [실측] 10만×768 f16: 질의 3~6ms, 파일 168MB, RSS +160~180MB. 빌드는 f16이 느려 8스레드 257s(i8 113s, 단일 스레드 f16 39 vec/s). f32는 8스레드 2.3k vec/s이지만 메모리가 약 330MB. i8은 합성 데이터 recall 0.84 | 서버 밖 프로세스에서 쓴다면 선택지. 인덱스를 통째로 저장하고 별도 일관성 관리가 필요 |

나머지 후보는 모두 비추천이다. hnswlib 0.8.0은 PyPI에 sdist만 있어(Windows 휠 없음, 2023-12 이후 갱신 없음) 컴파일러가 필요하다. DuckDB VSS(DuckDB 2.0.0, 휠 15.4MB)는 문서상 실험적이고 영속화는 WAL 복구 미구현으로 손상될 수 있다. LanceDB 0.41.0(Windows 휠 85.6MB + pyarrow 28.6MB)은 import만으로 RSS +79MB, 2.1s였다 [실측]. Chroma 1.5.9는 onnxruntime(14.9MB), grpcio, kubernetes 등 의존성이 무겁다.

### 2.6 추천

- 기본: sqlite-vec vec0, 별도 `vectors.db`, `page_size=65536`(첫 테이블 만들기 전에 설정), WAL, `synchronous=NORMAL`, mmap 끔.
- KB 필터는 KB당 vec0 테이블 하나(삭제는 DROP, 여러 KB 검색은 결과 병합)나 `kb_id` 파티션 키 중 고른다. 파티션 키는 필터가 걸린 KNN을 빠르게 하지만 삽입이 3배 느려진다. boolean 메타데이터 열은 속도 이득이 없다.
- 상한: 10만 청크까지는 순수 float 브루트포스(100~400ms). 20만을 넘으면 `bit[768]`를 별도 vec0에 같이 넣고 hamming으로 후보 100~200개를 뽑은 뒤 float 벡터로 재순위한다. Gemini 임베딩은 bit 양자화 학습 모델이 아니므로 recall은 실데이터로 확인해야 한다. 공개 수치(위 표)는 양자화에 맞춰 학습된 mxbai 모델이다.
- `VectorIndex` 인터페이스(add, delete, search(vec, k, filter))로 감싸 usearch나 numpy 구현으로 바꿀 수 있게 한다. sqlite-vec의 유지 보수 위험(공백 이력, pre-1.0)이 이유다.

## 3. 한국어 전문 검색과 하이브리드

### 3.1 KorQuAD 실험 [실측]

말뭉치는 KorQuAD 1.0 train+dev의 문단 10,621개(평균 518자, 총 550만 자)이고, 질문은 dev에서 1,000개다. 정답은 질문이 속한 문단이다. 점수는 FTS5 `bm25` 순위, 상위 10개다. 데이터 출처는 https://korquad.github.io/dataset/KorQuAD_v1.0_train.json, `..._dev.json`이다.

| 방식 | MRR@10 | R@1 | R@10 | 질의 중앙값 | DB 크기 |
|---|---|---|---|---|---|
| unicode61 원문, 단어 OR | 0.771 | 0.708 | 0.890 | 0.7ms | 27.7MB |
| trigram, 질의를 trigram으로 쪼개 OR | 0.822 | 0.755 | 0.941 | 1.9ms | 46.0MB |
| 음절 bigram 열(unicode61), 질의 bigram OR | 0.907 | 0.858 | 0.980 | 17.7~21.7ms | 37.7MB |
| 위 방식 + 질의의 df>5% bigram 제거 | 0.900 | 0.855 | 0.973 | 4.8ms | 〃 |
| 위 방식 + df>20% 제거 / df>2% 제거 | 0.906 / 0.862 | | 0.981 / 0.960 | 10.4 / 2.4ms | |
| 공백 무시 bigram(띄어쓰기 달라도 이어 붙임) | 0.917 | 0.876 | 0.982 | 25.1ms | 53.8MB |
| unicode61 + `prefix='2 3'`, 질의 `단어*` | 0.788 | | | 3.1ms | 35.7MB |
| 위 + `단어*`, `단어[:-1]*`(어미 1자 제거) | 0.890 | 0.842 | 0.966 | 5.4ms | |
| 위 + `단어[:-2]*`도 추가 | 0.901 | 0.855 | 0.973 | 5.7ms | |
| Kiwi 0.24.1 명사·용언어간 토큰, 질의도 Kiwi | 0.906 | 0.858 | 0.978 | 9.2ms | 18.7MB |

- 색인 만드는 시간은 bigram 5.8s, trigram 6.2s, Kiwi 108s(`num_workers=4`)다.
- FTS5 contentless(`content='', contentless_delete=1`, SQLite ≥3.43)로 bigram 색인이 37.7MB에서 13.9MB로 줄었다(`detail=column`이면 12.1MB, 단 phrase 질의 불가). 이 모드에서도 delete/update가 동작했다. 질의 변환 문자열만 저장하면 되므로 청크 원문은 app.db에 두면 된다.
- 예문 시험(문서 5개): "회의"는 unicode61 단어 일치가 없고 `회의*`는 [3,1,2], bigram은 [3,4,5,1,2](4번 "학회의"는 부분 문자열 일치), LIKE는 5개 전부였다. "회의록"은 unicode61 단어 일치가 없고 `회의록*`는 [1], trigram은 [5,1], bigram OR은 [5,1,3,4,2](정확히 맞는 5, 1이 앞)였다.
- LIKE 전체 스캔은 550만 자에서 30ms였다. trigram FTS 테이블도 2자 LIKE는 스캔으로 떨어진다(33ms). 3자 이상 LIKE는 색인을 탄다(3.1ms) [문서: 3자 미만 전문 질의는 아무 행도 맞추지 못한다].

### 3.2 BM25에 주는 영향

- bigram이 토큰이 되므로 문서 길이(avgdl)는 글자 수 기준이다. 긴 단어는 맞는 bigram 수가 많아 점수가 더 커진다(5자 단어는 4개, 2자 단어는 1개). 흔한 bigram(하는, 에서 등)은 IDF가 자연스럽게 낮아져 불용어 역할을 한다.
- 단점은 단어 경계를 넘는 우연한 일치("학회의"에 "회의")와 질의 bigram 수가 많아 posting list가 길어지는 것이다. 질의에서 문서 빈도가 높은 bigram을 버리면(df>5%) 정확도 손실 없이 4배 빨라진다. 말뭉치가 10배 커지면 지연도 늘 수 있어 [추정] 주기적으로 `fts5vocab`으로 df 상위 bigram 목록을 갱신한다.
- FTS5의 `bm25()` 상수는 k1=1.2, b=0.75로 고정이고 열 가중치(`bm25(f, 10.0, 1.0)`)만 바꿀 수 있다 [문서 https://www.sqlite.org/fts5.html]. 유니코드 정규화(NFC)를 색인·질의 양쪽에 거는 것을 권한다 [추정, 미시험].

### 3.3 kiwipiepy (형태소 분석기)

- kiwipiepy 0.24.1(PyPI, 2026-10-20, Windows 휠 3.72MB)은 모델을 별도 패키지 kiwipiepy_model 0.24.0(sdist만, 87.9MB)로 받고 설치 후 105MB다. 라이선스는 현재 Apache-2.0이고 0.23.2 이하는 LGPL이다 [문서 https://github.com/bab2min/kiwipiepy, https://github.com/bab2min/Kiwi].
- [실측] 기본 로드 `Kiwi()`는 약 0.7~1.1s, RSS +308~309MB다. 사전 3종(`load_default_dict`, `load_typo_dict`, `load_multi_dict`)을 모두 끄면 +177MB이고, 토큰화 시 RSS는 최대 598MB까지 올랐다. 속도는 단일 스레드 약 45k자/s, 질의 토큰화 약 0.9ms/건이다.
- 판단: 서버 RSS 300MB 예산(1단계 측정 유휴 108MB)과 양립하지 않는다. 별도 프로세스에서 돌려도 질의 때마다 필요해 상주해야 하고, 결과(0.906)는 bigram(0.907)과 같다. bigram으로 충분하다.

### 3.4 하이브리드 결합: RRF vs 정규화 가중합, Dify 방식

- RRF: 점수 대신 순위를 써서 `Σ w_i/(k + rank_i)`, k=60이다. Azure AI Search가 쓰며 "k를 60 같은 작은 값으로 두면 가장 잘 동작한다"고 설명한다 [문서, ms.date 2026-06-08 https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking]. 장점은 BM25(FTS5는 음수)와 코사인의 척도 차이가 문제되지 않고 튜닝 데이터가 필요 없다는 것이다. 단점은 점수 크기 정보를 버리고, 각 목록의 후보 깊이와 k에 결과가 민감하다는 점이다. Alex Garcia의 SQL 예제는 FTS5 + sqlite-vec RRF를 가중치(`weight_fts`, `weight_vec`)와 함께 보여 준다(https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html, 2024).
- 정규화 가중합(convex combination): 점수를 질의별 min-max 등으로 맞춘 뒤 `α·벡터 + (1-α)·키워드`로 합친다. 연구(https://arxiv.org/abs/2210.11934, v2 2023-05)는 이 방식이 도메인 내외에서 RRF보다 낫고 RRF가 매개변수에 민감하며, 파라미터 하나를 적은 예시로 조정할 수 있다고 보고한다(정규화 방식의 영향은 작다). 단점은 정규화가 필요하고 이상치에 취약하다는 점이다.
- Dify [문서]: 검색 설정에서 "Weight Settings"(semantic ↔ keyword 슬라이더)나 "Rerank Model" 중 하나를 고르며, 가중치 점수의 계산식은 공개 문서에 없다. 공개 DSL 예제의 가중치는 vector 0.7, keyword 0.3이고, 점수 문턱 기본값은 0.5(문서)다. 키워드 쪽 점수가 DB의 BM25인지 앱 안에서 다시 계산한 것인지는 확인하지 못했다.

추천: 가중 RRF(k=60, 각 목록 상위 50개 안팎)를 기본 후보로 하고 UI 슬라이더를 `w_vec`, `w_fts`에 매핑한다. 점수 문턱은 벡터 목록에서 코사인 유사도 기준으로 먼저 적용한다. 정규화 가중합은 `weights.weight_type=customized`의 대체 구현으로 남겨 둔다. 다만 근거가 엇갈린다. 위 논문은 가중합이 낫다고 보고하고, 3.5절의 한국어 합성 평가에서도 벡터 0.7~0.8의 점수 가중합이 RRF보다 나았다. 평가셋이 작으므로 설계에서 기본값을 정하되 실제 문서로 다시 재는 장치를 둔다.

한국어 전문 검색 추천: 청크마다 bigram 변환 문자열을 contentless FTS5(`unicode61`)에 넣고 질의는 bigram OR 가방 + df>5% 제거로 한다. 별도 변환 없이 시작하려면 unicode61 + `prefix='2 3'`에 질의 쪽에서 어미를 1~2자 잘라 `단어*`로 확장하는 방법도 거의 같다(0.901). 이 방법은 합성어 안쪽 일치("주간회의록"에서 "회의")를 못 찾는다. 1글자 단어 질의는 두 방식 모두 약하다.

### 3.5 한국어 합성 평가셋: 임베딩, 전문 검색, 결합 비교 [실측, 2026-10-30]

평가셋은 직접 만든 합성 데이터다. 사내 규정·회의록·매출 보고·개발 가이드·고객 지원 문단 48개와 질문 53개(말을 바꾼 질문 39개, 고유 키워드 질문 12개, 띄어쓰기 없는 질문 2개)로 이뤄진다. 주제마다 헷갈리게 비슷한 문단을 넣었다. 예를 들어 일본 일비와 일본 숙박비, ERR-4417과 ERR-4410, 10월 7일과 10월 14일 회의록이 있다. 질문마다 정답 문단이 하나다. 문단이 짧고(한두 문장) 개수가 적어서 실제 문서보다 쉬우므로 결과는 방향을 보는 용도로만 쓴다.

설정
- 벡터는 3072차원으로 받아 앞부분을 자르고 L2 정규화해 1536·768을 만들었다. 768로 직접 받은 벡터와 방향이 같다.
- emb2는 `gemini-embedding-2`에 접두어를 붙이지 않은 것이고, emb2p는 문서에 `title: none | text: `, 질문에 `task: search result | query: `를 붙인 것이다. e001은 `gemini-embedding-001`을 OpenAI 호환 경로로 불러 문서와 질문이 모두 RETRIEVAL_QUERY로 임베딩되고, e001tt는 네이티브 `batchEmbedContents`로 문서는 RETRIEVAL_DOCUMENT, 질문은 RETRIEVAL_QUERY로 임베딩했다.
- fts-word는 FTS5 unicode61에 원문 단어를 넣고 질문 단어를 OR로 묶어 bm25 순으로 뽑았다. fts-bigram은 한글을 음절 두 글자씩 겹쳐 자르고 영문·숫자는 통째로 둔 토큰으로 같은 방식으로 뽑았다. rrf는 벡터 순위와 fts-bigram 순위를 RRF(k=60)로 합쳤다.

결과

| 설정 | R@1 | R@3 | R@5 | MRR@10 |
|---|---|---|---|---|
| emb2-3072 | 0.943 | 0.981 | 1.000 | 0.966 |
| emb2-768 | 0.925 | 0.981 | 1.000 | 0.957 |
| emb2p-3072 | 1.000 | 1.000 | 1.000 | 1.000 |
| emb2p-768 | 0.981 | 1.000 | 1.000 | 0.991 |
| e001-3072 | 1.000 | 1.000 | 1.000 | 1.000 |
| e001-768 | 0.981 | 1.000 | 1.000 | 0.991 |
| e001tt-768 | 1.000 | 1.000 | 1.000 | 1.000 |
| fts-word | 0.509 | 0.698 | 0.736 | 0.599 |
| fts-bigram | 0.811 | 0.962 | 0.962 | 0.886 |
| rrf(emb2p-768 + bigram) | 0.962 | 0.981 | 1.000 | 0.976 |
| rrf(emb2-768 + bigram) | 0.925 | 0.981 | 1.000 | 0.958 |
| rrf(e001tt-768 + bigram) | 0.943 | 0.981 | 1.000 | 0.967 |

종류별로 보면 fts-bigram은 키워드 질문 12개와 띄어쓰기 없는 질문 2개를 모두 1위로 맞혔고, 말을 바꾼 질문에서는 R@1이 0.744였다. fts-word는 말을 바꾼 질문에서 R@1이 0.436이었고 띄어쓰기 없는 질문은 둘 다 1위로 못 맞혔다.

실패 사례
- emb2-768은 4건을 1위로 못 맞혔고 전부 말을 바꾼 질문이다("4분기에 몇 명 뽑기로 했어?"는 4분기 매출 목표가 1위이고 정답은 5위). emb2p-768은 "ERR-4417" 한 건을 놓쳤다(비슷한 ERR-4410이 1위, 정답이 2위).
- rrf(emb2p-768 + bigram)은 2건을 놓쳤다. "아파서 일주일 쉬려면 서류가 필요해?"는 bigram 쪽에서 정답이 아예 안 잡혀 벡터 1위였던 정답이 4위로 밀렸다. "ERR-4417"은 두 채널 순위가 엇갈려 점수가 같아졌고, 넣은 순서로 오답이 앞섰다.

읽은 것
- `gemini-embedding-2`에는 문서가 권하는 접두어를 붙이는 편이 낫다. 접두어 없을 때 놓친 4건을 접두어를 붙이자 모두 맞혔다. 768차원과 3072차원 차이는 R@1 한 건이라 저장 공간을 4분의 1로 줄이는 값으로는 작다.
- `gemini-embedding-001`을 네이티브 경로와 task type으로 쓰면 이 셋에서는 가장 좋았다. 다만 입력 상한이 2,048토큰(한국어 약 2,800자)이고 텍스트 전용이며, 쓰려면 네이티브 클라이언트가 필요하다.
- 한국어 전문 검색은 두 글자 단위가 단어 단위보다 확실히 낫고 키워드·코드·띄어쓰기 없는 질문에 강하다. 단순 RRF는 벡터 단독보다 R@1이 조금 낮았다. 전문 검색 쪽이 말을 바꾼 질문에서 엉뚱한 문단을 올리기 때문이다. 하이브리드 가중치, 동점 처리, 재순위는 설계에서 정하고 실제 문서로 다시 재야 한다. 짧은 합성 문단이라 실제 문서의 긴 청크, 표, 오타, 전문 용어 효과는 이 결과에 없다.

결합 방식 추가 비교: 같은 벡터를 재사용해 가중 RRF와 점수 가중합만 바꿔 쟀다. 점수 가중합은 질문마다 코사인을 최소-최대로 0~1에 맞추고, bm25 점수를 최댓값으로 나눈 뒤 `a*벡터 + (1-a)*전문`으로 더했다. 동점은 문단 id 순으로 깼다.

| 설정(768차원) | 벡터만 R@1 | RRF 1:1 | RRF 1:0.5 | 가중합 a=0.9 | a=0.8 | a=0.7 | a=0.5 |
|---|---|---|---|---|---|---|---|
| emb2p | 0.981 | 0.981 | 0.962 | 1.000 | 1.000 | 1.000 | 0.943 |
| emb2 | 0.925 | 0.925 | 0.943 | 0.925 | 0.962 | 0.981 | 0.943 |
| e001tt | 1.000 | 0.943 | 0.981 | 1.000 | 1.000 | 0.981 | 0.962 |

이 셋에서는 벡터 쪽에 0.7~0.8을 주는 점수 가중합이 RRF보다 나았다. Dify 공개 DSL 예제의 가중치(의미 0.7, 키워드 0.3, 1.1절)와 같은 방향이다. 셋이 작고 a를 이 셋으로 고른 것이라 과적합일 수 있다. 기본값 후보로만 쓰고 실제 문서로 다시 잰다.

벡터 쪽 정규화를 빼면 결과가 달라진다. 후보를 벡터 상위 50개로 두고 코사인 원값을 그대로 더하는 방식과, 그 후보 안에서 min-max로 맞추는 방식을 같은 벡터로 비교했다(2026-11-02) [실측].

| 설정(768차원) | 벡터만 R@1 | 코사인 원값 a=0.7 | 0.8 | 0.9 | min-max a=0.7 | 0.8 | 0.9 |
|---|---|---|---|---|---|---|---|
| emb2p | 0.981 | 0.943 | 0.962 | 1.000 | 1.000 | 1.000 | 1.000 |
| emb2 | 0.925 | 0.925 | 0.962 | 0.981 | 0.981 | 0.962 | 0.925 |
| e001tt | 1.000 | 0.943 | 0.962 | 0.981 | 0.981 | 1.000 | 1.000 |

상위 10개 코사인의 폭은 질문당 평균 0.19였다. 원값을 쓰면 벡터 쪽 점수 차가 작아 전문 검색 점수(0~1)가 순위를 끌고 가고, 접두어를 붙인 설정에서 a=0.7은 벡터 단독보다 나빠졌다. 결합에는 후보 안 min-max를 쓰고, 점수 문턱은 결합 전 코사인 원값에 거는 편이 이 결과와 맞다.

## 4. Text2SQL (2025~2026 실무)

### 4.1 표준 파이프라인

여러 출처를 종합한 단계다. 우리 구현 방식은 [추정]이다.

1. 질문 해석: 스키마로 답할 수 있는지 판단하고 모호하면 되묻는다 [Google Cloud 2025-05 https://cloud.google.com/blog/products/databases/techniques-for-improving-text-to-sql].
2. 스키마 연결(schema linking): 서베이(https://arxiv.org/html/2407.15186v5)는 LLM 기반 단계식(테이블 먼저, 컬럼 다음), 임베딩 유사도, 외래키 그래프 방식을 든다. 큰 스키마에서는 여전히 유효하고 모델이 좋아질수록 가치는 줄어든다.
3. 컨텍스트 조립: DDL + 컬럼 설명/샘플 값 + 용어집 문서 + 유사 (질문, SQL) 예시 top-k(질문 유사도 또는 질문 골격 유사도). 서베이는 구조화된 레이아웃이 낫고, 키 정보·힌트가 효과적이며, 예시 선택은 워크플로우가 복잡할수록 중요하다고 정리한다.
4. 생성: 단일 SELECT, 방언을 명시. 필요하면 후보 여러 개를 만들어 실행 결과 다수결(self-consistency)로 고른다(호출 수가 배로 늘어남).
5. 검증: 파싱(sqlglot), 허용 테이블·함수 목록, EXPLAIN/dry-run. Google은 "결정적 신호를 모델에 되돌려 두 번째 시도"를 권한다.
6. 실행: 읽기 전용, 타임아웃, LIMIT 주입, 행 상한.
7. 오류로 고치기: 오류 메시지와 SQL을 되먹임, 최대 2~3회.
8. 답변 정리: 결과 표 일부(상한 N행 + 집계 요약)만 LLM에 전달하고 SQL과 표를 함께 보여 준다.

정확도 기대치 [문서 리더보드]: BIRD 최고는 AskData 개발 78.49% / 시험 83.40%(2026-10-22), 사람 92.96%(https://bird-bench.github.io/). Spider 2.0-Lite 최고는 76.23%, Snow 최고는 96.70%(https://spider2-sql.github.io/). 이들은 대형 에이전트 시스템이고, 서베이 시점의 GPT-4는 BIRD 54.89%였다. 단일 호출 파이프라인은 이보다 낮다고 보아야 하므로 SQL 미리보기·승인 단계를 기본으로 두기를 권한다.

### 4.2 큰 스키마

Uber QueryGPT(https://www.uber.com/blog/query-gpt/, 게시일 미확인)는 테이블 하나가 40~60K 토큰이라 3개 이상이면 32K 한도에서 호출이 깨졌다. 도메인 분류(Workspace/Intent Agent) → 도메인 안에서 RAG → Table Agent(LLM이 테이블 선택, 사용자가 승인) → Column Prune Agent(LLM이 컬럼 제거)로 풀었다.

추천: 우리 환경(SQLite/CSV 위주)에서는 DDL이 작으면 전체를 넣고, 크면 테이블·컬럼 설명을 임베딩한 KB에서 top-N 테이블을 찾아 외래키로 확장하는 방식으로 충분하다 [추정]. 이 "스키마 KB"와 "예시 SQL KB"(Q&A 모드, 질문↔질문 매칭)는 3단계 지식 베이스를 그대로 재사용할 수 있다.

### 4.3 Vanna, WrenAI, 시맨틱 레이어

- Vanna 0.x(MIT)는 DDL, 문서(업무 용어·정의), SQL(예시)을 RAG 저장소에 학습시키고 정답 질문→SQL 쌍을 재사용한다 [README v0.7.9]. Vanna 2.0(PyPI 2.0.2, 2026-02-02)은 에이전트 구조로 다시 쓰였고 GitHub 저장소는 2026-03-29에 아카이브되어 개념만 차용한다. CVE-2024-5565(CVSS 8.1)는 LLM이 만든 Plotly 코드를 `exec`로 실행하는 경로의 프롬프트 주입이었다.
- WrenAI는 MDL(Modeling Definition Language)로 모델·컬럼·관계·메트릭·뷰를 정의하는 시맨틱 레이어를 두고 실행 전에 dry-plan 검증을 거친다. 행·열 수준 보안은 상용 기능이다 [https://github.com/Canner/WrenAI]. 시맨틱 레이어의 정확도 수치는 공급사 주장이라 근거가 약하다 [https://getwren.ai/post/why-the-semantic-layer-is-essential-for-reliable-text-to-sql-and-how-wren-ai-brings-it-to-life].

추천: 초기에는 시맨틱 레이어를 만들지 않고 "스키마 DDL + 컬럼 설명 + 용어집 + 예시 SQL"을 KB에 넣는 Vanna식으로 시작한다. 반복 지표가 생기면 YAML 정의(모델, 관계, 지표)를 추가한다.

### 4.4 읽기 전용 강제

SQLite [실측, SQLite 3.45.3 / Python 3.12.8]

| 방어 | 결과 |
|---|---|
| `mode=ro`만 | INSERT/CREATE는 막힘. `VACUUM INTO 'dump.db'` 성공(DB 전체 복사), `ATTACH`로 다른 DB 파일을 읽을 수 있고, 새 파일에 `create table e.stolen as select * from main.secrets`까지 성공 |
| `mode=ro` + `PRAGMA query_only=ON` | `VACUUM INTO`와 attached 쓰기는 막히지만 빈 파일은 생성되고, `ATTACH`로 다른 DB 읽기는 여전히 가능 |
| + `setlimit(SQLITE_LIMIT_ATTACHED, 0)` | `ATTACH`, `VACUUM INTO` 모두 "too many attached databases"로 차단 |
| + authorizer(SELECT/READ/허용 함수/RECURSIVE만 허용, 나머지 DENY) + 진행 핸들러(마감 시각 넘으면 1 반환) | INSERT, CREATE, ATTACH, VACUUM, PRAGMA, `load_extension` 모두 거부되고 재귀 CTE 폭주는 `interrupted`로 끊김. 파일 생성 없음 |

- `PRAGMA query_only` 자체의 한계는 [문서 https://www.sqlite.org/pragma.html] "완전한 읽기 전용이 아니며 체크포인트·COMMIT은 가능"이다.
- Python 연결은 `isolation_level=None`(자동 커밋)으로 열어야 한다. 기본 모드에서 실패한 DML 뒤에 열린 트랜잭션이 남아 `VACUUM`이 "트랜잭션 안에서는 안 된다"로 가려지는 착시가 생긴다.
- 업로드한 SQLite 파일은 작업공간으로 복사해 `immutable=1` URI로 열면 `-wal`/`-shm` 생성도 피한다 [문서 https://www.sqlite.org/uri.html].

DuckDB [문서 https://duckdb.org/docs/2.0/operations_manual/securing_duckdb/overview.html + 실측 2.0.0]: 영속 DB는 `read_only=True`(`access_mode`)로 연다. 설정은 아래 순서로 하고 마지막에 잠근다.
1. `SET autoload_known_extensions=false; SET autoinstall_known_extensions=false;`
2. 필요한 확장(`excel`, `encodings`)을 먼저 `LOAD`
3. `SET allowed_paths=['<업로드 파일>']; SET enable_external_access=false;`
4. `SET memory_limit='256MB'; SET threads=2; SET max_temp_directory_size='1GB';`
5. `SET lock_configuration=true`

[실측] 잠근 뒤에도 사전에 만든 VIEW는 읽힌다. 다른 파일 읽기·`read_text`·`glob`·COPY TO·ATTACH·INSTALL은 모두 권한 오류이고 설정 변경도 거부된다. 다만 인메모리 DB에서는 `CREATE TABLE`이 여전히 가능하다. 문서도 이 설정이 샌드박스를 대체하지 못한다고 말한다.

PostgreSQL·MySQL·SQL Server는 1차 범위 밖이라 요점만 적는다.
- PostgreSQL [문서 https://www.postgresql.org/docs/current/sql-set-transaction.html]: `BEGIN TRANSACTION READ ONLY`나 `default_transaction_read_only=on`과 `pg_read_all_data` 역할을 쓰고 `pg_read_server_files`·`pg_execute_server_program`은 주지 않는다(https://www.postgresql.org/docs/current/predefined-roles.html). 임시 테이블 쓰기는 허용되어 "모든 디스크 쓰기를 막지는 않는다". 문 시간 제한은 `statement_timeout`이다 [문서 확인 못함, 일반 지식].
- MySQL [문서 https://dev.mysql.com/doc/refman/8.4/en/commit.html]: `START TRANSACTION READ ONLY`와 SELECT 권한만 가진 계정을 함께 쓴다. `max_execution_time`과 `secure_file_priv`는 공식 항목을 확인하지 못했다 [일반 지식, 미확인].
- SQL Server [문서, ms.date 2025-12-11 https://learn.microsoft.com/en-us/sql/relational-databases/security/authentication-access/database-level-roles]: 읽기 전용 트랜잭션 모드가 없어 `db_datareader` + `db_denydatawriter`로 막는다. `ApplicationIntent=ReadOnly`는 쓰기 방지 수단이 아니다 [일반 지식, 공식 페이지 확인 못함].

공통 원칙: 앱 SQL 검사에만 기대지 않고 DB 자격 자체를 읽기 전용으로 만든다. Dify의 DB 플러그인 README도 "sql_execute는 어떤 SQL이든 실행하므로 항상 읽기 전용 계정을 쓰라"고 경고한다.

### 4.5 sqlglot

- 30.26.0(PyPI, 2026-10-29): 순수 파이썬 휠 0.8MB(설치 7.9MB), 의존성 없음, MIT. 방언 34개에 SQLite, DuckDB, Postgres, MySQL, TSQL이 들어 있다 [문서].
- [실측] import +14MB RSS, 파싱 0.23ms/문. 단일 문장 검사, SELECT 전용, 중첩 INSERT/DELETE 거부, 테이블 목록 추출(`find_all(exp.Table)`), LIMIT 주입(`parse_one(...).limit(100)`, T-SQL이면 `TOP 100`으로 변환)이 모두 동작한다.
- 한계: ①`SELECT 1 /* 주석 */ ; -- 끝`처럼 끝 주석이 별도 "문장"으로 잡혀 문장 수가 2가 되므로 주석·세미콜론을 먼저 정리해야 한다. ②루트 노드 검사만으로는 `load_extension()`, `pg_read_file()`, `read_csv('/etc/passwd')`, `SLEEP()`을 못 막으므로 함수·테이블 허용 목록이 따로 필요하다. ③sqlglot은 "변환기이지 검증기가 아니다"(문서)라서 방언 변환에 손실이 있다(`DATE_TRUNC`(Postgres)을 SQLite로 옮기면 SQLite에 없는 `TIMESTAMP_TRUNC`가 나온다). 목표 방언으로 직접 생성시키고 변환은 표시용으로만 쓴다.

### 4.6 DuckDB로 CSV/Excel 직접 질의

- CSV [문서 https://duckdb.org/docs/2.0/data/csv/overview.html]: `read_csv('f.csv')`가 구분자·헤더·타입을 자동 감지한다(`sample_size` 기본 20480행, 실패 시 `all_varchar`). 기본 인코딩은 UTF-8/UTF-16/Latin-1이다. 한국어 엑셀의 CP949 CSV는 `INSTALL encodings; LOAD encodings;` 뒤 `read_csv(..., encoding='cp949')`로 읽힌다(https://duckdb.org/docs/2.0/core_extensions/encodings.html, 이 PC에서 직접 확인).
- Excel [문서 https://duckdb.org/docs/2.0/core_extensions/excel.html]: 첫 사용 때 `excel` 확장이 자동 설치·로드된다. `read_xlsx('f.xlsx', sheet=..., range=..., header=..., all_varchar=...)`. `.xls`는 지원하지 않는다. 타입 추론은 DOUBLE/VARCHAR 위주라(실측: 정수 id가 DOUBLE) 필요하면 `all_varchar`로 읽고 캐스팅한다. 설치에 네트워크가 필요하므로 잠금 전에 미리 설치한다(잠금 후 INSTALL은 거부됨).
- [실측] 150만 행·61MB CSV를 `memory_limit='256MB'`, `threads=2`로 그룹 집계하는 데 476ms, 최대 작업 집합 125MB였다. 메모리 설정은 `SET memory_limit`(기본은 RAM의 80%) [문서 https://duckdb.org/docs/2.0/configuration/overview.html].

### 4.7 보안 위험

- 프롬프트 주입으로 파괴적 SQL을 만드는 공격(P2SQL)이 LangChain 기반 앱에서 광범위하게 성공한다고 보고됐다(arXiv 2308.01990, 방어 4종 제안). LangChain SQLDatabaseChain CVE-2023-36189(CVSS 7.5, 0.0.247에서 수정).
- 데이터 안의 문구가 지시로 작동하는 간접 주입, 대량 반출(`SELECT *`, `VACUUM INTO`, `ATTACH`), 서버 파일 읽기 함수, 자원 고갈(교차 조인, 재귀 CTE)이 주된 위험이다. OWASP LLM Top 10 2025는 LLM01 Prompt Injection, LLM02 Sensitive Information Disclosure, LLM05 Improper Output Handling, LLM06 Excessive Agency를 해당 항목으로 든다(https://genai.owasp.org/llm-top-10/).
- 방어: 읽기 전용 자격(4.4) + AST 검증(4.5) + 허용 테이블·컬럼(열 단위 마스킹) + 행·바이트 상한 + 시간 제한 + 실행 프로세스 격리 + 결과를 LLM에 보낼 때 일부 행만 전달(데이터가 Gemini로 나가므로 개인정보 열은 제외) + 승인 단계(SQL 확인).

### 4.8 Dify 사용자 방식과 실행 위치 추천

- Dify 기본 "SQL Creator" 템플릿은 테이블 구조를 입력창에 붙여 넣고 SQL 생성/해설만 하며 실행하지 않는다(안전장치도 없음. Zenn, 2024-08-15, https://zenn.dev/qed_ai/articles/c10b0201e00221). 커뮤니티 DB 플러그인은 junjiem "Database Query Utils"(README에 읽기 전용 언급 없음, https://github.com/junjiem/dify-plugin-tools-dbquery)와 hjlarry "database" v0.0.3(`TABLES`로 노출할 테이블 제한, https://github.com/WL-Engineer/dify-plugin-database) 등이 있다. 워크플로우 조합은 지식 검색(스키마·예시) → LLM(SQL 생성) → 도구(SQL 실행) → LLM(요약)이고, 우리 2단계 노드(`knowledge_retrieval`, `llm`, `loop`, `tool`)로 같은 구성이 가능하다.
- 검증(sqlglot 파싱, 허용 목록, LIMIT 주입)은 서버에서 하고, 실행은 샌드박스 하위 프로세스(`python -I`, Job Object 메모리·시간 상한)에서 한다. 이유는 네 가지다. DuckDB와 질의가 서버 RSS 300MB를 쉽게 넘길 수 있다. SQLite `ATTACH`로 app.db를 읽는 경로를 구조적으로 끊을 수 있다(작업공간에는 대상 DB 복사본만 둔다). 시간 초과를 프로세스 종료로 확실히 처리할 수 있다. 외부 DB 자격은 서버 쪽 시크릿으로 두고 하위 프로세스에 필요한 것만 넘긴다.
- 2단계 `code` 노드와 분리해 전용 `sql_query` 노드(연결 참조, `max_rows`, `timeout_sec`, 출력 `columns/rows/row_count/truncated/sql_executed`)를 두는 안을 추천한다 [추정].
- 1차 범위는 SQLite + DuckDB(CSV/Excel)로 두고 PostgreSQL/MySQL/SQL Server는 읽기 전용 계정 안내와 함께 후속으로 미룬다.

## 5. 확인하지 못한 부분

- Dify: General·Parent-child 청크의 UI 기본값(구분자·최대 길이·겹침·자식 길이), 가중치 점수의 계산식과 키워드 점수 산출 방식, 파이프라인 실행 중 문서 하나 실패 시 동작과 재실행 방식, 클래식 재색인 버튼의 정확한 동작, 레거시 Agent 앱이 KB를 도구로 쓰는지, `knowledge_retrieval` `result` 항목의 정확한 필드, `metadata_filtering_*` DSL 필드는 공식 문서와 공개 DSL 예제에서 찾지 못했다. 문서가 말하는 Top K 기본값 3은 실제 UI 기본값과 다를 수 있다.
- sqlite-vec: 50만·100만 개는 RAM 여유 때문에 재지 않았다. 측정은 한 대의 PC, 부하가 변하는 상태(109~380ms)라 절대값보다 비율을 믿어야 한다. 합성 벡터라 Gemini 임베딩의 bit 양자화 recall은 모르고, usearch recall도 합성 군집 데이터 기준이다.
- 한국어 검색 실험은 KorQuAD(위키 문단, 질문형)와 짧은 합성 문단 48개(3.5절)뿐이다. 업무 문서·표·코드에서는 다를 수 있고, Kiwi의 질의 쪽 이득은 이 실험에서는 나타나지 않았다.
- MySQL `max_execution_time`·`secure_file_priv`, SQL Server `ApplicationIntent`(공식 페이지 404), PostgreSQL `statement_timeout`은 공식 문서에서 확인하지 못했다. Uber 블로그 게시일, 마켓플레이스 설치 수(약 10만은 검색 요약)는 미확인이다.
- Gemini 쪽에서 남은 확인 항목은 `docs/research/gemini-api-2026-10.md` 7절에 있다.

## 6. 핵심 출처와 날짜

- Dify 문서(날짜 없음, 1.18.0 기준): https://docs.dify.ai/en/self-host/use-dify/knowledge/ 아래 페이지들, https://docs.dify.ai/en/api-reference/ (OpenAPI https://docs.dify.ai/en/api-reference/openapi_service.json), https://docs.dify.ai/en/cloud/use-dify/knowledge-base/overview.md (Agent KB)
- Dify 릴리스: https://github.com/langgenius/dify/releases — 1.9.0 2025-09-22, 1.11.0 2025-12-11, 1.12.0 2026-02-03, 1.13.1 2026-03-17, 1.17.1 2026-09-10, 1.18.0 2026-10-23
- 공개 DSL 예제: https://github.com/svcvit/Awesome-Dify-Workflow/tree/main/DSL
- sqlite-vec: https://github.com/asg017/sqlite-vec, https://pypi.org/project/sqlite-vec/ (0.1.9, 2026-03-31), 릴리스 v0.1.7~v0.1.10-alpha.4, ANN PR #276~#278(2026-03-31), https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html (2024-08), https://alexgarcia.xyz/blog/2024/sqlite-vec-metadata-release/index.html (2024-11), https://alexgarcia.xyz/sqlite-vec/features/vec0.html
- 그 밖의 URL은 본문 해당 위치에 있다. Python `sqlite3` 문서는 https://docs.python.org/3/library/sqlite3.html, DuckDB 2.0 문서는 https://duckdb.org/docs/2.0/(vss, configuration, excel, csv, encodings, securing_duckdb)다.
