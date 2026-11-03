# ADR-0008: Text2SQL은 파일 데이터 소스에 서버 검증·샌드박스 읽기 전용 실행

- 상태: 채택(2026-11-02). 설계서(`docs/design/phase3-rag.md`) Z5·Z6을 사용자가 따로 정하지 않아 기본값대로 3단계 1차에 넣고(파일 대상, 승인 기본 자동) M27부터 진행한다. v2 (architect, 2026-11-02, design-reviewer 검토 반영).
- 날짜: 2026-11-02
- 배경: 사용자가 3단계 설계 때 Text2SQL을 함께 검토하자고 했다. 개인 PC의 표 데이터는 대부분 CSV·Excel·SQLite 파일이다. 실측에서 SQLite는 `mode=ro`만으로 `VACUUM INTO`·`ATTACH`가 통과했고, DuckDB 2.0은 `allowed_paths`·`enable_external_access=false`·`lock_configuration` 순서로 잠그면 외부 접근이 모두 거부됐다. sqlglot은 SELECT 전용 검사·테이블 추출·LIMIT 주입이 되지만 함수 허용 목록이 따로 필요하고 "변환기이지 검증기가 아니다". Text2SQL 정확도는 대형 시스템도 BIRD 78~83%다. 서버 RSS 예산은 300MB다. 근거는 `docs/research/rag-text2sql-2026-11.md` 4장.

## 결정

### D1. 1차 대상은 파일. 외부 DB는 4단계 A2 뒤
- 데이터 소스(`sql_source`, 마이그레이션 0008)는 CSV·TSV·xlsx와 SQLite 파일이다. 파일은 저장소 `sql-sources/`에 두고 표 이름·옵션·스키마 스냅샷·표·열 설명·숨김 열·용어집을 가진다.
- PostgreSQL·MySQL·SQL Server는 자격 증명 저장소(4단계 A2)와 읽기 전용 계정 안내가 전제라 후속이다. `kind`에 자리만 둔다.

### D2. 검증은 서버(sqlglot), 실행은 샌드박스 하위 프로세스, 읽기 전용과 숨김 열은 엔진 수준에서 강제
- 서버는 sqlglot(지연 import, +14MB)으로 단일 문장·SELECT/CTE 전용·트리 전체의 DML·DDL·Command 거부·테이블 허용 목록·경로 치환 스캔과 테이블 함수 거부·방언별 함수 허용 목록·숨김 열 거부·`SELECT *` 전개·`LIMIT max_rows + 1` 주입을 한다. 검증은 첫 관문이고 거부 사유는 `sql_query_log`에 남는다.
- 실행은 세 번째 로컬 백엔드(`data/kb-work/`)의 `python -I` 하위 프로세스(Job Object 512MB, 프로세스 4, `timeout_sec + 5`에 트리 종료)다. 서버는 DuckDB·sqlite3 대상 파일을 열지 않는다.
- DuckDB 파일 소스는 "준비" 단계에서 보이는 열만 담은 `data/cache/kb-sql/<source>.duckdb`를 한 번 만들고(이때만 원본 경로와 `excel`·`encodings` 확장이 필요하다), 질의 때는 그 파일을 `read_only=True`, `enable_external_access=false`, `allowed_paths` 비움, 메모리 256MB·스레드 2, `lock_configuration=true`로 연다. 숨긴 열은 파일에 없어 `read_csv('<원본>')`·`FROM '<경로>'`·`SELECT t.*`·서브쿼리 `*` 어느 길로도 읽히지 않고, 200MB CSV를 질의마다 다시 파싱하지도 않는다. 파일·옵션·숨김 열이 바뀌면 다시 만든다.
- SQLite 파일 소스는 `mode=ro&immutable=1` + `isolation_level=None` + `query_only` + `SQLITE_LIMIT_ATTACHED=0` + authorizer(SELECT·READ·허용 함수·RECURSIVE만 허용, `SQLITE_READ`에서 숨김 열은 `SQLITE_IGNORE`로 NULL) + 진행 핸들러다. 쓰기·반출(`VACUUM INTO`, `ATTACH`)·재귀 CTE 폭주 차단과 DuckDB의 잠금 뒤 외부 접근 거부, 메모리 집계는 실측했고, 타임아웃의 프로세스 트리 종료와 `SQLITE_IGNORE` 동작은 M27의 Windows 시험에서 확인한다.
- 결과는 `max_rows`(기본 200, 상한 1,000)·변수 1MB 안이고 넘치면 CSV 파일로 낸다. LLM에 보내는 `result_text`는 앞 50행·64KB다.

