#!/usr/bin/env bash
# 떠 있는 로컬 서버(uv run platform serve)가 기본 응답을 하는지 확인한다. 하나라도 실패하면 종료 코드 1.
set -uo pipefail

cd "$(dirname "$0")/.."

# Git Bash(Windows)가 /로 시작하는 인자를 Windows 경로로 바꾸지 않게 한다.
export MSYS_NO_PATHCONV=1

port="${SERVER_PORT:-$(sed -n 's/^SERVER_PORT=//p' .env 2>/dev/null | tail -n 1 | tr -d '\r[:space:]')}"
base="http://127.0.0.1:${port:-8500}"

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

status() {
  curl -s -o /dev/null -w '%{http_code} %{content_type}' "$@"
}

check "/healthz" '"status": ?"ok"' curl -s "$base/healthz"
check "/healthz db" '"db": ?"ok"' curl -s "$base/healthz"
check "/healthz rss_mb" '"rss_mb": ?[0-9]' curl -s "$base/healthz"
check "/readyz" '^200 ' status "$base/readyz"
check "/ (화면 또는 빌드 안내)" '^200 text/html' status "$base/"
check "/agents/abc (화면 라우트)" '^200 text/html' status "$base/agents/abc"
check "/api/nope는 JSON 404" '^404 application/json' status "$base/api/nope"
check "Host 변조는 400" '^400 ' status -H 'Host: evil.example' "$base/healthz"

if [ "$failed" = 0 ]; then
  echo "모두 통과"
fi
exit "$failed"
