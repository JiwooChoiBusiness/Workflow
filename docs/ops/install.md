# 설치

새 PC나 새 폴더에서 처음부터 깔아 서버를 띄우고 첫 에이전트가 답하는 것까지의 절차다. 평소 쓰는 법은 [operations.md](operations.md)에 있다. 명령은 PowerShell과 Git Bash에서 똑같이 돌아가고, `bash scripts/...`처럼 bash가 필요한 것만 Git Bash에서 돌린다.

## 필요한 것

Windows 11, Python 3.12, uv, Node 24(corepack이 들어 있는 버전)가 필요하다. Docker, Postgres, Redis는 쓰지 않는다. Git Bash는 스모크 스크립트를 돌릴 때만 있으면 된다.

에이전트의 파이썬 도구는 서버 venv가 아니라 설치된 기반 인터프리터를 쓴다. 그래서 pip이 들어 있는 python.org 설치판 3.12가 무난하고, 아래 `doctor`가 pip까지 확인해 준다.

uv는 winget으로 깐다.

```powershell
winget install --id=astral-sh.uv -e
```

새 터미널에서 `uv --version`이 나오면 된다. PATH는 새로 연 터미널부터 반영되니 안 나오면 터미널을 닫았다 다시 연다. pip으로 깐 uv(`python -m pip install uv`)는 PATH에 `uv`가 없어도 `python -m uv`로 부를 수 있다. 이 문서와 README의 `uv ...`는 그럴 때 `python -m uv ...`로 바꿔 읽으면 된다.

## 받아서 설정하기

```bash
git clone https://github.com/JiwooChoiBusiness/Workflow.git
cd Workflow
cp .env.example .env
```

비공개 저장소라 처음 받을 때 GitHub 로그인이 필요하다. `.env`는 저장소에 올라가지 않는 파일이다. VS Code 같은 편집기로 열어서 값을 채운다. PowerShell의 `>`로 만들면 UTF-16 파일이 되어 서버가 읽지 못한다. 꼭 채울 것은 `ENCRYPTION_KEY`, `GEMINI_API_KEYS`, `DATA_DIR`이고 나머지는 기본값 그대로 두면 된다. 각 항목의 뜻은 `.env.example`의 주석에 있다.