### D3. 노드는 `sql_schema`·`sql_query` 둘, SQL 생성은 `llm` 노드, 에이전트 도구는 둘
- `sql_schema`는 스냅샷에서 DDL·설명·샘플값·용어집 글을 만든다(DB 접근 없음). `sql_query`는 템플릿 또는 변수의 SQL(코드 펜스 안을 쓴다)을 검증·실행한다. 블랙박스 `text2sql` 노드는 두지 않는다. 프롬프트·오류 되먹임·되묻기는 사용자가 캔버스에서 조립하고 템플릿 챗플로우 `text_to_sql`(생성 → 실행 → 실패 시 1회 수정 → 답변)을 제공한다.
- 에이전트는 내장 도구 `describe_sql_source`·`query_sql`(그룹 `sql`, 승인 등급 read)로 쓴다. 4단계 A1이 오면 `kind: sql` 레지스트리 항목으로 옮긴다.

### D4. 승인은 기본 자동, 선택으로 항상 승인. SQL은 항상 보인다
- 읽기 전용과 숨김 열이 D2로 강제되므로 틀린 SQL은 안전 문제가 아니라 품질 문제다. 기본은 자동 실행이고 노드 런 카드·도구 카드·웹앱에 실행된 SQL과 결과를 항상 같이 보여 준다.
- 노드 또는 데이터 소스의 `approval=always`면 2단계 `human_input` 폼 대기 기계로 "이 SQL을 실행할까요" 카드를 띄우고 사용자가 SQL을 고쳐 승인할 수 있다(재검증 뒤 실행). 새 대기 경로는 없다.
- 데이터가 채팅 모델로 나간다는 문구를 노드 폼과 데이터 소스 화면에 적고, 데이터 소스의 `key_tier`가 `paid`면 그 런은 유료 키로 돈다(ADR-0007 D8).

### D5. 스키마·예시 SQL은 지식 베이스를 재사용. 시맨틱 레이어는 만들지 않는다
- 큰 스키마는 "스키마를 KB로 내보내기"(표마다 문서 하나)로 `knowledge_retrieval → llm → sql_query` 흐름을 만든다. 예시 SQL은 Q&A 모드 KB에 (질문, SQL)로 저장하고 "예시로 저장" 단추가 넣는다. 지식 쪽 코드는 바꾸지 않는다.
- WrenAI식 MDL·시맨틱 레이어, Vanna 코드는 쓰지 않는다. 반복 지표가 생기면 YAML 정의를 뒤에 더할 수 있다.

## 기각한 대안
- 서버 프로세스 안에서 DuckDB 실행(메모리 예산). 검증만으로 읽기 전용·숨김 열 보장(sqlglot 우회·방언 함수·`*` 전개 누락). `mode=ro`만(VACUUM INTO·ATTACH 통과). 질의 때 원본 CSV를 뷰로 읽기(원본 경로가 `allowed_paths`에 남아 숨김 열이 한 겹 방어가 된다. v1의 방식). 질의마다 소스 파일 복사(200MB 파일). 항상 승인 기본(개인용 UX, 안전은 엔진이 맡는다). 블랙박스 `text2sql` 노드(프롬프트·반복을 숨긴다). 외부 DB 1차(자격 증명·서버별 계정 안내). Vanna·WrenAI 의존(아카이브, 보안 이력, 스택 불일치).

## 영향
- 마일스톤 M27(백엔드)·M28(화면). 마이그레이션 0008. 신규 의존성: 서버 `sqlglot`, 샌드박스 `duckdb`(첫 사용 전 "런타임 준비"로 설치, DuckDB 확장 `excel`·`encodings`는 `data/cache/kb-sql/duckdb-ext/`에 미리 설치). 새 캐시 폴더 `data/cache/kb-sql/`(소스별 질의 파일. 백업 제외, 원본에서 다시 만든다).
- 1·2단계 접점: `human_input` 폼 대기 함수 재사용, `ToolContext.sql`, `agent_version.sql_sources`, `WorkflowTemplate`에 `text_to_sql`, DSL `source_slug`, 런 tier 결정에 데이터 소스 포함. 설정 `SQL_MAX_ROWS`(1000), `SQL_TIMEOUT_SEC`(30, 상한 60), `SQL_MEMORY_MB`(512).
- 보안 확인표 KS3·KS4·KS5(우회 네 가지)·KS11(설계 14.5)을 M29의 Text2SQL 갈래에서 수동 검증한다.
