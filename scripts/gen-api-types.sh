#!/usr/bin/env bash
# 떠 있는 서버의 /openapi.json으로 apps/web/src/api/schema.d.ts를 다시 만든다(설계 7.1).
# 포트는 SERVER_PORT 환경변수, 없으면 .env의 SERVER_PORT, 그것도 없으면 8500이다.
set -euo pipefail

cd "$(dirname "$0")/.."

port="${SERVER_PORT:-$(sed -n 's/^SERVER_PORT=//p' .env 2>/dev/null | tail -n 1 | tr -d '\r[:space:]')}"
url="http://127.0.0.1:${port:-8500}/openapi.json"

if ! curl -sf "$url" >/dev/null; then
  echo "$url 에서 OpenAPI 문서를 받지 못했다. uv run platform serve로 서버를 먼저 띄운다." >&2
  exit 1
fi

# 출력 경로는 apps/web 기준이다(pnpm --dir가 작업 위치를 바꾼다).
corepack pnpm --dir apps/web exec openapi-typescript "$url" --output src/api/schema.d.ts
