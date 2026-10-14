# 운영

평소 서버를 띄우고 끄고, 로그와 디스크를 살피고, 키와 허용 목록을 바꾸는 방법이다. 처음 설치는 [install.md](install.md), 백업과 복구는 [backup-restore.md](backup-restore.md)에 있다. 데이터는 모두 `DATA_DIR` 아래에 있고 예시는 `C:\wf-data`로 쓴다.

## 기동과 종료

```bash
uv run platform serve
```

uv가 PATH에 없는 터미널이면 `python -m uv run platform serve`로 같다. VS Code에서는 `apps/api/app/main.py`를 열어 편집기 위쪽의 실행 버튼을 누르거나 실행 및 디버그의 'platform serve'를 고른다. 둘 다 `platform serve`와 같은 경로로 뜬다. `.env`를 고쳤다면 서버를 다시 띄워야 반영된다. 허용 목록 파일만 예외다.

끌 때는 Ctrl+C다. 로그오프나 시스템 종료 때도 Windows 콘솔 이벤트를 받아 같은 정리를 하게 해 두었고, OS가 5초 뒤에 강제로 끝내므로 4초까지만 기다린다. 다만 콘솔 창을 닫아서 끄지는 않는다. `uv run`으로 띄운 서버는 창이 닫힐 때 앞에 있는 venv 런처가 먼저 끝나면서 정리 중인 서버까지 끝내는 것으로 보인다. 로그에 `runner.shutdown_started`만 있고 `server.stopped`가 없으면 그렇게 끝난 것이고, 다음 기동의 복구가 뒷정리를 한다. Ctrl+C로 끄면 도는 중이던 런은 `failed`로 끝나고 오류 코드가 `shutdown`이며 그때까지 나온 응답은 남는다. 도구 프로세스는 같이 끝나고, 정상 종료면 데이터 폴더에 `-wal` 파일이 남지 않는다. 승인이나 질문을 기다리던 런은 그대로 두었다가 서버가 다시 뜬 뒤 답하면 이어서 실행된다. 답은 `WAITING_INPUT_TTL_HOURS`(기본 24시간) 안에 해야 한다.

다시 띄우면 기동 복구가 먼저 돈다. 돌던 런은 `failed(shutdown)`으로 정리하고, 큐에 있던 런은 다시 넣는다. 작업공간 파일과 설치한 패키지는 그대로여서 같은 세션에서 이어서 작업할 수 있다. 확인하려면 도구 카드가 '실행 중'일 때 Ctrl+C로 끄고 다시 띄운다. 로그 화면에서 그 런이 실패와 `shutdown`으로 보이는지, 같은 세션에 새 메시지를 보냈을 때 앞서 만든 파일이 `Get-ChildItem`에 나오는지 본다.

## 로그와 헬스

로그는 `C:\wf-data\logs\app.log`에 JSON 한 줄씩 쌓이고 콘솔에도 같은 줄이 나온다. 10MB를 넘으면 `app.log.1`로 넘어가며 5개까지 남긴다. `AIza…`·`AQ.…`·`sk-…` 꼴의 키, `Bearer` 토큰, `.env`에 든 비밀값 원문, `password`·`token`·`api_key` 같은 이름의 필드 값은 `***`로 바뀐 채 기록된다. 한글은 `\uXXXX`로 이스케이프되어 있어서 읽을 때 풀어야 한다.

```bash
tail -n 20 C:/wf-data/logs/app.log | python -c "import sys,json;[print(json.dumps(json.loads(l),ensure_ascii=False)) for l in sys.stdin]"
grep housekeeping.hourly C:/wf-data/logs/app.log | tail -n 1
```

자주 보게 되는 이벤트는 `server.ready`(기동 완료, 포트와 RSS), `startup.check_failed`(기동 점검 경고), `housekeeping.hourly`(매시 정리 결과), `memory.over_budget`(메모리 예산 초과)이다. 로그 수준은 `LOG_LEVEL`로 바꾼다.

```bash
curl -s http://127.0.0.1:8500/healthz
```

