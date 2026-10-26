#!/usr/bin/env bash
# ---------------------------------------------------------------
#  Build script for yangvis
#  Builds the multi-stage Docker image only. The frontend bundle is
#  built inside the image (frontend-builder stage); running npm build
#  on the host would be wasted work -- frontend/dist is excluded by
#  .dockerignore and never reaches the build context.
#    IMAGE_TAG=my:tag ./scripts/build.sh
# ---------------------------------------------------------------
set -euo pipefail

# 脚本位于 scripts/，构建上下文（Dockerfile 所在处）是项目根
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

IMAGE_TAG="${IMAGE_TAG:-yangvis:latest}"

echo "==> Building Docker image (${IMAGE_TAG})..."
docker build -t "${IMAGE_TAG}" "${ROOT}"

echo "==> Done. Image: ${IMAGE_TAG}"
echo "    Run with: docker run -d -p 18099:18099 ${IMAGE_TAG}"
echo "    Or:       docker-compose up -d --build"
