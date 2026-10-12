#!/usr/bin/env bash
# 샌드박스 이미지를 빌드한다. apps/sandboxd를 복사해야 해서 저장소 루트를 컨텍스트로 쓴다.
set -euo pipefail

cd "$(dirname "$0")/../.."
tag="${1:-platform-sandbox:dev}"
docker build -f infra/sandbox-image/Dockerfile -t "$tag" .
