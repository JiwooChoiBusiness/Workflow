#!/usr/bin/env bash
# CI e2e 잡의 시험 한 묶음. 가짜 어댑터 서버를 띄워 헬스를 기다리고, 그 서버를 대상으로 Playwright를
# 돌린 뒤 서버를 내린다. Windows 러너에서는 한 스텝에서 띄운 백그라운드 서버가 다음 스텝까지 살아
# 있다는 보장이 없어서 띄우기와 내리기를 시험과 같은 스텝에 묶는다.
#
#   bash scripts/ci-e2e.sh <포트> <데이터 폴더> [playwright test 인자...]
#
# 서버 로그는 $RUNNER_TEMP(없으면 임시 폴더)/e2e-<포트>.log에 남는다. GitHub Actions에서 실패하면 그
# 로그의 오류 줄 몇 개를 주석으로도 낸다(scripts/ci-log-annotations.py). LLM_FAKE_SCRIPT, E2E_FAKE_SCRIPT,
# E2E_SQL_RUNTIME, ENCRYPTION_KEY 같은 환경변수는 서버와 Playwright에 그대로 간다. GEMINI_API_KEYS가
# 없으면 쉼표 하나(키 없음)로 띄운다. uv·pnpm이 PATH에 없으면 UV·PNPM으로 명령을 준다
# (예: PNPM="corepack pnpm"). 저장소의 .env가 있는 PC에서 돌릴 때는 GEMINI_API_KEYS와
# GEMINI_PAID_API_KEY를 둘 다 더미 값으로 줘서 서버가 .env의 키를 읽지 않게 한다.
set -uo pipefail

port=$1
data=$2
shift 2
cd "$(dirname "$0")/.."
log="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/e2e-$port.log"
read -r -a uv <<< "${UV:-uv}"
read -r -a pnpm <<< "${PNPM:-pnpm}"

SERVER_PORT=$port DATA_DIR=$data LLM_FAKE=true GEMINI_API_KEYS=${GEMINI_API_KEYS:-,} PYTHONUTF8=1 \
  nohup "${uv[@]}" run --no-sync platform serve > "$log" 2>&1 &
server=$!

stop() {
  # uv가 띄운 파이썬 서버까지 트리째 끝낸다. Git Bash의 /proc/<pid>/winpid가 Windows 프로세스 id다.
  if [ -r "/proc/$server/winpid" ]; then
    taskkill //F //T //PID "$(cat "/proc/$server/winpid")" > /dev/null 2>&1 || true
  else
    kill "$server" 2> /dev/null || true
  fi
  wait "$server" 2> /dev/null || true
}
trap stop EXIT

# 러너는 영어판 Windows라 파이썬이 파이프에 cp1252로 써서 한글 주석에서 멈춘다. UTF-8로 둔다.
annotate_log() {
  [ -n "${GITHUB_ACTIONS:-}" ] && [ -s "$log" ] || return 0
  PYTHONUTF8=1 "${uv[@]}" run --no-sync python scripts/ci-log-annotations.py "$log" "$port" || true
}

up=""
for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:$port/healthz" > /dev/null; then
    up=1
    break
  fi
  kill -0 "$server" 2> /dev/null || break
  sleep 1
done
if [ -z "$up" ]; then
  echo "시험 서버($port)가 뜨지 않았다. 서버 로그:" >&2
  cat "$log" >&2
  annotate_log
  exit 1
fi

(cd apps/web && E2E_BASE_URL="http://127.0.0.1:$port" "${pnpm[@]}" exec playwright test "$@")
status=$?
if [ "$status" -ne 0 ]; then
  annotate_log
fi
exit "$status"
