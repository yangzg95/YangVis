"""SFTP 流式传输（窗口化读写与上传体计数）的测试。

不连真实 SSH：用 Fake 对象模拟 ``SFTPClientFile`` 的 ``read(size, offset)``
/``write(data, offset)`` 语义（含乱序完成、短读、中途失败），验证窗口逻辑
的偏移正确性、背压上界和失败清理。依赖里没有 pytest-asyncio，统一在同步
测试里 ``asyncio.run()``。
"""
import asyncio
import random
from typing import Optional

import pytest

from app.routers.ops_files import _UploadTooLarge, _iter_body_chunks
from app.services.ops_sftp import SftpError, iter_file_chunks, upload_stream


def run(coro):
    return asyncio.run(coro)


# ---- Fakes --------------------------------------------------------------------


class _AsyncCM:
    """把任意对象包成 async with 能用的上下文管理器。"""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class FakeRemote:
    """模拟 SFTPClientFile。read 从源 buffer 切片，write 写进目标 bytearray。

    - ``short_read``：单次 read 最多返回的字节数（模拟服务端合法短读）。
    - ``fail_read_at`` / ``fail_write_at``：到这个 offset 就抛 OSError。
    - ``truncated_at``：声称的 size 之外实际只有这么多数据（之后 read 返回空）。
    - 每次 I/O 前睡随机小时长，打乱并发任务的完成顺序。
    """

    def __init__(
        self,
        data: bytes = b"",
        *,
        short_read: Optional[int] = None,
        fail_read_at: Optional[int] = None,
        fail_write_at: Optional[int] = None,
        truncated_at: Optional[int] = None,
        seed: int = 42,
    ):
        self._data = data
        self._short_read = short_read
        self._fail_read_at = fail_read_at
        self._fail_write_at = fail_write_at
        self._truncated_at = truncated_at
        self._rng = random.Random(seed)
        self.written = bytearray()
        self.writes = []  # (offset, len(data))，记录落笔顺序
        self._pos = 0  # offset=None 顺序读的位置
        self.inflight = 0
        self.max_inflight = 0

    async def _jitter(self):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(self._rng.random() * 0.002)

    async def _settle(self):
        self.inflight -= 1

    async def read(self, size: int = -1, offset: Optional[int] = None) -> bytes:
        await self._jitter()
        try:
            if offset is None:
                offset = self._pos
            if self._fail_read_at is not None and offset >= self._fail_read_at:
                raise OSError("simulated read failure")
            end = len(self._data) if size < 0 else offset + size
            if self._short_read is not None:
                end = min(end, offset + self._short_read)
            if self._truncated_at is not None:
                end = min(end, self._truncated_at)
            chunk = self._data[offset:end]
            if offset == self._pos:
                self._pos += len(chunk)
            return chunk
        finally:
            await self._settle()

    async def write(self, data: bytes, offset: Optional[int] = None) -> int:
        await self._jitter()
        try:
            if offset is None:
                offset = self._pos
                self._pos += len(data)
            if self._fail_write_at is not None and offset >= self._fail_write_at:
                raise OSError("simulated write failure")
            end = offset + len(data)
            if end > len(self.written):
                self.written.extend(b"\x00" * (end - len(self.written)))
            self.written[offset:end] = data
            self.writes.append((offset, len(data)))
            return len(data)
        finally:
            await self._settle()


class FakeSftp:
    def __init__(self, remote: FakeRemote):
        self.remote = remote
        self.removed = []
        self.exited = False

    def open(self, path, mode):
        return _AsyncCM(self.remote)

    async def remove(self, path):
        self.removed.append(path)

    async def exit(self):
        self.exited = True


class FakeConn:
    def __init__(self, sftp: FakeSftp):
        self.sftp = sftp

    async def start_sftp_client(self):
        return self.sftp


def make_conn(remote: FakeRemote) -> tuple[FakeConn, FakeSftp]:
    sftp = FakeSftp(remote)
    return FakeConn(sftp), sftp


async def collect(aiter) -> bytes:
    parts = []
    async for chunk in aiter:
        parts.append(chunk)
    return b"".join(parts)


SOURCE = bytes(range(256)) * 4096  # 1MB，内容可预测


# ---- iter_file_chunks -----------------------------------------------------------


def test_iter_in_order_despite_out_of_order_completion():
    remote = FakeRemote(SOURCE)

    async def main():
        return await collect(
            iter_file_chunks(remote, len(SOURCE), chunk_size=64 * 1024, max_requests=4)
        )

    assert run(main()) == SOURCE
    assert remote.max_inflight <= 4


