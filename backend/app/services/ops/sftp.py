"""SFTP 远程文件浏览器的文件操作层。

每个操作都收一条已建好的 SSH 连接（``ops_server.connect`` 的产物），自己
``start_sftp_client()``，用完关掉。不做路径沙箱：面板能到的地方和用户自己
开终端能到的地方完全一样，沙箱只会给人虚假的安全感。
"""
from __future__ import annotations

import asyncio
import logging
import posixpath
import stat as stat_module
from typing import AsyncIterator, Optional

import asyncssh

from app.config import get_settings
from app.models.schemas import SftpEntry, SftpListResult

logger = logging.getLogger("yangvis.ops_sftp")

# 同名粘贴最多试多少个副本名，超过就让用户自己收拾。
_PASTE_MAX_RENAME = 100


class SftpError(RuntimeError):
    """已翻译成用户可读消息的 SFTP 失败。"""


def _stalled(timeout: float) -> SftpError:
    """停顿看门狗到点。用 SftpError 是为了让路由层现成的分支接住它。"""
    return SftpError(f"传输停顿超过 {timeout:.0f} 秒，已中断")


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


async def open_session(
    conn: asyncssh.SSHClientConnection, timeout: Optional[float] = None
) -> asyncssh.SFTPClient:
    """``conn.start_sftp_client()`` 加超时。

    SSH 建连那步由 ``ops_server.connect`` 管，这一步以前没人管：对端的会话数打满
    或 sshd 卡住时，新通道请求会被静默丢弃，调用方永远等不到回复——面板上就是
    一次点击、转圈转到用户怀疑人生。超时只是止损，会话本身交给调用方
    ``async with`` 收尾。
    """
    if timeout is None:
        timeout = get_settings().OPS_SFTP_OP_TIMEOUT
    message = f"服务器无响应：打开 SFTP 会话超过 {timeout:.0f} 秒，请稍后重试"
    return await stall_guard(conn.start_sftp_client(), timeout, message)


async def home_of(sftp: asyncssh.SFTPClient) -> str:
    """登录用户的 home。SFTP 会话的起始工作目录就是 home。"""
    cwd = await sftp.getcwd()
    if isinstance(cwd, bytes):
        cwd = cwd.decode("utf-8", errors="replace")
    return cwd or "/"


async def resolve_path(conn: asyncssh.SSHClientConnection, path: Optional[str]) -> str:
    """短开一条 SFTP 会话，把 path 规整成绝对路径（空 → home）。"""
    try:
        async with await open_session(conn) as sftp:
            return normalize_path(path, await home_of(sftp))
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc


async def list_dir(
    conn: asyncssh.SSHClientConnection, path: Optional[str]
) -> SftpListResult:
    settings = get_settings()
    try:
        async with await open_session(conn) as sftp:
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
        # 顺手把上传上限带给面板：它是这个页面的第一个请求，前端拿到上限就能在
        # 用户选完文件的那一刻拒绝，而不是让他传完一整颗心再收一张 413。
        max_upload_bytes=settings.OPS_SFTP_MAX_UPLOAD_BYTES,
    )


def _is_dir(attrs: asyncssh.SFTPAttrs) -> bool:
    return stat_module.S_ISDIR(attrs.permissions)


async def make_dir(conn: asyncssh.SSHClientConnection, path: str) -> None:
    try:
        async with await open_session(conn) as sftp:
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


