#!/usr/bin/env bash
# 개발용으로 전체 서비스를 띄운다. api와 runner는 소스 마운트와 자동 리로드로 뜬다.
# 추가 인자는 docker compose up에 그대로 넘긴다(예: bash scripts/dev-up.sh api).
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo ".env가 없다. cp .env.example .env 후 값을 채운다." >&2
  exit 1
fi

docker compose --env-file .env \
  -f infra/docker/compose.yaml -f infra/docker/compose.dev.yaml \
  up -d --build --wait "$@"
