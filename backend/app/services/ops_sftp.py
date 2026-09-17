"""SFTP 远程文件浏览器的文件操作层。

每个操作都收一条已建好的 SSH 连接（``ops_server.connect`` 的产物），自己
``start_sftp_client()``，用完关掉。不做路径沙箱：面板能到的地方和用户自己
开终端能到的地方完全一样，沙箱只会给人虚假的安全感。
"""
from __future__ import annotations

import posixpath
import stat as stat_module
from typing import Optional

import asyncssh

from app.config import get_settings
from app.models.schemas import SftpEntry, SftpListResult


class SftpError(RuntimeError):
    """已翻译成用户可读消息的 SFTP 失败。"""


def map_sftp_error(exc: BaseException) -> SftpError:
    if isinstance(exc, SftpError):
        return exc
    if isinstance(exc, asyncssh.SFTPNoSuchFile):
        return SftpError("路径不存在")
    if isinstance(exc, asyncssh.SFTPPermissionDenied):
        return SftpError("权限不足")
    if isinstance(exc, asyncssh.SFTPNotADirectory):
        return SftpError("目标不是目录")
    if isinstance(exc, asyncssh.SFTPFailure):
        return SftpError(f"操作失败：{exc}")
    return SftpError(f"SFTP 错误：{exc}")


def normalize_path(path: Optional[str], home: str) -> str:
    """把用户给的路径规整成绝对路径：空串回 home，``~`` 展开，其余原样。

    不 normpath：面板面包屑一段段点出来的路径本身就是规范的，而用户在
    地址栏里手输 ``/var/../etc`` 时保留原样反而和终端行为一致（SFTP 服务端
    自己会解析）。
    """
    path = (path or "").strip()
    if not path or path == "~":
        return home
    if path.startswith("~/"):
        return posixpath.join(home, path[2:])
    return path


async def home_of(sftp: asyncssh.SFTPClient) -> str:
    """登录用户的 home。SFTP 会话的起始工作目录就是 home。"""
    cwd = await sftp.getcwd()
    if isinstance(cwd, bytes):
        cwd = cwd.decode("utf-8", errors="replace")
    return cwd or "/"


async def resolve_path(conn: asyncssh.SSHClientConnection, path: Optional[str]) -> str:
    """短开一条 SFTP 会话，把 path 规整成绝对路径（空 → home）。"""
    try:
        async with conn.start_sftp_client() as sftp:
            return normalize_path(path, await home_of(sftp))
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc


async def list_dir(
    conn: asyncssh.SSHClientConnection, path: Optional[str]
) -> SftpListResult:
    settings = get_settings()
    try:
        async with conn.start_sftp_client() as sftp:
            target = normalize_path(path, await home_of(sftp))
            names = await sftp.readdir(target)
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc

    items = []
    for entry in names:
        if entry.filename in (".", ".."):
            continue
        attrs = entry.attrs
        items.append(
            SftpEntry(
                name=entry.filename,
                is_dir=bool(attrs.permissions is not None and _is_dir(attrs)),
                size=attrs.size or 0,
                mtime=int(attrs.mtime or 0),
                mode=attrs.permissions or 0,
            )
        )
    items.sort(key=lambda item: (not item.is_dir, item.name.lower()))

    truncated = len(items) > settings.OPS_SFTP_LIST_LIMIT
    return SftpListResult(
        path=target,
        items=items[: settings.OPS_SFTP_LIST_LIMIT] if truncated else items,
        truncated=truncated,
    )


def _is_dir(attrs: asyncssh.SFTPAttrs) -> bool:
    return stat_module.S_ISDIR(attrs.permissions)


async def make_dir(conn: asyncssh.SSHClientConnection, path: str) -> None:
    try:
        async with conn.start_sftp_client() as sftp:
            target = normalize_path(path, await home_of(sftp))
            await sftp.mkdir(target)
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc


async def upload(
    conn: asyncssh.SSHClientConnection, remote_path: str, data: bytes
) -> None:
    """整块写入。大小上限在路由层校验过了（``OPS_SFTP_MAX_UPLOAD_BYTES``）。"""
    try:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "wb") as remote:
                await remote.write(data)
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc


async def remove(conn: asyncssh.SSHClientConnection, path: str) -> None:
    """删除文件或目录。目录走 ``rmtree`` 递归——前端弹确认时已明示这一点。"""
    try:
        async with conn.start_sftp_client() as sftp:
            target = normalize_path(path, await home_of(sftp))
            if await sftp.isdir(target):
                await sftp.rmtree(target)
            else:
                await sftp.remove(target)
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc
