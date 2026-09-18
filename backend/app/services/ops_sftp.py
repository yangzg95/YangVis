"""SFTP 远程文件浏览器的文件操作层。

每个操作都收一条已建好的 SSH 连接（``ops_server.connect`` 的产物），自己
``start_sftp_client()``，用完关掉。不做路径沙箱：面板能到的地方和用户自己
开终端能到的地方完全一样，沙箱只会给人虚假的安全感。
"""
from __future__ import annotations

import asyncio
import posixpath
import stat as stat_module
from typing import AsyncIterator, Optional

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


async def _read_block(remote: asyncssh.SFTPClientFile, offset: int, size: int) -> bytes:
    """读满一块。SFTP 服务端合法地短读，要循环补足；读到空（EOF）提前结束。"""
    buf = bytearray()
    while len(buf) < size:
        data = await remote.read(size - len(buf), offset + len(buf))
        if not data:
            break
        buf += data
    return bytes(buf)


async def _cancel_all(tasks: list[asyncio.Task]) -> None:
    """取消并回收一组任务。不回收的话事件循环会报「exception never retrieved」。"""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _raise_first(tasks: set[asyncio.Task]) -> None:
    """抛出一批已完成任务里的首个异常。每个任务都必须 ``exception()`` 一遍：
    只 ``result()`` 到第一个就抛出的话，其余失败任务的异常没人回收。"""
    error: Optional[BaseException] = None
    for task in tasks:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None and error is None:
            error = exc
    if error is not None:
        raise error


async def iter_file_chunks(
    remote: asyncssh.SFTPClientFile,
    size: Optional[int],
    *,
    chunk_size: int,
    max_requests: int = 8,
) -> AsyncIterator[bytes]:
    """按序产出远程文件内容。

    ``size`` 已知（stat 拿到了）时开窗口预读：按递增 offset 保持最多
    ``max_requests`` 个在途读任务，按顺序 yield——高延迟链路上省掉每个块
    边界的 RTT 空等。读取总量钳制在 size 内（响应带 Content-Length，多读
    会破坏 HTTP 响应）；中途读到空块说明文件被截断，停。``size`` 未知时
    没法排偏移，退化为顺序读。
    """
    if size is None:
        while True:
            chunk = await remote.read(chunk_size)
            if not chunk:
                break
            yield chunk
        return

    pending: list[asyncio.Task] = []
    try:
        next_offset = 0
        while next_offset < size or pending:
            while next_offset < size and len(pending) < max_requests:
                block_size = min(chunk_size, size - next_offset)
                pending.append(
                    asyncio.ensure_future(_read_block(remote, next_offset, block_size))
                )
                next_offset += block_size
            if not pending:
                break
            block = await pending.pop(0)
            if not block:
                # 文件在下载途中被截断：提前结束（HTTP 侧响应随之变短，
                # 浏览器报下载失败，和改动前的行为一致）。
                break
            yield block
    finally:
        await _cancel_all(pending)


async def upload_stream(
    conn: asyncssh.SSHClientConnection,
    remote_path: str,
    chunks: AsyncIterator[bytes],
    *,
    max_requests: int = 8,
) -> int:
    """流式上传：按到达顺序为每块分配递增 offset，窗口内并发 ``remote.write``。

    失败时尽力删除远端留下的半截文件。返回写入的字节数（审计用）。
    ``(asyncssh.Error, OSError)`` 翻译成 ``SftpError``；``CancelledError``
    和 chunks 迭代器自己抛的异常（比如路由层的大小超限）原样透传。
    """
    offset = 0
    completed = False
    pending: set[asyncio.Task] = set()
    sftp: Optional[asyncssh.SFTPClient] = None
    try:
        sftp = await conn.start_sftp_client()
        # open 保持默认参数：写路径由 asyncssh 按服务端 write_len 自动切分，
        # 传大块 block_size 会让不广播 limits 的 OpenSSH 服务端直接断连。
        async with sftp.open(remote_path, "wb") as remote:
            async for chunk in chunks:
                pending.add(asyncio.ensure_future(remote.write(chunk, offset)))
                offset += len(chunk)
                if len(pending) >= max_requests:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    _raise_first(done)
            if pending:
                done, _ = await asyncio.wait(pending)
                _raise_first(done)
        completed = True
        return offset
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc
    finally:
        # 失败/取消都从这里走：先回收在途写任务，再清理远端半截文件。
        # remove 失败（比如连接已断）不影响原异常的传播。
        await _cancel_all(list(pending))
        if sftp is not None and not completed:
            try:
                await sftp.remove(remote_path)
            except Exception:
                pass
        if sftp is not None:
            try:
                await sftp.exit()
            except Exception:
                pass


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