async def stall_guard(awaitable, timeout: float, stall_message: Optional[str] = None):
    """给一次 SFTP 等待套上停顿看门狗，到点抛出可读的 ``SftpError``。

    只对「没有新字节进出」敏感：慢但在动的传输不会被误杀，真正 wedge 住的
    （对端进程被 OOM 掉、SSH 通道半死）才会——那种情况不掐就是前端一个永远
    停在「传输中」的僵尸任务。

    不直接用 ``asyncio.wait_for``：3.12 起它只在外层抛超时，被等的那件事如果
    自己吞掉 CancelledError 并「正常完成」，``wait_for`` 会把那个结果返回给你
    而不是报超时（本机 3.13.2 实测）。看护不该赌第三方库怎么响应取消，所以这里
    显式 wait → cancel → 回收：超时就一定抛错，在途任务也一定收干净。
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if stall_message:
            raise SftpError(stall_message)
        raise _stalled(timeout)
    return task.result()


async def iter_file_chunks(
    remote: asyncssh.SFTPClientFile,
    size: Optional[int],
    *,
    chunk_size: int,
    max_requests: int = 8,
    stall_timeout: Optional[float] = None,
) -> AsyncIterator[bytes]:
    """按序产出远程文件内容。

    ``size`` 已知（stat 拿到了）时开窗口预读：按递增 offset 保持最多
    ``max_requests`` 个在途读任务，按顺序 yield——高延迟链路上省掉每个块
    边界的 RTT 空等。读取总量钳制在 size 内（响应带 Content-Length，多读
    会破坏 HTTP 响应）；中途读到空块说明文件被截断，停。``size`` 未知时
    没法排偏移，退化为顺序读。

    两条路径都受 ``stall_timeout`` 看护：到点抛出 ``SftpError``，响应随之提前
    结束，浏览器按「下载被截断」报错——比永远吊着一个不吐字节的响应好。
    """
    if stall_timeout is None:
        stall_timeout = get_settings().OPS_SFTP_STALL_TIMEOUT

    if size is None:
        while True:
            chunk = await stall_guard(remote.read(chunk_size), stall_timeout)
            if not chunk:
                break
            yield chunk
        return

    pending: list[asyncio.Task] = []
    produced = 0
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
            block = await stall_guard(pending.pop(0), stall_timeout)
            if not block:
                # 文件在下载途中被截断：提前结束（HTTP 侧响应随之变短，
                # 浏览器报下载失败，和改动前的行为一致）。
                logger.warning(
                    "remote file shrank during a pipelined read: stat said %d bytes, stopping at %d",
                    size,
                    produced,
                )
                break
            produced += len(block)
            yield block
    finally:
        await _cancel_all(pending)


async def upload_stream(
    conn: asyncssh.SSHClientConnection,
    remote_path: str,
    chunks: AsyncIterator[bytes],
    *,
    max_requests: int = 8,
    stall_timeout: Optional[float] = None,
) -> int:
    """流式上传：按到达顺序为每块分配递增 offset，窗口内并发 ``remote.write``。

    失败时尽力删除远端留下的半截文件。返回写入的字节数（审计用）。
    ``(asyncssh.Error, OSError)`` 翻译成 ``SftpError``；``CancelledError``
    和 chunks 迭代器自己抛的异常（比如路由层的大小超限）原样透传。

    窗口回收与收尾各受 ``stall_timeout`` 看护：对端不再收字节就判死，走同一条
    失败清理路径，半截文件不会留在服务器上。
    """
    if stall_timeout is None:
        stall_timeout = get_settings().OPS_SFTP_STALL_TIMEOUT
    offset = 0
    completed = False
    opened = False
    pending: set[asyncio.Task] = set()
    sftp: Optional[asyncssh.SFTPClient] = None
    try:
        sftp = await open_session(conn)
        # open 保持默认参数：写路径由 asyncssh 按服务端 write_len 自动切分，
        # 传大块 block_size 会让不广播 limits 的 OpenSSH 服务端直接断连。
        async with sftp.open(remote_path, "wb") as remote:
            opened = True
            async for chunk in chunks:
                pending.add(asyncio.ensure_future(remote.write(chunk, offset)))
                offset += len(chunk)
                if len(pending) >= max_requests:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED, timeout=stall_timeout
                    )
                    if not done:
                        raise _stalled(stall_timeout)
                    _raise_first(done)
            if pending:
                done, pending = await asyncio.wait(pending, timeout=stall_timeout)
                if not done:
                    raise _stalled(stall_timeout)
                _raise_first(done)
        completed = True
        return offset
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc
    finally:
        # 失败/取消都从这里走：先回收在途写任务，再清理远端半截文件。
        # remove 失败（比如连接已断）不影响原异常的传播。
        await _cancel_all(list(pending))
        if sftp is not None and opened and not completed:
            try:
                await sftp.remove(remote_path)
                logger.info("removed partial upload %s after a failed transfer", remote_path)
            except Exception as exc:
                logger.warning("failed to remove partial upload %s: %s", remote_path, exc)
        if sftp is not None:
            try:
                await sftp.exit()
            except Exception as exc:
                logger.debug("sftp session exit failed: %s", exc)


def paste_candidate(name: str, attempt: int) -> str:
    """``attempt`` 从 1 起：1 → ``x_副本.ext``，2 → ``x_副本2.ext``。

    ``splitext`` 只切最后一段后缀，所以 ``x.tar.gz`` 的副本名落在 ``.gz`` 前——
    后缀留在末尾才符合直觉；隐藏文件 ``.gitignore`` 整段当主体处理。
    """
    stem, ext = posixpath.splitext(name)
    suffix = "_副本" if attempt <= 1 else f"_副本{attempt}"
    return f"{stem}{suffix}{ext}"


async def unique_target(sftp: asyncssh.SFTPClient, dst_dir: str, name: str) -> str:
    """目标目录里避开同名：原名可用就用原名，否则依次试 ``_副本``、``_副本2``…"""
    target = posixpath.join(dst_dir, name)
    if not await sftp.exists(target):
        return target
    for attempt in range(1, _PASTE_MAX_RENAME + 1):
        candidate = posixpath.join(dst_dir, paste_candidate(name, attempt))
        if not await sftp.exists(candidate):
            return candidate
    raise SftpError(f"目标目录里同名副本过多，先清理 {name} 的副本再粘贴")


async def _move(sftp: asyncssh.SFTPClient, src: str, dst: str) -> None:
    """剪切：先试 rename，被拒（跨文件系统）再退化成复制＋删除。"""
    try:
        await sftp.rename(src, dst)
        return
    except (asyncssh.SFTPPermissionDenied, asyncssh.SFTPFailure):
        logger.debug("rename %s -> %s rejected, falling back to copy+remove", src, dst)
    await sftp.copy(src, dst, recurse=True, preserve=True)
    try:
        if await sftp.isdir(src):
            await sftp.rmtree(src)
        else:
            await sftp.remove(src)
    except (asyncssh.Error, OSError) as exc:
        # 副本已经落盘，删不掉源只是留了个重复项，不该让这次操作看起来失败。
        logger.warning("copied %s to %s but failed to remove the source: %s", src, dst, exc)


async def paste(
    conn: asyncssh.SSHClientConnection, src: str, dst_dir: str, *, move: bool
) -> str:
    """服务端把 ``src`` 复制/移动到目录 ``dst_dir``，返回实际落地的绝对路径。

    同名不覆盖：目标已被占用时改名成 ``xxx_副本.ext``。``dst_dir`` 必须是目录，
    不存在就交给 SFTP 报错（权限不足/路径不存在都翻成可读消息）。
    """
    try:
        async with await open_session(conn) as sftp:
            home = await home_of(sftp)
            src_path = normalize_path(src, home)
            base_dir = normalize_path(dst_dir, home)
            name = posixpath.basename(src_path)
            if not name:
                raise SftpError("根目录不能粘贴")
            if base_dir == src_path or base_dir.startswith(src_path + "/"):
                raise SftpError("不能把目录粘贴到它自己里面")
            # 剪切回原目录本来是无操作，但同名检测会把它当成冲突改出个 _副本 来。
            if move and posixpath.dirname(src_path) == base_dir:
                raise SftpError("它已经在这个目录里了")
            target = await unique_target(sftp, base_dir, name)
            if move:
                await _move(sftp, src_path, target)
            else:
                await sftp.copy(src_path, target, recurse=True, preserve=True)
            return target
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc


async def remove(conn: asyncssh.SSHClientConnection, path: str) -> None:
    """删除文件或目录。目录走 ``rmtree`` 递归——前端弹确认时已明示这一点。"""
    try:
        async with await open_session(conn) as sftp:
            target = normalize_path(path, await home_of(sftp))
            if await sftp.isdir(target):
                await sftp.rmtree(target)
            else:
                await sftp.remove(target)
    except (asyncssh.Error, OSError) as exc:
        raise map_sftp_error(exc) from exc
