"""SFTP 远程文件浏览器的 REST 端点。

和终端那条 WebSocket 完全独立：每个请求单独 ``connect()`` 一条 SSH 连接，
做完就关，终端断开时文件面板照常可用。下载是全项目唯一不套统一信封的
端点（二进制流），它的错误以 HTTP 200 + 信封形状 JSON 返回，前端按
``application/json`` 识别。
"""
from __future__ import annotations

import asyncio
import logging
import posixpath
import time
from typing import AsyncIterator, Optional
from urllib.parse import quote

import asyncssh
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.errors import CODE_OPS_CONNECT_FAILED, BusinessError
from app.models.entities import OpsAuditLog, OpsServer
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    SftpListResult,
    SftpMkdirRequest,
)
from app.services import ops_sftp, ops_server as server_ops

logger = logging.getLogger("yangvis.ops_files")


def _mb_per_sec(nbytes: int, elapsed: float) -> float:
    """传输速率。elapsed 可能小到测不出来，给个下限防止除零。"""
    return nbytes / 1024 / 1024 / max(elapsed, 1e-6)

router = APIRouter(prefix="/ops/servers", tags=["ops-files"])

settings = get_settings()


def _get_server(
    server_id: int,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> tuple[OpsServer, Session]:
    """取服务器。台账全员共用后不再校验归属，只区分存在与否（404）。"""
    service = server_ops.OpsServerService(db, user.user_id)
    try:
        server = service.get(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return server, db


async def _connect(server: OpsServer, db: Session) -> asyncssh.SSHClientConnection:
    try:
        # 传 service：用户自己发起的连接，TOFU 首次固定指纹要落库。
        return await server_ops.connect(server, server_ops.OpsServerService(db, server.owner_id))
    except server_ops.OpsConnectError as exc:
        raise BusinessError(CODE_OPS_CONNECT_FAILED, str(exc)) from exc


def _audit(
    db: Session,
    actor_id: int,
    server: OpsServer,
    command: str,
    success: bool,
    error: Optional[str] = None,
) -> None:
    # owner_id 记操作者本人而不是资产登记人：审计可见性是「用户看自己的、
    # 管理员看全部」，共享资产上不能张冠李戴。
    db.add(
        OpsAuditLog(
            owner_id=actor_id,
            target_type="server",
            target_id=server.id,
            target_name=server.name,
            actor="user",
            command=command[:8000],
            verdict="sftp",
            success=success,
            error=error[:512] if error else None,
        )
    )
    db.commit()


def _json_error(message: str) -> JSONResponse:
    """下载端点的错误形态：HTTP 200 + 信封 JSON，前端按 content-type 识别。"""
    return JSONResponse(content=APIResponse(code=-1, message=message).model_dump())


def _require_ops_write(user: CurrentUser) -> None:
    """文件写操作的 ops_write 闸门。列目录与下载保持只读开放，不经过这里。"""
    if not user.can_ops_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="没有运维写权限，请联系管理员"
        )


# ---- 列目录 -------------------------------------------------------------------


@router.get("/{server_id}/files/list", response_model=APIResponse[SftpListResult])
async def list_files(
    path: Optional[str] = Query(default=None, max_length=1024),
    ctx: tuple[OpsServer, Session] = Depends(_get_server),
) -> APIResponse[SftpListResult]:
    server, db = ctx
    conn = await _connect(server, db)
    try:
        result = await ops_sftp.list_dir(conn, path)
    except ops_sftp.SftpError as exc:
        return APIResponse(code=-1, message=str(exc))
    finally:
        conn.close()
    return APIResponse(data=result)


# ---- 下载 ---------------------------------------------------------------------


def _content_disposition(filename: str) -> str:
    """RFC 5987：中文名走 filename*，同时给一个纯 ASCII 的兜底的 filename。"""
    fallback = filename.encode("ascii", errors="replace").decode().replace('"', "_")
    return (
        f"attachment; filename=\"{fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )


@router.get("/{server_id}/files/download")
async def download_file(
    path: str = Query(..., min_length=1, max_length=1024),
    user: CurrentUser = Depends(require_user),
    ctx: tuple[OpsServer, Session] = Depends(_get_server),
):
    server, db = ctx
    try:
        conn = await _connect(server, db)
    except BusinessError as exc:
        return _json_error(exc.msg)

    # 连接和 sftp 客户端交给流生成器持有，生成器的 finally 里关——
    # handler 在第一个字节产出前就返回了，不能在这里 close。
    try:
        sftp = await conn.start_sftp_client()
        home = await ops_sftp.home_of(sftp)
        target = ops_sftp.normalize_path(path, home)
        attrs = await sftp.stat(target)
        if await sftp.isdir(target):
            raise ops_sftp.SftpError("目录不支持下载，请到终端里打包后再取")
    except (asyncssh.Error, OSError) as exc:
        conn.close()
        return _json_error(str(ops_sftp.map_sftp_error(exc)))
    except ops_sftp.SftpError as exc:
        conn.close()
        return _json_error(str(exc))

    async def stream() -> AsyncIterator[bytes]:
        started = time.monotonic()
        try:
            async with sftp.open(target, "rb") as remote:
                async for chunk in ops_sftp.iter_file_chunks(
                    remote,
                    attrs.size,
                    chunk_size=settings.OPS_SFTP_CHUNK_SIZE,
                    max_requests=settings.OPS_SFTP_PIPELINE_REQUESTS,
                ):
                    yield chunk
            elapsed = time.monotonic() - started
            logger.info(
                "sftp download %s on server %s finished: %d bytes in %.1fs (%.1f MB/s)",
                target,
                server.id,
                attrs.size or 0,
                elapsed,
                _mb_per_sec(attrs.size or 0, elapsed),
            )
            _audit(db, user.user_id, server, f"sftp download {target}", True)
        except (asyncssh.Error, OSError) as exc:
            _audit(db, user.user_id, server, f"sftp download {target}", False, str(exc))
            logger.warning("sftp download %s on server %s failed: %s", target, server.id, exc)
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端断开（取消下载/关页面）：正常用户行为，不记审计、不刷告警。
            logger.debug("sftp download %s on server %s cancelled by the client", target, server.id)
            raise
        finally:
            await sftp.exit()
            conn.close()

    headers = {"Content-Disposition": _content_disposition(posixpath.basename(target))}
    # size 缺失时宁可不带 Content-Length：写 0 会让浏览器以为文件是空的。
    if attrs.size is not None:
        headers["Content-Length"] = str(attrs.size)
    return StreamingResponse(
        stream(),
        media_type="application/octet-stream",
        headers=headers,
    )


# ---- 上传 ---------------------------------------------------------------------


class _UploadTooLarge(Exception):
    """流式计数超限。upload_stream 已负责清理远端半截文件，这里只管报错。"""


async def _iter_body_chunks(
    file: UploadFile, limit: int, chunk_size: int
) -> AsyncIterator[bytes]:
    """按块读上传体并累计字节数，超限抛 ``_UploadTooLarge``。

    Starlette 对超 1MB 的 multipart 自动落临时盘，这里读的是 spool，内存有界。
    提成模块级函数是为了能直接单测（任何有 ``async read(n)`` 的对象都能喂）。
    """
    sent = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        sent += len(chunk)
        if sent > limit:
            raise _UploadTooLarge
        yield chunk


@router.post("/{server_id}/files/upload", response_model=APIResponse[None])
async def upload_file(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1024),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    ctx: tuple[OpsServer, Session] = Depends(_get_server),
) -> APIResponse[None]:
    _require_ops_write(user)
    server, db = ctx

    limit = settings.OPS_SFTP_MAX_UPLOAD_BYTES
    oversize_message = f"文件过大，上限 {limit // 1024 // 1024} MB"

    # 提前拒绝：Content-Length 是整个 multipart 的大小，留 1MB 余量给表单开销。
    # 不能用 UploadFile.size——处理器入口时它只反映已 spool 的部分。
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > limit + 1024 * 1024:
        logger.debug(
            "sftp upload %s on server %s rejected early: content-length %s over the %d-byte limit",
            file.filename,
            server.id,
            content_length,
            limit,
        )
        return APIResponse(code=-1, message=oversize_message)

    # 文件名只留 basename：multipart 里偶尔会带上客户端的本地路径分隔符。
    name = (file.filename or "unnamed").replace("\\", "/").rsplit("/", 1)[-1]
    if name in ("", ".", ".."):
        return APIResponse(code=-1, message="文件名不合法")

    conn = await _connect(server, db)
    target = ""
    started = time.monotonic()
    try:
        directory = await ops_sftp.resolve_path(conn, path)
        target = posixpath.join(directory, name)
        written = await ops_sftp.upload_stream(
            conn,
            target,
            _iter_body_chunks(file, limit, settings.OPS_SFTP_CHUNK_SIZE),
            max_requests=settings.OPS_SFTP_PIPELINE_REQUESTS,
        )
    except _UploadTooLarge:
        # 半截文件已由 upload_stream 清掉；和改动前一样，超限不记审计。
        logger.info(
            "sftp upload %s on server %s aborted: over the %d-byte limit",
            f"{path}/{name}",
            server.id,
            limit,
        )
        return APIResponse(code=-1, message=oversize_message)
    except ops_sftp.SftpError as exc:
        _audit(db, user.user_id, server, f"sftp upload {path}/{name}", False, str(exc))
        logger.warning("sftp upload %s on server %s failed: %s", f"{path}/{name}", server.id, exc)
        return APIResponse(code=-1, message=str(exc))
    except asyncio.CancelledError:
        # 客户端断开：半截文件由 upload_stream 清理，不记审计。
        logger.debug("sftp upload %s on server %s cancelled by the client", f"{path}/{name}", server.id)
        raise
    finally:
        conn.close()

    elapsed = time.monotonic() - started
    logger.info(
        "sftp upload %s on server %s finished: %d bytes in %.1fs (%.1f MB/s)",
        target,
        server.id,
        written,
        elapsed,
        _mb_per_sec(written, elapsed),
    )
    _audit(db, user.user_id, server, f"sftp upload {target} ({written} bytes)", True)
    return APIResponse(data=None, message="uploaded")


# ---- 新建目录 -----------------------------------------------------------------


@router.post("/{server_id}/files/mkdir", response_model=APIResponse[None])
async def make_directory(
    payload: SftpMkdirRequest,
    user: CurrentUser = Depends(require_user),
    ctx: tuple[OpsServer, Session] = Depends(_get_server),
) -> APIResponse[None]:
    _require_ops_write(user)
    server, db = ctx
    conn = await _connect(server, db)
    try:
        await ops_sftp.make_dir(conn, payload.path)
    except ops_sftp.SftpError as exc:
        _audit(db, user.user_id, server, f"sftp mkdir {payload.path}", False, str(exc))
        return APIResponse(code=-1, message=str(exc))
    finally:
        conn.close()

    _audit(db, user.user_id, server, f"sftp mkdir {payload.path}", True)
    return APIResponse(data=None, message="created")


# ---- 删除 ---------------------------------------------------------------------


@router.delete("/{server_id}/files", response_model=APIResponse[None])
async def remove_file(
    path: str = Query(..., min_length=1, max_length=1024),
    user: CurrentUser = Depends(require_user),
    ctx: tuple[OpsServer, Session] = Depends(_get_server),
) -> APIResponse[None]:
    _require_ops_write(user)
    server, db = ctx
    conn = await _connect(server, db)
    try:
        await ops_sftp.remove(conn, path)
    except ops_sftp.SftpError as exc:
        _audit(db, user.user_id, server, f"sftp delete {path}", False, str(exc))
        return APIResponse(code=-1, message=str(exc))
    finally:
        conn.close()

    _audit(db, user.user_id, server, f"sftp delete {path}", True)
    return APIResponse(data=None, message="deleted")
