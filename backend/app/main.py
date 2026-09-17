"""yangvis 的 FastAPI 应用入口。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap import (
    bootstrap_admin,
    create_tables,
    migrate_schema,
    reset_stale_jobs,
    seed_agents,
    validate_settings,
)
from app.config import get_settings
from app.errors import BusinessError
from app.models.schemas import APIResponse
from app.routers import (
    agents,
    auth,
    chat,
    knowledge,
    netdisk,
    ops,
    ops_files,
    resume,
    settings as settings_router,
    users,
)

logger = logging.getLogger("yangvis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="yangvis backend API",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS 放开，方便本地开发时 SPA 直接调用后端 API。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 路由 ---------------------------------------------------------------

API_PREFIX = "/api"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(settings_router.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(agents.router, prefix=API_PREFIX)
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(ops.router, prefix=API_PREFIX)
app.include_router(ops_files.router, prefix=API_PREFIX)
app.include_router(resume.router, prefix=API_PREFIX)
app.include_router(netdisk.router, prefix=API_PREFIX)


# ---- 错误处理 --------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """把 HTTP 错误也套进统一响应结构里返回。

    SPA 的 axios 拦截器是按响应体里的 ``code`` 判断的，如果直接吐 FastAPI 原生
    的错误格式（``{"detail": ...}``），401 就会绕过拦截逻辑，页面也就不会跳转到
    登录页。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse(code=exc.status_code, message=str(exc.detail)).model_dump(),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    """业务拒绝统一按 HTTP 200 返回，靠响应体里的非零 code 表达失败。

    刻意保持 200：SPA 只在成功响应里读这层结构，而这些 code（比如「尚未配置
    embedding 模型」）驱动的是一套引导用户去配置的流程，而不是弹一个笼统的
    错误提示。
    """
    return JSONResponse(
        status_code=200,
        content=APIResponse(code=exc.code, message=exc.msg).model_dump(),
    )


# ---- 健康检查 ----------------------------------------------------------------

@app.get(f"{API_PREFIX}/health", tags=["health"])
async def health_check() -> JSONResponse:
    return JSONResponse(
        content={
            "status": "ok",
            "app": settings.APP_NAME,
            "version": "1.0.0",
        }
    )


# ---- 静态文件（前端构建产物）-----------------------------------------

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ASSETS_DIR = STATIC_DIR / "assets"

# 确保目录存在；assets 目录缺失不应导致服务崩溃。
STATIC_DIR.mkdir(parents=True, exist_ok=True)

if ASSETS_DIR.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(ASSETS_DIR), check_dir=False),
        name="assets",
    )

# 其他顶层静态文件（favicon、manifest 等）挂载在 /static 下。
if STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )


# ---- SPA 回退路由 ----------------------------------------------------------

INDEX_FILE = STATIC_DIR / "index.html"


@app.get("/", include_in_schema=False)
async def spa_root() -> FileResponse:
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    return FileResponse(str(_placeholder()), media_type="text/html")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> FileResponse:
    # 不覆盖 API 或静态资源路由。
    if full_path.startswith("api/") or full_path.startswith("assets/") or full_path.startswith("static/"):
        # FastAPI 的路由优先级应该能处理好，这里只是防御性检查。
        return FileResponse(str(_placeholder()), media_type="text/html")
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    return FileResponse(str(_placeholder()), media_type="text/html")


def _placeholder() -> Path:
    """前端尚未构建时返回的兜底 HTML。"""
    placeholder = STATIC_DIR / "index.html"
    if not placeholder.exists():
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.write_text(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>yangvis</title></head><body>"
            "<h1>yangvis is starting...</h1>"
            "<p>Frontend bundle not found. Run <code>npm run build</code> "
            "in <code>frontend/</code> and rebuild the image.</p>"
            "<p>API docs: <a href='/api/docs'>/api/docs</a></p>"
            "</body></html>",
            encoding="utf-8",
        )
    return placeholder


@app.on_event("startup")
async def on_startup() -> None:
    validate_settings(settings)
    create_tables()
    migrate_schema()
    bootstrap_admin(settings)
    seed_agents()
    reset_stale_jobs()
    logger.info("yangvis backend started on port %s", settings.PORT)
