# 샌드박스 Windows 검증 (M3)

설계 10장 M3의 Windows 수동 검증 8단계를 이 PC에서 돌린 기록이다. 1번은 `uv run pytest -m windows`이고, 2~8번도 `tests/sandbox/test_windows_job.py`와 `test_local_backend.py`가 자동으로 확인한다(4번은 상한을 8로 줄여서). M3 시점에는 채팅 화면이 없어서 2~8번은 로컬 백엔드를 직접 부르는 스크립트로 명령을 실행했다. M6 이후에는 미리보기 채팅에서 A11 항목으로 다시 확인한다.

## 환경

2026-10-13, Windows 11 Enterprise 10.0.26300, Windows PowerShell 5.1.26100.9444, Python 3.12.8. 샌드박스 파이썬은 플랫폼 venv가 아닌 기반 인터프리터(`...\Python312\python.exe`)다. 실행 정책은 MachinePolicy·UserPolicy 모두 Undefined, LongPathsEnabled=0, 시스템 코드 페이지 CP949(문화권 ko-KR, UI en-US), 여유 메모리는 약 1GB였다. 설정은 기본값(`SANDBOX_MEMORY_LIMIT_MB=1024`, `SANDBOX_MAX_PROCESSES=64`)이고 데이터 폴더는 임시 폴더를 썼다.

## 다시 돌리는 법

```
uv run pytest -m windows
```

명령 하나를 백엔드로 직접 돌려 보려면 아래처럼 한다. `script`만 바꾸면 2~7번을 그대로 재현할 수 있다.

```python
import asyncio, tempfile, uuid
from pathlib import Path
from app.config import Settings
from app.sandbox.base import ExecSpec, ExecOutput, ExecExited
from app.sandbox.local_workspace import LocalWorkspaceBackend

async def main():
    backend = LocalWorkspaceBackend(Settings(data_dir=Path(tempfile.mkdtemp())))
    handle = await backend.acquire(uuid.uuid4())
    async for event in backend.exec(handle, ExecSpec(exec_id="t1", kind="shell", script="Write-Output '한글'")):
        print(event)
    await backend.aclose()

asyncio.run(main())
```

## 결과

1. `uv run pytest -m windows`: 10개 통과(나머지 585개는 선택 해제). Job 상한 설정 값, 생성·할당·종료, `PeakJobMemoryUsed`·`ActiveProcesses` 조회, `SANDBOX_SHELL_MODE=encoded` 실행을 포함한다. 구조체 크기(`JOBOBJECT_EXTENDED_LIMIT_INFORMATION` = 144바이트)는 `tests/unit/sandbox/test_jobobject_struct.py`가 모든 OS에서 확인한다.

2. `Start-Process powershell -ArgumentList '-c','Start-Sleep 60'` 뒤 kill: 손자 PowerShell이 kill 후 0.08초 안에 사라졌다(`Get-Process -Id`로 확인). 바탕화면에 창이 뜨지 않게 `-WindowStyle Hidden`을 붙여 실행했다. 창 표시 방식은 Job 소속과 관계없다.

3. `run_python`에 `bytearray(2*1024**3)`: `MemoryError`, 종료 코드 1, 93ms. 플랫폼 RSS는 40.1MB에서 40.1MB로 그대로였다. 경계도 확인했다. 512MB는 할당되고 1100MB는 `MemoryError`가 난다.

4. `1..100 | % { Start-Process cmd -ArgumentList '/c','timeout 30' }`: 46개가 뜬 뒤 나머지 54개는 "Not enough quota is available to process this command"로 실패했다. kill 후 남은 cmd는 0개였다. 창 46개가 뜨지 않게 `-WindowStyle Hidden`을 붙였고, `timeout`은 `C:\Windows\System32\timeout.exe`로 불렀다(아래 "알게 된 것" 참고). cmd가 띄우는 timeout.exe도 같은 Job에 들어가 상한을 함께 쓰므로 정확히 64번째에서 멈추지는 않는다(이번에는 47번째부터 실패).

