# 백업과 복구

백업은 `uv run platform backup` 하나다. 결과 폴더의 구조가 `DATA_DIR`과 같아서 복구에도 따로 명령이 없고, 그 폴더를 `DATA_DIR`로 쓰면 된다. 다만 `.env`와 `ENCRYPTION_KEY`는 백업에 들어가지 않으니 따로 챙겨야 한다. 주기적으로 돌리는 예시는 [operations.md](operations.md#정기-백업)에 있다.

## 들어가는 것

| 항목 | 백업에 | 비고 |
|---|---|---|
| `app.db` | 들어감 | SQLite 온라인 백업으로 복사한다. 서버가 떠 있어도 되고 복사본에는 WAL 내용까지 합쳐진다. `-wal`, `-shm` 파일은 남기지 않는다 |
| `storage\` | 들어감 | 에이전트 첨부, 스킬, 업로드, 세션 결과 파일, 발행 스냅샷, 큰 도구 출력. 쓰다 만 `*.part`는 뺀다 |
| `allowlist.txt` | 들어감 | `http_request` 허용 목록. `DATA_DIR`의 이 파일만 복사하므로 `HTTP_ALLOWLIST_FILE`로 다른 파일(예: `DATA_DIR` 밖)을 쓰고 있다면 그 파일은 들어가지 않는다. 따로 챙긴다 |
| `workspaces\` | `--with-workspaces`일 때만 | 설치한 패키지 때문에 클 수 있다. 결과 파일은 이미 `storage\session-files`에 있다. `.deleted-*`는 뺀다 |
| `cache\`, `logs\` | 안 들어감 | pip 캐시와 로그는 다시 생긴다 |
| `.env` | 안 들어감 | 키가 든 파일이라 넣지 않는다 |

백업 폴더에는 `manifest.json`이 같이 들어간다. 만든 시각(`created_at`), 원본 데이터 폴더(`source_data_dir`), 스키마 리비전(`alembic_revision`), `--with-workspaces` 여부(`with_workspaces`), 파일 수(`files`)와 바이트 수(`bytes`), 복사한 DB를 `PRAGMA quick_check`로 점검한 결과(`quick_check`), 암호화 키 fingerprint(`encryption_key_fingerprint`)가 적혀 있다. 키 값은 어디에도 들어가지 않는다.

## 백업하기

```bash
uv run platform backup
uv run platform backup --with-workspaces
uv run platform backup --out D:/wf-backup/20261013
```

기본 결과는 저장소 루트의 `backups\<YYYYMMDD-HHMMSS>\`이고 이 폴더는 `.gitignore`에 들어 있다. `--out <폴더>`는 결과 폴더 자체의 경로이고 아직 없는 경로여야 한다. 이미 있으면 덮어쓰지 않고 멈춘다. 쓰는 동안은 `<폴더>.part`에 만들고 끝나면 이름을 바꾸므로 `.part`가 붙은 폴더는 쓰다 만 것이다. 실패하면 명령이 `.part`를 지운다.

끝나면 만든 폴더와 파일 수·크기, 스키마 리비전, 암호화 키 fingerprint를 보여 준다. 작업공간 안의 링크와 정션은 따라가지 않고 건너뛰는데, 모델이 만든 정션이 PC의 다른 폴더를 가리킬 수 있어서다. 건너뛴 개수는 알려 준다. 복사한 DB의 `quick_check`가 실패하면 종료 코드가 1이고, 그때는 예전 백업을 정리하지 않는다.

`--keep N`은 기본 위치에 만들 때 쓰는 보존 개수이고 기본값은 5다. 이름이 타임스탬프 꼴이고 `manifest.json`이 든 폴더만 세어서 최근 N개를 남기고 오래된 것을 지운다. 직접 만든 다른 이름의 폴더는 건드리지 않는다. `--out`을 줬을 때는 아무것도 정리하지 않고, `--keep`을 같이 주면 무시한다고 알려 준다.

백업은 암호화하지 않는다. 대화 내용과 파일이 그대로 들어 있으니 외부 드라이브나 클라우드로 복사할 때는 그걸 알고 한다.

## 서버가 떠 있을 때

떠 있어도 백업은 된다. SQLite 백업이 읽기만 하므로 서버의 쓰기를 막지 않는다. 서버가 떠 있으면 명령이 '주의'로 시작하는 경고를 한 줄 내고 계속한다. 런이 도는 중이면 DB와 `storage`가 서로 어긋날 수 있다는 내용이다. 예를 들어 DB에는 결과 파일 행이 있는데 `storage`의 복사본은 아직 없는 식이다. 쓰는 중이던 파일은 다음 백업에 들어간다. 런이 없을 때 하는 게 낫고, 꼭 남겨야 하는 백업은 서버를 끄고 한다.

## 복구하기

복구하려면 서버를 끄고 아래 순서로 한다. 지금 데이터는 지우지 말고 옆으로 치워 둔다.

```powershell
Rename-Item C:\wf-data wf-data.before-restore
Copy-Item -Recurse <저장소 경로>\backups\20261013-180000 C:\wf-data
```

`Copy-Item`은 대상 폴더가 이미 있으면 그 안에 하위 폴더로 복사하니 위에서 치웠는지 확인한다. 그다음 점검하고 띄운다.

```bash
uv run platform doctor
uv run platform serve
```

doctor의 암호화 키 항목은 아래 '키 보관과 fingerprint'에서 비교한다. 백업이 지금 코드보다 오래된 스키마라면 서버가 뜰 때 마이그레이션이 자동으로 올린다. 미리 올려 두려면 `uv run platform migrate`를 먼저 돌리고, 백업의 스키마 버전은 `manifest.json`의 `alembic_revision`으로 안다.

`.env`의 `DATA_DIR`을 백업 폴더로 바꿔 복사 없이 띄우는 방법도 있다. 다만 서버가 그 폴더에 계속 쓰기 때문에 백업이 더는 백업이 아니게 된다. 내용을 잠깐 열어 보는 용도로만 쓰고, 계속 쓸 데이터는 복사해서 쓴다. 백업 폴더 경로가 80자를 넘으면 doctor가 데이터 폴더 경고를 내기도 한다.

`--with-workspaces` 없이 만든 백업은 작업공간 폴더가 없다. 다음 런이 빈 폴더로 새로 만들고 '작업공간 폴더가 없어 새로 만들었습니다'라고 알린다. 그 세션에서 설치했던 패키지와 작업 중이던 파일은 없지만, 결과 파일은 `storage`에 있어서 그대로 내려받을 수 있다.

## 키 보관과 fingerprint

`ENCRYPTION_KEY`는 에이전트 환경변수를 암호화하는 키이고 `.env`에만 있다. 백업 폴더만 새어도 값이 풀리지 않도록 일부러 백업에 넣지 않았다. 그래서 비밀번호 관리자 같은 다른 안전한 곳에 한 벌 따로 보관해 둔다. 이 키를 잃으면 DB에 든 환경변수 값은 되살릴 수 없다. 에이전트, 대화, 파일 같은 나머지는 영향이 없다.

복구한 PC의 `.env`가 같은 키인지는 fingerprint로 본다. 키 32바이트의 sha256 앞 8자라서 키를 드러내지 않고 일치 여부만 알 수 있다. 백업 쪽 값은 `manifest.json`에 있고, 지금 `.env`의 값은 doctor의 '암호화 키' 항목에 나온다.

```bash
python -c "import json;print(json.load(open('backups/20261013-180000/manifest.json', encoding='utf-8'))['encryption_key_fingerprint'])"
uv run platform doctor
```

두 값이 같으면 된다. 키를 설정하지 않은 상태로 만든 백업이면 manifest 값이 null이다.

## 복구 뒤 확인

서버를 띄운 뒤 아래를 본다. 모두 백업을 만든 시점 그대로 보이면 복구가 된 것이다.

1. 백업 때의 계정과 비밀번호로 로그인된다.
2. 에이전트 목록에 에이전트와 발행 상태, 버전 이력이 있다.
3. 로그 화면에 세션과 메시지가 있고, 세션에서 결과 파일을 내려받을 수 있다.
4. 발행한 에이전트의 웹앱에서 새 대화를 열면 첫 `Get-ChildItem`에 발행 때의 파일이 보인다. 스냅샷에서 복원된다는 뜻이다.
5. 환경변수가 있는 에이전트로 미리보기 런을 한 번 돌려도 복호화 오류가 나지 않는다.

확인 기록(2026-10-13): 시험 서버(8503)가 떠 있는 채 `--out`으로 백업하자 '주의' 한 줄이 나왔고 결과는 파일 4개, 429KB, `-wal` 없는 `app.db`였다. 서버를 끄고 그 폴더를 `DATA_DIR`로 띄우니 doctor와 manifest의 fingerprint가 같았고 1~3번과 5번이 모두 맞았다. 4번은 발행 스냅샷 zip이 백업에 들어 있는 것까지만 봤다. `--with-workspaces` 없이 만든 백업이라 첫 런에 '작업공간 폴더가 없어 새로 만들었습니다'가 나왔다. 기본 위치에서 `--keep 2`, `--keep 1`은 타임스탬프 폴더만 지우고 직접 만든 폴더는 남겼다.

## 다른 PC로 옮기기

새 PC에는 [install.md](install.md)대로 저장소를 받고 `uv sync`와 화면 build까지 해 둔다. 그다음 백업 폴더를 옮겨서(USB, 외부 드라이브) 새 PC의 `DATA_DIR` 자리에 복사한다. 패키지와 작업 중이던 파일까지 가져가려면 백업을 `--with-workspaces`로 만든다.

`.env`는 새로 만들되 `ENCRYPTION_KEY`는 옛 PC의 값을 그대로 넣는다. 새로 만들면 옛 환경변수를 풀 수 없다. `GEMINI_API_KEYS`는 옮기거나 새로 넣는다. `DATA_DIR`은 새 PC에서 짧게 잡은 경로면 되고 옛 PC와 같을 필요가 없다. DB는 작업공간의 옛 경로를 기억하지만 서버가 지금의 `DATA_DIR` 기준으로 다시 계산한다. `manifest.json`의 `source_data_dir`는 어디서 만든 백업인지 알려 주는 참고용이다. 브라우저의 로그인은 새 PC에서 다시 한다. 끝으로 doctor로 fingerprint를 비교하고 서버를 띄워 위의 복구 뒤 확인을 한다.

## 키가 다를 때

`.env`의 `ENCRYPTION_KEY`가 백업 때의 키와 다르면 서버는 뜨고 대부분 평소처럼 쓸 수 있다. 문제는 환경변수를 가진 에이전트뿐이다. 그 에이전트로 런을 보내면 모델을 부르기 전에 `env_decrypt_failed` 오류로 끝나고, 메시지는 '환경변수 <이름>을(를) 복호화하지 못했다. ENCRYPTION_KEY가 바뀌었는지 확인한다'이다. 빌더의 환경변수 탭에서는 비밀이 아닌 값도 비어 보이고 비밀 값은 '저장된 값 유지'로만 나온다.

맞는 키를 찾았으면 `.env`에 되돌리고 서버를 다시 띄운다. 못 찾겠으면 그 에이전트의 환경변수 값을 다시 입력해 저장한다. 새 키로 다시 암호화된다. 발행한 버전에도 옛 암호문이 그대로 있으니 웹앱이나 API로 쓰는 에이전트는 초안을 고친 뒤 다시 발행해야 한다. 키가 아예 비어 있으면 환경변수 저장이 `encryption_key_missing`(503)으로 거절된다.