`status`, `rss_mb`(서버 프로세스의 메모리), `uptime_sec`, `runner`(돌고 있는 런과 대기 중인 런 수, 종료 중인지), `db`가 나온다. `/readyz`는 DB가 응답하고 러너가 떠 있으면 200이고 아니면 503이다.

## 하우스키핑과 보존

정리 작업은 서버가 뜬 직후에 한 번, 그다음에는 한 시간마다 돈다. 켜고 끄는 일이 잦은 PC라서 한 시간을 기다리지 않고 기동 직후에도 돌게 했다. 만료된 로그인 세션을 지우고, `WAITING_INPUT_TTL_HOURS`가 지나도록 답이 없는 입력 대기 런을 `input_timeout`으로 닫고, 보존 기간이 지난 런의 이벤트와 큰 도구 출력 파일을 지운다. 어느 버전에서도 쓰지 않는 에이전트 첨부 파일과 스킬 버전은 만든 지 7일이 지나면 지운다.

파일 쪽으로는 지운 세션에 남은 첨부, 세션이 없는 작업공간 폴더와 행, `.deleted-`로 이름이 바뀐 폴더를 치운다. 서버가 종료되는 중에 끝나서 지우지 못한 일회성(ephemeral) 과업의 작업공간과, 이벤트는 지웠는데 파일 삭제가 실패해 남은 `tool-outputs` 폴더도 여기서 정리한다. SQLite WAL은 읽기와 쓰기를 막지 않는 방식(PASSIVE)으로 비운다. `data\workspaces`와 `cache\pip`의 크기도 재서 `housekeeping.hourly` 로그에 `workspaces_mb`, `pip_cache_mb`로 남긴다. 재는 데 3초가 넘게 걸려 일부만 쟀으면 `disk_estimated`가 true다. 별도로 60초마다 메모리를 재고 예산을 넘으면 경고만 남긴다.

끝난 세션의 작업공간 폴더는 세션을 지울 때까지 남긴다. 세션이 만든 결과 파일, 지운 에이전트의 발행 스냅샷, pip 캐시도 자동으로 지우지 않는다. 디스크가 차면 아래 '디스크와 메모리'를 본다.

런 이벤트(도구 호출 입출력과 승인 기록이 든 로그의 원본)의 보관 기간은 설정 → 워크스페이스의 '런 이벤트 보관 기간(일)'이다. 기본은 90일이고 1일부터 3650일까지 정한다. 지나면 이벤트와 도구 출력 파일만 지우고 메시지와 런 요약은 남는다. `.env`의 `RUN_EVENT_RETENTION_DAYS`는 처음 계정을 만들 때 화면 값의 시작값으로 한 번 들어갈 뿐이고, 그 뒤에는 화면 값이 우선한다.

## 디스크와 메모리

데이터 폴더에서 크기를 차지하는 곳은 세션별 작업공간(`workspaces`), 첨부와 결과 파일과 발행 스냅샷이 있는 `storage`, pip 캐시(`cache\pip`)다. 작업공간 하나는 `WORKSPACE_MAX_BYTES`(기본 2GB)를 넘으면 그 세션의 새 메시지를 `workspace_disk_limit` 오류로 거절한다. 설정 → 작업공간 화면에서 작업공간별 크기와 한도 초과 여부를 볼 수 있고, 거기서 정리한다(미리보기 채팅의 '작업공간 보기'에도 같은 버튼이 있다). '패키지 삭제'는 `.packages`와 `.cache`를 비우고 설치 이력은 남겨 필요하면 다시 설치하게 한다. '작업공간 초기화'는 폴더를 지우고 첨부 파일과 스킬만 다시 채우므로 에이전트가 만든 파일과 패키지가 사라진다. 대화 기록과 이미 내려받을 수 있게 보관된 결과 파일은 남는다.

`cache\pip`는 자동으로 지우지 않는다. 커졌으면 서버를 끈 상태에서 폴더를 지운다. 다음 패키지 설치가 느려질 뿐이다.

```powershell
Remove-Item -Recurse -Force C:\wf-data\cache\pip
```