def test_iter_handles_short_reads():
    remote = FakeRemote(SOURCE, short_read=7 * 1024)

    async def main():
        return await collect(
            iter_file_chunks(remote, len(SOURCE), chunk_size=64 * 1024, max_requests=4)
        )

    assert run(main()) == SOURCE


def test_iter_stops_cleanly_when_file_truncated_mid_read():
    # stat 说 1MB，实际只有 100KB：产出已有前缀就停，不报错。
    remote = FakeRemote(SOURCE, truncated_at=100 * 1024)

    async def main():
        return await collect(
            iter_file_chunks(remote, len(SOURCE), chunk_size=64 * 1024, max_requests=4)
        )

    assert run(main()) == SOURCE[: 100 * 1024]


def test_iter_error_cancels_pending_and_propagates():
    remote = FakeRemote(SOURCE, fail_read_at=128 * 1024)

    async def main():
        baseline = len(asyncio.all_tasks())
        with pytest.raises(OSError, match="simulated read failure"):
            await collect(
                iter_file_chunks(
                    remote, len(SOURCE), chunk_size=64 * 1024, max_requests=4
                )
            )
        # finally 已经取消并回收了全部在途任务，不能泄漏。
        assert len(asyncio.all_tasks()) == baseline

    run(main())


def test_iter_unknown_size_falls_back_to_sequential():
    remote = FakeRemote(SOURCE)

    async def main():
        return await collect(
            iter_file_chunks(remote, None, chunk_size=64 * 1024, max_requests=4)
        )

    assert run(main()) == SOURCE
    # 顺序读不并发。
    assert remote.max_inflight == 1


def test_iter_zero_size_yields_nothing():
    remote = FakeRemote(b"")

    async def main():
        return await collect(
            iter_file_chunks(remote, 0, chunk_size=64 * 1024, max_requests=4)
        )

    assert run(main()) == b""


# ---- upload_stream ----------------------------------------------------------------


async def chunk_iterator(data: bytes, chunk_size: int):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


def test_upload_writes_correct_offsets_despite_reordering():
    remote = FakeRemote()
    conn, sftp = make_conn(remote)

    written = run(
        upload_stream(conn, "/tmp/f", chunk_iterator(SOURCE, 64 * 1024), max_requests=4)
    )

    assert written == len(SOURCE)
    assert bytes(remote.written) == SOURCE
    assert remote.max_inflight <= 4
    assert sftp.removed == []  # 成功的文件绝不能被误删
    assert sftp.exited


def test_upload_failure_maps_error_and_removes_partial():
    remote = FakeRemote(fail_write_at=128 * 1024)
    conn, sftp = make_conn(remote)

    with pytest.raises(SftpError):
        run(
            upload_stream(
                conn, "/tmp/f", chunk_iterator(SOURCE, 64 * 1024), max_requests=4
            )
        )

    assert sftp.removed == ["/tmp/f"]


def test_upload_propagates_iterator_error_unmapped_and_removes_partial():
    remote = FakeRemote()
    conn, sftp = make_conn(remote)

    class Boom(Exception):
        pass

    async def bad_chunks():
        yield b"x" * 1024
        raise Boom()

    async def main():
        baseline = len(asyncio.all_tasks())
        with pytest.raises(Boom):
            await upload_stream(conn, "/tmp/f", bad_chunks(), max_requests=4)
        assert len(asyncio.all_tasks()) == baseline

    run(main())
    assert sftp.removed == ["/tmp/f"]


def test_upload_empty_file_creates_no_writes_and_returns_zero():
    remote = FakeRemote()
    conn, sftp = make_conn(remote)

    async def empty():
        return
        yield  # pragma: no cover - 只是让它成为异步生成器

    assert run(upload_stream(conn, "/tmp/f", empty(), max_requests=4)) == 0
    assert remote.writes == []
    assert sftp.removed == []


# ---- _iter_body_chunks -----------------------------------------------------------


class FakeUpload:
    """只有 ``async read(n)`` 的上传体。"""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        end = len(self._data) if n < 0 else self._pos + n
        chunk = self._data[self._pos : end]
        self._pos += len(chunk)
        return chunk


def test_body_chunks_exactly_at_limit_passes():
    data = b"y" * (300 * 1024)

    assert run(collect(_iter_body_chunks(FakeUpload(data), len(data), 64 * 1024))) == data


def test_body_chunks_over_limit_raises():
    data = b"y" * (300 * 1024)

    with pytest.raises(_UploadTooLarge):
        run(collect(_iter_body_chunks(FakeUpload(data), len(data) - 1, 64 * 1024)))
