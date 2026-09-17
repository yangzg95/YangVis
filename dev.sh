#!/usr/bin/env bash
# ---------------------------------------------------------------
#  Dev start script for yangvis
#    ./dev.sh            start backend + frontend
#    ./dev.sh backend    start backend only
#    ./dev.sh frontend   start frontend only
#  Backend : http://localhost:18099  (docs: /api/docs)
#  Frontend: http://localhost:5173   (/api proxied to backend)
# ---------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

TARGET="${1:-all}"

if [ ! -f backend/.env ]; then
  echo "==> backend/.env not found, copying from .env.example"
  cp backend/.env.example backend/.env
  echo "    Please fill in __CHANGE_ME__ values in backend/.env"
fi

start_backend() {
  echo "==> Syncing python dependencies (uv sync)..."
  uv sync
  echo "==> Starting backend on http://localhost:18099"
  (cd backend && uv run --project "${ROOT}" uvicorn app.main:app --host 0.0.0.0 --port 18099 --reload)
}

start_frontend() {
  cd "${ROOT}/frontend"
  if [ ! -d node_modules ]; then
    echo "==> Installing frontend dependencies..."
    if [ -f package-lock.json ]; then npm ci; else npm install; fi
  fi
  echo "==> Starting frontend on http://localhost:5173"
  npm run dev
}

case "${TARGET}" in
  backend)  start_backend ;;
  frontend) start_frontend ;;
  all)
    start_backend &
    BACKEND_PID=$!
    trap 'kill ${BACKEND_PID} 2>/dev/null || true' EXIT INT TERM
    start_frontend
    ;;
  *)
    echo "Usage: ./dev.sh [all|backend|frontend]"
    exit 1
    ;;
esac
