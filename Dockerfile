# 故意不写 `# syntax=docker/dockerfile:x.y`：本文件只用经典指令（没有 --mount、
# heredoc、--link），BuildKit 自带的前端就够。钉了它等于每次构建先去镜像站拉一次
# 前端镜像，那个站一抖（502）整场构建还没开始就失败。
# ============================================================
#  Stage 1: build the Vue 3 frontend
# ============================================================
FROM docker.1ms.run/library/node:20-alpine AS frontend-builder

WORKDIR /web

# Install dependencies first for better layer caching.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com \
 && (npm ci || npm install)

# Copy the rest of the frontend source and build.
COPY frontend/ ./
RUN npm run build


# ============================================================
#  Stage 2: backend runtime image (FastAPI + static frontend)
# ============================================================
# 3.12 而不是 3.13：requirements.txt 钉死的 sqlalchemy==2.0.35 只发到 cp312 为止
# （PyPI 上无 cp313 wheel），3.13 会退回 sdist 源码编译，slim 镜像里没有编译器，
# C 扩展装不上就静默降级成纯 Python 路径。3.12 与 pyproject 的 >=3.12,<3.14 一致。
FROM docker.m.daocloud.io/library/python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# Install Python dependencies.
COPY backend/requirements.txt ./requirements.txt
# Use a China mirror for apt as well; deb.debian.org is painfully slow from CN.
# (tuna 403s on trixie pool files, so use USTC.)
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --upgrade pip \
 && pip install -r requirements.txt

# Copy backend source.
COPY backend/app ./app

# Copy built frontend into the static directory served by FastAPI.
COPY --from=frontend-builder /web/dist /app/static

# Create a non-root user and hand over the app dir.
# /app/uploads must exist here: a named volume takes its ownership from this
# path in the image, and if the path is missing the volume comes up root-owned
# while the app runs as yangvis, so every image upload fails with EACCES.
RUN useradd --create-home --shell /bin/bash yangvis \
 && mkdir -p /app/uploads \
 && chown -R yangvis:yangvis /app
USER yangvis

EXPOSE 18099

# Healthcheck against the FastAPI endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:18099/api/health || exit 1

# Use Gunicorn with the Uvicorn worker class for production.
#
# --timeout 300: indexing a document runs as a background task inside the
# worker and can take minutes on a large upload; the default 60s would have the
# arbiter kill the worker mid-job. The same headroom is what the streaming chat
# endpoint will need.
# Worker count is configurable via GUNICORN_WORKERS: on a small (1GB) box
# 4 langchain-laden workers will OOM, so scripts/docker-run.ps1 passes a lower value.
# sh -c + exec keeps gunicorn as PID 1 so SIGTERM still reaches it.
CMD ["sh", "-c", "exec gunicorn app.main:app \
     -w \"${GUNICORN_WORKERS:-4}\" \
     -k uvicorn.workers.UvicornWorker \
     -b 0.0.0.0:18099 \
     --access-logfile - \
     --error-logfile - \
     --timeout 300 \
     --graceful-timeout 30 \
     --max-requests 1000 \
     --max-requests-jitter 100"]