5. `Write-Output '한글'; python -c "print('한글')"`: 출력이 `한글\n한글\n`로 깨지지 않았다. `'x' > a.txt` 뒤 `Get-Content a.txt -Encoding utf8`는 `x`를 돌려줬고, a.txt 바이트는 `EF BB BF 78 0D 0A`(BOM 있는 UTF-8, UTF-16 아님)였다.

6. 셸에서 `New-Item -ItemType Junction -Path j -Target C:\Windows`로 정션을 만든 뒤 `read_file("j/win.ini")`: `path_outside_workspace`. 같은 정션으로의 `write_file`도 거부되고, `list_files`는 `j`를 `link`로만 보여 주고 따라 들어가지 않는다. 작업공간을 지워도 정션이 가리키는 쪽은 건드리지 않는다(`test_links_are_listed_but_not_followed`).

7. `KEEP_RUN_SCRIPTS=true`: `.run/step7.ps1`의 앞 3바이트가 `EF BB BF`(BOM)였다.

8. 플랫폼 프로세스 강제 종료: 작업 관리자 대신, 백엔드로 `Start-Sleep 60`을 띄운 테스트용 부모 파이썬 프로세스를 `taskkill /F /PID`로 죽였다. Job 안의 PowerShell이 0.58초 뒤 사라졌다(`KILL_ON_JOB_CLOSE`).

검증을 마친 뒤 python·cmd·timeout 프로세스는 남지 않았다.

## 알게 된 것

- `PeakJobMemoryUsed`에는 실패한 할당 시도도 잡힌다. 3번에서 2GB 할당이 거부됐는데도 피크가 2.16GB로 나왔다. `tool_call.completed.peak_memory_bytes`는 실제 사용량이 아니라 Job이 본 최고 요청치로 읽어야 한다.
- 도구 프로세스의 PATH 뒤쪽은 플랫폼 프로세스의 PATH를 그대로 이어받는다. 이 PC에서는 Git for Windows의 `usr\bin`이 System32보다 앞에 있어 `timeout`이 GNU coreutils의 timeout으로 잡혔다("Try 'timeout --help'", 종료 코드 125). 서버를 Git Bash에서 띄우면 같은 일이 생긴다. PowerShell 터미널에서 띄우거나 명령에 전체 경로를 쓴다.
- PowerShell 5.1의 `$input`은 표준 입력을 시작 시점의 콘솔 코드 페이지(CP949)로 읽어서, `ExecSpec.stdin`으로 넘긴 한글이 깨진다. 프리앰블이 인코딩을 바꾼 뒤 다시 만들어지는 `[Console]::In`과 파이썬의 `sys.stdin`은 UTF-8로 제대로 읽는다. 지금 도구 스키마(4.4)에는 stdin이 없어 영향은 없다.
- PowerShell 5.1의 `Start-Process -ArgumentList`는 배열을 따옴표 없이 공백으로 잇는다. `'-c','import time; time.sleep(30)'`처럼 주면 파이썬이 `-c import`만 받아 바로 끝난다. 샌드박스 문제는 아니지만 모델이 만든 명령이 조용히 실패하는 흔한 원인이다.
- 콘솔 프로그램마다 따라오는 `conhost.exe`도 Job 계정(`ActiveProcesses`)에 잡힌다. 상한 8로 시험했을 때 PowerShell과 파이썬 7개가 뜬 뒤 생성이 실패했고, 그때 Job이 보고한 활성 프로세스는 16개였다.
- `platform serve --reload`로 띄우면 uvicorn이 Windows에서 SelectorEventLoop를 써서 서브프로세스를 만들 수 없다. 이때 셸·파이썬 도구는 `sandbox_unavailable`("--reload 없이 서버를 띄운다")로 실패한다. 실제로 SelectorEventLoop에서 실행해 확인했다.
