#!/usr/bin/env bash
# ---------------------------------------------------------------
#  Build script for yangvis
#  1) Build the frontend bundle
#  2) Build the Docker image (multi-stage)
# ---------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

IMAGE_TAG="${IMAGE_TAG:-yangvis:latest}"

echo "==> [1/2] Building frontend..."
cd frontend
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi
npm run build
cd ..

echo "==> [2/2] Building Docker image (${IMAGE_TAG})..."
docker build -t "${IMAGE_TAG}" .

echo "==> Done. Image: ${IMAGE_TAG}"
echo "    Run with: docker run -d -p 18099:18099 ${IMAGE_TAG}"
echo "    Or:       docker-compose up -d --build"