`ENCRYPTION_KEY`는 에이전트 환경변수의 비밀값을 암호화하는 키다. 아래 명령이 찍는 한 줄을 그대로 붙여 넣는다. 이 키를 잃으면 암호화된 값을 되살릴 수 없으니 `.env`와 별개로 한 군데 더 보관한다([backup-restore.md](backup-restore.md#키-보관과-fingerprint)).

```bash
python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

`GEMINI_API_KEYS`에는 Gemini API 키를 넣는다. 쉼표로 여러 개를 이으면 앞에서부터 쓰다가 429를 받으면 다음 키로 넘어간다. `DATA_DIR`은 DB, 파일, 작업공간이 쌓이는 폴더이고 `C:\wf-data`처럼 짧은 절대 경로로 둔다. 따옴표는 붙이지 않는다. 작업공간 안의 패키지 경로가 Windows의 260자 제한에 걸리지 않게 하려는 것이라 80자를 넘으면 `doctor`가 경고한다. 비워 두면 저장소의 `data`를 쓴다.

키 없이 화면부터 둘러보고 싶으면 `GEMINI_API_KEYS`는 비워 두고 `LLM_FAKE=true`를 넣는다. 모델 대신 정해진 각본이 답하는 가짜 어댑터라 실제 모델은 부르지 않는다. 실제로 쓸 때는 지운다.

의존성을 받고 화면을 빌드한다. 처음 쓰는 `corepack pnpm`은 pnpm을 내려받으므로 인터넷이 필요하다.

```bash
uv sync
corepack pnpm --dir apps/web install
corepack pnpm --dir apps/web build
```

build가 끝나면 `apps/web/dist`가 생기고 서버가 이걸 서빙한다. 메모리가 넉넉하지 않은 PC에서는 build를 pytest 같은 다른 무거운 작업과 겹쳐 돌리면 `Out of memory`로 죽으니 혼자 돌린다.

## 점검하고 띄우기

```bash
uv run platform doctor
```

셸, 샌드박스 파이썬과 pip, 데이터 폴더, 긴 경로 정책, 실행 정책, 포트, 서브프로세스, 암호화 키를 차례로 보여 준다. `OK`면 통과이고 `WARN`은 같은 줄에 고칠 방법이 붙는다. WARN이 하나라도 있으면 종료 코드가 1이다. 암호화 키는 `.env`의 `ENCRYPTION_KEY`가 비어 있거나 형식이 틀리면 WARN이고, 맞으면 fingerprint를 보여 준다. 서버가 이미 떠 있을 때 돌리면 포트 항목이 WARN으로 나오는데 정상이다.

```bash
uv run platform serve
```

브라우저로 http://127.0.0.1:8500 을 연다. 콘솔에 JSON 로그가 흐르고 `server.ready`가 나오면 준비된 것이다. Ctrl+C로 끄면 남는 프로세스가 없다. 끈 뒤에 PowerShell에서 `Get-Process python,powershell`을 쳐서 서버가 띄운 것이 남지 않았는지 볼 수 있다(지금 쓰는 터미널의 powershell은 목록에 보인다).

## 첫 화면

처음 접속하면 '처음 설정' 화면이 뜬다. 이메일과 비밀번호(12자 이상)를 넣고 계정을 만들면 로그인된 채 에이전트 목록으로 넘어간다. 계정은 하나만 만들 수 있고 만든 뒤에는 이 화면이 다시 열리지 않는다. 비밀번호를 잃으면 서버를 끄고 `uv run platform reset-password`로 바꾼다.

계정을 만들었으면 설정 → LLM에서 '연결 테스트'로 키가 읽혔는지 본다. 키 값은 화면 어디에도 나오지 않고 번호와 끝 4자리만 보인다. `LLM_FAKE=true`로 띄웠다면 가짜 어댑터로 동작 중이라는 안내와 `GEMINI_API_KEYS`가 비어 있다는 경고가 나오는 게 정상이니 넘어간다. 이어서 에이전트 → 새 에이전트를 만들고 미리보기에 메시지를 보낸다. 셸을 쓰는 답이면 명령 전문이 담긴 승인 카드가 먼저 뜨고, 승인하면 도구 카드에 출력이 흐른다. `LLM_FAKE=true`일 때는 `echo fake-llm`을 실행하는 카드가 뜬다. 가짜 어댑터는 메시지가 '질문: 내용'으로 시작하면 질문 카드를, '파일: 내용'으로 시작하면 결과 파일(`outputs/결과.txt`)을 만들어 보여 준다. 여기까지 되면 설치는 끝난 것이다.

## 백신 검사 제외

작업공간 폴더는 에이전트가 만든 파일과 설치한 패키지로 파일이 많이 생기고 지워지는 곳이다. 백신이나 검색 인덱서가 그 파일을 잡고 있으면 삭제, 복원, 스냅샷이 실패할 수 있어서 `DATA_DIR` 아래 `workspaces`(예: `C:\wf-data\workspaces`)는 검사 제외에 넣어 두는 게 좋다. Windows 보안의 바이러스 및 위협 방지에서 설정 관리 → 제외 항목 추가 또는 제거 → 폴더로 추가한다. 정책 때문에 못 넣는 PC에서도 쓸 수는 있다. 잠겨서 못 지운 작업공간 폴더는 서버가 `.deleted-` 이름으로 바꿔 두었다가 매시 다시 지운다.

## 문제가 생기면

uv를 찾지 못한다고 나오면 PATH가 반영되지 않은 터미널이다. 새 터미널을 열고, 그래도 없으면 위의 `python -m uv`를 쓴다.

doctor의 실행 정책이 WARN이고 `AllSigned`나 `Restricted`가 보이면 회사 그룹 정책이 PowerShell 스크립트 실행을 막는 것이다. `.env`에 `SANDBOX_SHELL_MODE=encoded`를 넣고 서버를 다시 띄운다. 명령을 스크립트 파일 대신 `-EncodedCommand`로 넘기는 방식이라 한 번에 8,000자까지만 실행된다.

데이터 폴더가 WARN이면 `DATA_DIR`이 80자를 넘는 것이다. 더 짧은 경로로 옮긴다. Windows의 긴 경로 정책(`LongPathsEnabled`)은 건드리지 않기로 했다. 이미 쌓인 데이터가 있으면 서버를 끄고 폴더를 옮긴 뒤 `.env`의 `DATA_DIR`만 고치면 된다.

포트 항목이 WARN이거나 서버가 곧바로 죽으면 8500을 다른 프로세스가 쓰고 있는 것이다. 먼저 이전에 띄운 서버가 남아 있지 않은지 본다.

```powershell
Get-NetTCPConnection -LocalPort 8500 -State Listen | Select-Object OwningProcess
Get-Process -Id <위에서 나온 번호>
```

다른 프로그램이 쓰는 포트면 `.env`의 `SERVER_PORT`를 바꾼다. 8000과 8080은 피한다.

Git Bash에서 doctor나 backup의 한글이 `����`처럼 깨져 나오면 파이썬이 출력을 시스템 코드 페이지(CP949)로 내보내서다. `PYTHONUTF8=1 uv run platform doctor`처럼 앞에 붙이거나 PowerShell에서 돌린다. `serve --reload`의 `reload:` 안내도 같고, 서버 로그는 JSON이라 영향이 없다.

접속했는데 '화면이 아직 빌드되지 않았다'는 안내만 보이면 `corepack pnpm --dir apps/web build`를 하고 서버를 다시 띄운다. build가 `Out of memory`로 죽으면 다른 프로그램을 닫고 build만 단독으로 다시 돌린다.

도구 출력은 UTF-8로 맞춰 실행하므로 한글이 깨지지 않는다. 다만 PowerShell 구문 오류처럼 셸이 직접 내는 일부 메시지는 시스템 코드 페이지(한국어 Windows는 CP949)로 나와 깨져 보일 수 있다. 그런 메시지는 대개 모델이 만든 명령에 구문 오류가 있다는 뜻이고 서버 문제가 아니다. 서버 로그의 한글은 `\uXXXX`로 이스케이프되어 있고 읽는 법은 [operations.md](operations.md#로그와-헬스)에 있다.