메모리 예산은 300MB(`MEMORY_BUDGET_MB`)다. 서버가 도는 동안은 `/healthz`의 `rss_mb`로 보고, 예산을 넘으면 `memory.over_budget` 경고가 로그에 남는다. 서버가 알아서 재시작하지는 않는다. 새로 재려면 스크립트가 서버를 따로 띄워서 잰다. 빈 포트와 임시 데이터 폴더, 가짜 어댑터를 쓰므로 지금 쓰는 서버와 데이터에 영향이 없다.

```bash
python scripts/memcheck.py
```

유휴 상태에서 재고, 가짜 어댑터로 런 하나를 돌리는 동안 재고, 런이 끝난 뒤에 다시 잰다. 결과는 `RSS 최대 X MB (유휴 Y, 런 중 Z, 런 뒤 W)`로 나오고 최댓값이 예산을 넘으면 종료 코드가 1이다. 서버가 뜨지 않거나 런이 성공으로 끝나지 않아 못 쟀으면 2다. 예산은 셸 환경변수 `MEMORY_BUDGET_MB`에서 읽고 없으면 300이다. `--seconds`로 유휴 측정 시간(기본 5초)을 바꾸고, `--idle-only`는 유휴만 잰다. `uv run platform memcheck`도 같은 스크립트다. CI의 ubuntu와 windows 잡에서도 돌지만 기준은 Windows 값이다.

