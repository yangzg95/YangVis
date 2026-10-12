"""智能办公 · 幻灯片的本地图片存储。

幻灯片配图落在服务器本地磁盘（``settings.upload_media_path``），并由
``app.main`` 挂成 ``settings.UPLOAD_MEDIA_URL`` 下的静态路径，浏览器可以直接
访问 —— 放映文档里的 ``<img>`` 和前端素材库的预览都吃这个 URL，不需要再走
一层带鉴权的下载代理（``<img>`` 发不出 Authorization 头）。

三道防线：

1. **扩展名按内容魔数判定**，不信客户端给的 ``Content-Type`` 和文件名 ——
   上传一个改名成 ``.png`` 的 ``.html`` / ``.svg``，返回的 URL 也会是
   ``.png``，浏览器按图片解码，不会当成脚本执行（SVG 可以携带脚本，这里
   直接不支持）。
2. **文件名是随机 id 加服务端给的扩展名**，用户提供的名字只存进数据库做展示，
   永远不参与路径拼接，所以拼不出 ``..``，也撞不出「覆盖别人的文件」。
3. **删除只按数据库里记录的 ``rel_path`` 再解析一次**，并且校验解析结果仍在
   媒体目录内；目录本身永远不对外列目录。

URL 不带鉴权，知道路径的人都能看到图片，路径靠随机 id 保证不可猜。对外完全
封闭的部署可以把 ``UPLOAD_MEDIA_URL`` 指到自己的鉴权反代后面。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from app.config import Settings, get_settings

logger = logging.getLogger("yangvis.slides.store")

# 模块级快照：媒体目录与 URL 前缀都从这里取，测试把它换成 SimpleNamespace 就能
# 把落盘位置指到临时目录。
settings: Settings = get_settings()

# 魔数 → (mime, 扩展名)。每项是 (文件头, 次级签名, 次级签名的偏移, mime, 扩展名)，
# 次级签名只有 WebP 用得到（RIFF 容器还可能是 WAV / AVI）。
_SIGNATURES: tuple[tuple[bytes, str, int, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "", 0, "image/png", ".png"),
    (b"\xff\xd8\xff", "", 0, "image/jpeg", ".jpg"),
    (b"GIF87a", "", 0, "image/gif", ".gif"),
    (b"GIF89a", "", 0, "image/gif", ".gif"),
    (b"RIFF", "WEBP", 8, "image/webp", ".webp"),
    (b"BM", "", 0, "image/bmp", ".bmp"),
)

# 一张幻灯片里最多能存几张配图。
SLIDE_IMAGE_MAX_BYTES = settings.SLIDE_MAX_IMAGE_BYTES

_DECK_ID_RE = re.compile(r"^\d{1,20}$")
# 文件名只能是 store_image 写得出来的那一种：16 位小写十六进制的随机 id +
# 魔数判定过的扩展名。收紧到这里，删目录时才不会碰见用户手工塞进媒体目录的
# 文件，渲染器也不可能拼出一个指向脚本/矢量图的 <img src>。
_FILE_RE = re.compile(r"^[0-9a-f]{16}\.(?:png|jpe?g|gif|webp|bmp)$")


class SlideImageError(ValueError):
    """内容不是可存下来的图片。路由按业务拒绝（``code=-1``）呈现。"""


@dataclass(frozen=True)
class StoredImage:
    id: str
    name: str
    mime: str
    size: int
    rel_path: str
    url: str


def sniff_image(raw: bytes) -> tuple[str, str]:
    """按内容判定图片格式，返回 ``(mime, 扩展名)``；识别不出抛
    :class:`SlideImageError`。"""
    for prefix, tail, offset, mime, extension in _SIGNATURES:
        if raw.startswith(prefix):
            if tail and raw[offset : offset + len(tail)] != tail.encode():
                continue
            return mime, extension
    raise SlideImageError("只支持 PNG / JPEG / GIF / WebP / BMP 图片")


def _deck_dir(deck_id: int) -> str:
    return f"slides/{deck_id}"


def store_image(deck_id: int, filename: str, raw: bytes) -> StoredImage:
    """把一份图片字节写到 ``slides/<deck_id>/<随机 id>.<真实扩展名>``。

    写失败抛 :class:`SlideImageError`——落不了盘的图片对前端没有任何意义，
    宁可拒绝入库也不要留一条打不开的素材。
    """
    if len(raw) > SLIDE_IMAGE_MAX_BYTES:
        raise SlideImageError(f"图片超过 {SLIDE_IMAGE_MAX_BYTES // 1024 // 1024}MB 上限")
    mime, extension = sniff_image(raw)

    root = settings.upload_media_path
    asset_id = uuid.uuid4().hex[:16]
    relative = f"{_deck_dir(deck_id)}/{asset_id}{extension}"
    target = root / relative

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as exc:
        logger.error("slide image write failed: %s (%s)", target, exc)
        raise SlideImageError(f"图片写入服务器失败：{exc}") from exc

    logger.info(
        "slide image stored: deck=%s id=%s %s bytes=%d",
        deck_id,
        asset_id,
        extension,
        len(raw),
    )
    return StoredImage(
        id=asset_id,
        name=(filename or "image")[:255],
        mime=mime,
        size=len(raw),
        rel_path=relative,
        url=f"{settings.UPLOAD_MEDIA_URL}/{relative}",
    )


def delete_image(rel_path: str) -> None:
    """best-effort 删掉一个文件：数据库行已经删了，磁盘残留不该阻塞删除流程。"""
    if not is_safe_rel_path(rel_path):
        logger.warning("slide image delete skipped, unsafe rel_path: %r", rel_path)
        return
    path = (settings.upload_media_path / rel_path).resolve()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("slide image delete failed: %s (%s)", path, exc)


def delete_image_dir(deck_id: int) -> None:
    """删掉一份幻灯片时连带清空它的图片目录（只删里面符合命名规则的文件）。"""
    directory = settings.upload_media_path / _deck_dir(deck_id)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and _FILE_RE.match(path.name):
            delete_image(f"{_deck_dir(deck_id)}/{path.name}")
    try:
        directory.rmdir()
    except OSError:
        # 目录里还有不认识的文件：留着，不硬删。
        logger.warning("slide deck image dir not empty, kept: %s", directory)


def resolve_url(rel_path: str) -> str:
    """由库里存的 rel_path 拼出浏览器 URL；路径不合法时返回空串（调用方按
    「这张图不可用」处理，而不是拼出一个越界 URL）。"""
    if not is_safe_rel_path(rel_path):
        logger.warning("slide image url rejected, unsafe rel_path: %r", rel_path)
        return ""
    return f"{settings.UPLOAD_MEDIA_URL}/{rel_path}"


def is_safe_rel_path(rel_path: str) -> bool:
    """路径必须是 ``slides/<deck id>/<随机 id>.<扩展名>`` 这一种形状。

    库里的 ``rel_path`` 和渲染进 ``<img src>`` 的 URL 都过这一道：写入方是
    本模块自己，但读出来的一列可能被任何途径改过（手工修库、旧数据），
    渲染器不该假设它干净。三段都单独验，不接受 ``..`` 也不接受绝对路径。
    """
    parts = rel_path.split("/")
    if len(parts) != 3 or parts[0] != "slides":
        return False
    if not _DECK_ID_RE.match(parts[1]):
        return False
    return bool(_FILE_RE.match(parts[2]))
