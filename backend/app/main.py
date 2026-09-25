"""yangvis 的 FastAPI 应用入口。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
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
    ai_gateway,
    ai_gateway_openai,
    auth,
    chat,
    interview,
    knowledge,
    memory,
    netdisk,
    ops,
    ops_files,
    resume,
    settings as settings_router,
    users,
)

logger = logging.getLogger("yangvis")

settings = get_settings()

_log_level = getattr(logging, settings.LOG_LEVEL.upper(), None)
if not isinstance(_log_level, int):
    # 配置写错（比如 "inf"）不至于让服务起不来，回退 INFO。
    _log_level = logging.INFO
logging.basicConfig(level=_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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
app.include_router(memory.router, prefix=API_PREFIX)
app.include_router(ops.router, prefix=API_PREFIX)
app.include_router(ops_files.router, prefix=API_PREFIX)
app.include_router(resume.router, prefix=API_PREFIX)
app.include_router(interview.router, prefix=API_PREFIX)
app.include_router(netdisk.router, prefix=API_PREFIX)
app.include_router(ai_gateway.router, prefix=API_PREFIX)
# 对外的 OpenAI 兼容端点挂在站点根的 /v1 下（不带 /api 前缀），调用方填的
# base_url 就是 http://<host>:<port>/v1，与各家 SDK 的默认约定一致。
app.include_router(ai_gateway_openai.router)


# ---- 错误处理 --------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """把 HTTP 错误也套进统一响应结构里返回。

    SPA 的 axios 拦截器是按响应体里的 ``code`` 判断的，如果直接吐 FastAPI 原生
    的错误格式（``{"detail": ...}``），401 就会绕过拦截逻辑，页面也就不会跳转到
    登录页。
    """
    # 5xx 是服务端问题的信号；4xx 是客户端问题，留 debug 给排障。
    if exc.status_code >= 500:
        logger.warning(
            "http %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail
        )
    else:
        logger.debug(
            "http %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail
        )
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
    # 业务拒绝是用户可见的正常分支，不值得 warning；但排障时要能看见是哪条。
    logger.debug(
        "business error %s on %s %s: %s", exc.code, request.method, request.url.path, exc.msg
    )
    return JSONResponse(
        status_code=200,
        content=APIResponse(code=exc.code, message=exc.msg).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求体校验失败也套统一信封（FastAPI 默认吐 ``{"detail": [...]}``，会绕过
    SPA 拦截器，和上面 HTTPException 处理器要解决的问题一样）。"""
    logger.debug(
        "request validation failed on %s %s: %s",
        request.method,
        request.url.path,
        exc.errors()[0] if exc.errors() else exc,
    )
    return JSONResponse(
        status_code=422,
        content=APIResponse(code=422, message="请求参数不合法").model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常的最后一道网：带请求上下文记堆栈，回 500 统一信封。

    没有它的话堆栈只落在 uvicorn.error 里，和具体哪个请求对不上。
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=APIResponse(code=500, message="服务器内部错误").model_dump(),
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
    # 顶层静态文件（favicon.svg、robots.txt 等）原样返回，其余路径回退到 SPA。
    # resolve + is_relative_to 防 '..' 路径穿越出 STATIC_DIR。
    candidate = (STATIC_DIR / full_path).resolve()
    if candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
        return FileResponse(str(candidate))
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
    # 每步单独记失败：裸 traceback 看不出死在哪一步。
    for step_name, step in (
        ("validate_settings", lambda: validate_settings(settings)),
        ("create_tables", create_tables),
        ("migrate_schema", migrate_schema),
        ("bootstrap_admin", lambda: bootstrap_admin(settings)),
        ("seed_agents", seed_agents),
        ("reset_stale_jobs", reset_stale_jobs),
    ):
        try:
            step()
        except Exception:
            logger.error("startup step %s failed", step_name, exc_info=True)
            raise
    logger.info("yangvis backend started on port %s", settings.PORT)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("yangvis backend shutting down")