예산을 계속 넘으면 코드에서 순서대로 줄인다. `openai`를 첫 모델 호출 때 불러오는지 확인하고, 이벤트 링버퍼를 2000개에서 1000개로, 도구 출력의 메모리 보관을 256KB에서 128KB로, SQLAlchemy 풀을 3에서 2로 줄인다. 그래도 넘으면 예산을 올리고 결정 기록(ADR)에 적는다([설계 12.3](../design/phase1-agent-sandbox.md#123-리스크)).

## 계정과 키

비밀번호를 잊었으면 서버를 끄고 터미널에서 바꾼다. 새 비밀번호를 두 번 묻고(12자 이상), 바꾸면 모든 로그인 세션이 끊기니 브라우저에서 다시 로그인한다. 로그인해 있다면 '내 계정'에서 바꾸는 쪽이 간단하다.

```bash
uv run platform reset-password
```

Gemini 키를 바꾸려면 `.env`의 `GEMINI_API_KEYS`를 고치고 서버를 다시 띄운다. 키는 `.env`에서만 읽어서 재기동 없이는 바뀌지 않는다. 401이나 403을 받은 키는 서버가 다시 뜰 때까지 쓰지 않는다. 설정 → LLM에서 키별 상태와 '연결 테스트'를 볼 수 있고 키 값은 번호와 끝 4자리만 나온다. 모델이 429나 5xx로 막히면 `GEMINI_MODEL_FALLBACK` 순서대로 다음 모델을 시도한다. `ENCRYPTION_KEY`는 바꾸지 않는다. 바꾸면 기존 에이전트 환경변수가 복호화되지 않는다([backup-restore.md](backup-restore.md#키-보관과-fingerprint)).

## 허용 목록

에이전트의 `http_request` 도구가 접속할 수 있는 호스트는 `C:\wf-data\allowlist.txt`에 있다. `HTTP_ALLOWLIST_FILE`로 다른 파일을 지정할 수도 있다. 편집기로 열어 고치고 저장하면 몇 초 안에 반영되어 서버를 다시 띄우지 않아도 된다. 설정 → HTTP 허용 목록 화면은 읽은 규칙을 보여 주기만 한다. 처음 뜰 때 PyPI와 GitHub 도메인만 든 기본 파일을 만든다.

```text
# 한 줄에 하나. #부터는 주석
api.example.com
# 앞의 점은 하위 도메인 전부(example.org 자신은 빠진다)
.example.org
```

`*` 한 줄은 모든 공개 호스트를 연다. 사설·루프백 주소와 IP 주소는 `*`로도 열리지 않는다(IP는 그 주소를 직접 적어도 사설 주소면 막힌다). `http_request`의 GET은 승인 없이 나가므로 `*`는 꼭 필요할 때만 쓴다([security.md](security.md#운영-시-지킬-것)). 셸과 파이썬이 직접 여는 네트워크는 이 목록과 상관없고 승인 모드가 통제한다.

## 개발할 때

```bash
uv run platform serve --reload
corepack pnpm --dir apps/web dev
```

`--reload`를 붙이면 `apps/api/app`과 `apps/api/migrations`의 `.py` 파일이 바뀔 때 서버를 끄고 다시 띄운다. 파일을 지켜보는 프로세스가 하나 더 떠서 메모리를 20MB 안팎 더 쓰므로 개발 중에만 쓴다. 서버는 평소와 같은 방식으로 뜨기 때문에 이 모드에서도 셸과 파이썬 도구가 실행된다. 콘솔에서 `reload:`로 시작하는 줄이 감시 프로세스의 안내다. 무엇이 바뀌어 다시 띄우는지(`reload: <파일> 바뀜. 서버를 다시 띄운다`), 서버가 혼자 죽었는지(`reload: 서버가 종료 코드 N으로 끝났다…`)를 알려 준다. 다시 뜨는 동안 돌던 런은 `shutdown`으로 끝나고, 서버가 10초 안에 끝나지 않으면 강제로 끝낸 뒤 다시 띄운다. 문법 오류 때문에 서버가 죽어도 감시 프로세스는 남아서 파일을 고쳐 저장하면 다시 띄운다. `.env`는 지켜보지 않으니 고쳤으면 직접 다시 띄운다. Ctrl+C 한 번이면 둘 다 끝나고, 정리를 기다리는 중에 한 번 더 누르면 기다리지 않고 바로 끝낸다.

화면을 고칠 때는 `dev`로 vite 개발 서버(5173)를 띄운다. `/api` 요청은 127.0.0.1:8500으로 넘어가므로 API 서버도 떠 있어야 한다. 개발 서버의 출처는 `DEV_ORIGINS`로 허용하고, 개발을 안 할 때는 이 값을 비워 둔다. `corepack pnpm --dir apps/web build`로 만든 `apps/web/dist`는 서버가 서빙하고, 서버가 이미 떠 있으면 새로 고침만 해도 새 화면이 나온다. build는 `dist.next`에 만든 뒤 성공했을 때만 `dist`와 바꾸므로, 실패해도 서버가 내주던 화면은 그대로 남는다. 단 서버를 띄울 때 `dist`가 없었다면 build 뒤에 서버를 다시 띄워야 한다.

## 스모크 스크립트

```bash
bash scripts/smoke.sh
```

떠 있는 서버에 `/healthz`, `/readyz`, 화면 라우트, 없는 API 경로의 JSON 404, 변조한 Host의 400을 차례로 확인한다. 포트는 `SERVER_PORT` 환경변수나 `.env`에서 읽고, 하나라도 실패하면 종료 코드가 1이다. 발행한 에이전트의 서비스 API는 `scripts/smoke-api.sh`로 훑는다. 쓰는 법은 [README](../../README.md)에 있다.

## 정기 백업

백업은 서버가 하지 않고 필요할 때 `uv run platform backup`을 직접 돌린다. 옵션과 복구는 [backup-restore.md](backup-restore.md)에 있다. 주기적으로 하고 싶으면 작업 스케줄러에 등록한다. 아래는 매일 18시에 최근 14개만 남기고 백업하는 예시이고 PowerShell에서 실행한다. Git Bash는 `/SC` 같은 인자를 경로로 바꿔 버린다. 작업 스케줄러는 PATH와 시작 위치가 달라서 venv의 실행 파일을 직접 부른다.

```powershell
schtasks /Create /SC DAILY /ST 18:00 /TN "Workflow backup" /TR "<저장소 경로>\.venv\Scripts\platform.exe backup --keep 14"
schtasks /Run /TN "Workflow backup"
```

두 번째 줄은 바로 한 번 돌려 `backups` 폴더에 새 폴더가 생기는지 보는 용도다. 이 계정으로 로그인해 있을 때만 돌고, 그 시각에 PC가 꺼져 있었다면 그날은 건너뛴다. 저장소 경로에 공백이 있으면 `/TR`의 따옴표 규칙이 까다로우니 공백 없는 경로를 쓰거나 작업 스케줄러 화면에서 만든다. 없앨 때는 `schtasks /Delete /TN "Workflow backup" /F`.
