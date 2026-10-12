#!/usr/bin/env bash
# compose로 띄운 서비스가 모두 응답하는지 확인한다. 하나라도 실패하면 종료 코드 1.
set -uo pipefail

cd "$(dirname "$0")/.."

# Git Bash(Windows)가 /로 시작하는 인자를 Windows 경로로 바꾸지 않게 한다.
export MSYS_NO_PATHCONV=1

compose() {
  docker compose --env-file .env -f infra/docker/compose.yaml "$@"
}

port="${GATEWAY_HTTP_PORT:-$(sed -n 's/^GATEWAY_HTTP_PORT=//p' .env 2>/dev/null | tail -n 1)}"
gateway="http://127.0.0.1:${port:-8080}"

failed=0

report() {
  local name="$1" ok="$2" detail="$3"
  if [ "$ok" = 1 ]; then
    echo "OK    $name"
  else
    echo "FAIL  $name: $detail"
    failed=1
  fi
}

check() {
  local name="$1" pattern="$2" output
  shift 2
  output="$("$@" 2>&1)"
  if grep -qE -- "$pattern" <<<"$output"; then
    report "$name" 1 ""
  else
    report "$name" 0 "$(head -c 300 <<<"$output")"
  fi
}

# 컨테이너 안에서 URL 상태 코드를 찍는다. 두 번째 인자가 있으면 그 프록시를 거친다.
http_status() {
  local service="$1"
  shift
  compose exec -T "$service" python - "$@" <<'PY'
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
handlers = []
if len(sys.argv) > 2:
    handlers.append(urllib.request.ProxyHandler({"http": sys.argv[2], "https": sys.argv[2]}))
opener = urllib.request.build_opener(*handlers)
try:
    print(opener.open(url, timeout=20).status)
except urllib.error.HTTPError as e:
    print(e.code)
except OSError as e:
    print(e)
PY
}

check "gateway /" '^200$' curl -s -o /dev/null -w '%{http_code}' "$gateway/"
check "gateway /api/healthz" '"status": ?"ok"' curl -s "$gateway/api/healthz"
check "gateway /internal 차단" '^403$' curl -s -o /dev/null -w '%{http_code}' "$gateway/internal/x"
check "api /healthz" '^200$' http_status api http://127.0.0.1:5001/healthz
check "api /readyz" '^200$' http_status api http://127.0.0.1:5001/readyz
check "sandbox-manager /healthz (runner에서)" '^200$' \
  http_status runner http://sandbox-manager:7100/healthz
check "runner 하트비트" '^fresh$' compose exec -T runner python -c \
  "import os, time; print('fresh' if time.time() - os.path.getmtime('/tmp/runner-heartbeat') < 30 else 'stale')"
check "redis" '^PONG' compose exec -T redis redis-cli ping
check "postgres" 'accepting connections' compose exec -T postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
check "egress-proxy: pypi.org 허용" '^200$' \
  http_status runner https://pypi.org/simple/pip/ http://egress-proxy:3128
check "egress-proxy: example.org 차단" '403' \
  http_status runner https://example.org/ http://egress-proxy:3128

if [ "$failed" = 0 ]; then
  echo "모두 통과"
fi
exit "$failed"
