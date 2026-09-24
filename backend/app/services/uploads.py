"""上传体的有界读取：先按 Content-Length 提前拒绝，再分块计数读，超限即停。

``await file.read()`` 会把整个上传体物化成 bytes 进内存——Starlette 的
spool 只保证接收阶段有界，read() 本身不设上限。需要全量字节的端点
（简历/知识库这类本地文本抽取）用这个模块把大小上限前移。
"""
from __future__ import annotations

from typing import Optional

from fastapi import UploadFile

# multipart 表单开销（边界、其他字段）的余量，与 ops_files 的提前拒绝口径一致。
_FORM_OVERHEAD_BYTES = 1024 * 1024
_CHUNK_SIZE = 256 * 1024


def oversize_message(limit: int) -> str:
    return f"文件过大，上限 {limit / 1024 / 1024:.0f} MB"


def content_length_exceeds(content_length: Optional[str], limit: int) -> bool:
    """Content-Length 是整个 multipart 的大小，留余量给表单开销。

    不能用 UploadFile.size——处理器入口时它只反映已 spool 的部分。
    """
    if not content_length:
        return False
    try:
        return int(content_length) > limit + _FORM_OVERHEAD_BYTES
    except ValueError:
        return False


async def read_upload_limited(file: UploadFile, limit: int) -> Optional[bytes]:
    """分块读上传体，超限返回 None（停止读取，内存占用有界）。"""
    parts: list[bytes] = []
    sent = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        sent += len(chunk)
        if sent > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)
