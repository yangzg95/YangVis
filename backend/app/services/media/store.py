"""媒体文件在服务器本地磁盘的存储：短视频产物 + 素材库条目。

为什么必须转存而不是直接用方舟返回的 URL：文档写明输出的 ``video_url`` **24 小时
后失效**，任务记录 7 天后查不到。把那个 URL 存进数据库，等于给用户一份第二天就打
不开的成片。

三道防线与 ``services/slides/store.py`` 同一套，只是这里还要认视频容器：

1. **扩展名按文件头魔数判定**，不信 ``Content-Type``，也不信上游 URL 的后缀——
   一个改名成 ``.mp4`` 的 HTML 存下来，浏览器 ``<video>`` 解不开它，也就当不了脚本执行。
2. **文件名是随机 id + 服务端给的扩展名**，上游 URL、镜头号、用户给的名字都只进
   数据库做展示，永远不参与路径拼接。
3. **删除与读取都按 ``rel_path`` 重新解析一次**并校验仍在媒体目录内；形状不是
   ``<videos|materials|characters>/<数字 id>/<随机 id>.<扩展名>`` 的一律拒绝。
   ``materials`` 是素材库的落盘目录（``services/materials/store.py`` 写入），
   ``characters`` 是人物库形象图的落盘目录（``services/characters/store.py`` 写入）：
   首帧渲染要把选中的素材/角色形象图内联成 base64 送给方舟，读的一方不该只有产物
   目录能读。

``/uploads`` 不带鉴权（``<video>`` 和 ``<img>`` 发不出 Authorization 头），靠随机 id
保证不可猜。要把 ``image_url`` 内联成 base64 送给方舟时走 :func:`read_blob`，
不落第二次盘。
"""
from __future__ import annotations

import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger("yangvis.media.store")

settings: Settings = get_settings()

_DIR = "videos"
# 媒体目录下的三种树：产物按项目分（删项目 = 删一棵目录树），素材与人物形象图
# 按账号分（跨项目复用，删项目不碰它们）。三者的文件名规则完全相同。
_ROOTS = (_DIR, "materials", "characters")

# 每项是 (文件头所在偏移, 文件头, mime, 扩展名)。WebP 比 RIFF 容器的次级签名（偏移
# 8），视频比 ISO BMFF 的 'ftyp'（前 4 字节是 box 长度，所以偏移 4）。
_SIGNATURES: tuple[tuple[int, bytes, str, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (0, b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (0, b"GIF87a", "image/gif", ".gif"),
    (0, b"GIF89a", "image/gif", ".gif"),
    (8, b"WEBP", "image/webp", ".webp"),
    (0, b"BM", "image/bmp", ".bmp"),
    (4, b"ftyp", "video/mp4", ".mp4"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm", ".webm"),
)
# ``<随机 id>[_thumb].<白名单扩展名>``。``_thumb`` 是素材缩略图
# （``services/materials/store.py`` 生成）：只出现在 ``materials`` 树下，但这里的
# 口径是纯形状白名单（安全边界是「仍在媒体目录内」），一并放行不值得分树特判。
_EXT_RE = re.compile(r"^[0-9a-f]{16}(?:_thumb)?\.(?:png|jpe?g|gif|webp|bmp|mp4|webm)$")
_NUMERIC_ID_RE = re.compile(r"^\d{1,20}$")

# 一张首帧图/一段片段的上限。真正拦人的是上游（图 <30MB），这里是磁盘闸门：
# 生产那台机器盘很紧，宁可拒绝一条超限的产物，也不要让它把卷写满。
_IMAGE_MAX_BYTES = settings.MEDIA_IMAGE_MAX_BYTES
_VIDEO_MAX_BYTES = settings.MEDIA_VIDEO_MAX_BYTES


class MediaFileError(ValueError):
    """内容不是可存下来的媒体文件，或磁盘写不下。路由按业务拒绝（``code=-1``）呈现。"""


@dataclass(frozen=True)
class StoredBlob:
    id: str
    mime: str
    ext: str
    size: int
    rel_path: str
    url: str


def sniff_media(raw: bytes) -> tuple[str, str]:
    """按文件头判定 ``(mime, 扩展名)``；识别不出抛 :class:`MediaFileError`。

    按顺序比，图片签名先于 ``ftyp``：``ftyp`` 落在偏移 4，任何前 8 字节里恰好含这
    四个字符的文件都可能被误判成视频，而视频误判的后果是存下一个浏览器解不开的
    容器。真视频的 PNG/JPEG 签名不可能出现在偏移 0，所以这个顺序不会误伤。
    """
    if not raw:
        raise MediaFileError("产物是空的")
    for offset, magic, mime, extension in _SIGNATURES:
        if raw[offset : offset + len(magic)] == magic:
            return mime, extension
    raise MediaFileError("只支持 PNG / JPEG / GIF / WebP / BMP 图片与 MP4 / WebM 视频")


def store_blob(project_id: int, raw: bytes) -> StoredBlob:
    """把一段产物字节写到 ``videos/<project_id>/<随机 id>.<真实扩展名>``。

    按项目分目录而不是按镜头：删一个项目就是删一棵目录树，而镜头行会被重排、
    重做、整批替换，按镜头建目录既留不下干净的结构，也容易删漏。
    """
    mime, extension = sniff_media(raw)
    limit = _VIDEO_MAX_BYTES if mime.startswith("video/") else _IMAGE_MAX_BYTES
    if len(raw) > limit:
        raise MediaFileError(f"产物超过 {limit // 1024 // 1024}MB 上限")

    asset_id = uuid.uuid4().hex[:16]
    relative = f"{_DIR}/{project_id}/{asset_id}{extension}"
    target = settings.upload_media_path / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as exc:
        logger.error("media blob write failed: %s (%s)", target, exc)
        raise MediaFileError(f"产物写入服务器失败：{exc}") from exc

    logger.info(
        "media blob stored: project=%s id=%s mime=%s bytes=%d",
        project_id,
        asset_id,
        mime,
        len(raw),
    )
    return StoredBlob(
        id=asset_id,
        mime=mime,
        ext=extension,
        size=len(raw),
        rel_path=relative,
        url=f"{settings.UPLOAD_MEDIA_URL}/{relative}",
    )


def read_blob(rel_path: str) -> bytes:
    """按库里的 ``rel_path`` 读回产物字节（内联成 data URL 送给方舟时用）。

    路径不合法或文件不存在都抛 :class:`MediaFileError`：调用方是作业执行器，
    它需要一条明确的失败去写进 ``media_job.error_msg``，而不是一个半截请求。
    """
    if not is_safe_rel_path(rel_path):
        raise MediaFileError(f"非法的产物路径：{rel_path!r}")
    path = settings.upload_media_path / rel_path
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MediaFileError(f"产物文件读不出来：{exc}") from exc
    if not raw:
        raise MediaFileError("产物文件是空的")
    return raw


def store_file(project_id: int, src: Path) -> StoredBlob:
    """把一个**已经在磁盘上**的成品文件移进媒体目录并登记（ffmpeg 合成的成片走这里）。

    与 :func:`store_blob` 同一套防线，但入口是路径而不是字节：合成产物可能上百 MB，
    先整块读进内存再写一遍只是白白复制两次。仍然按文件头魔数判类型（只读前 16 字节，
    所有签名的最大偏移是 8），不信容器后缀；文件名同样是随机 id，``src`` 的名字不参与
    路径拼接。``shutil.move`` 在同一文件系统上是原子改名——合成的临时目录就开在媒体
    目录下面，落库时不会出现半个文件。
    """
    try:
        with src.open("rb") as handle:
            head = handle.read(16)
        size = src.stat().st_size
    except OSError as exc:
        raise MediaFileError(f"成品文件读不出来：{exc}") from exc
    mime, extension = sniff_media(head)
    limit = _VIDEO_MAX_BYTES if mime.startswith("video/") else _IMAGE_MAX_BYTES
    if size > limit:
        raise MediaFileError(f"成品超过 {limit // 1024 // 1024}MB 上限")

    asset_id = uuid.uuid4().hex[:16]
    relative = f"{_DIR}/{project_id}/{asset_id}{extension}"
    target = settings.upload_media_path / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
    except OSError as exc:
        logger.error("media file move failed: %s -> %s (%s)", src, target, exc)
        raise MediaFileError(f"成品写入服务器失败：{exc}") from exc

    logger.info(
        "media file stored: project=%s id=%s mime=%s bytes=%d",
        project_id,
        asset_id,
        mime,
        size,
    )
    return StoredBlob(
        id=asset_id,
        mime=mime,
        ext=extension,
        size=size,
        rel_path=relative,
        url=f"{settings.UPLOAD_MEDIA_URL}/{relative}",
    )


def resolve_url(rel_path: str) -> str:
    """由 ``rel_path`` 拼浏览器 URL；不合法返回空串，让调用方按「没有产物」处理。"""
    if not is_safe_rel_path(rel_path):
        logger.warning("media url rejected, unsafe rel_path: %r", rel_path)
        return ""
    return f"{settings.UPLOAD_MEDIA_URL}/{rel_path}"


def delete_blob(rel_path: str) -> None:
    """best-effort 删一个文件：数据库行已经删了，磁盘残留不该阻塞删除流程。

    顺手连 ``_thumb`` 变体一起删：素材删了，它的缩略图不该变孤儿。缩略图固定是
    ``.webp``（见 ``services/materials/store.py``），与主文件扩展名无关；别处这一步
    就是查一个不存在的路径，没有副作用。
    """
    if not is_safe_rel_path(rel_path):
        logger.warning("media blob delete skipped, unsafe rel_path: %r", rel_path)
        return
    path = (settings.upload_media_path / rel_path).resolve()
    try:
        path.unlink(missing_ok=True)
        stem, _, _ = path.name.partition(".")
        path.with_name(f"{stem}_thumb.webp").unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("media blob delete failed: %s (%s)", path, exc)


def delete_project_dir(project_id: int) -> None:
    """删项目时连带清空它的产物目录（只删符合命名规则的文件）。"""
    directory = settings.upload_media_path / f"{_DIR}/{project_id}"
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and _EXT_RE.match(path.name):
            delete_blob(f"{_DIR}/{project_id}/{path.name}")
    try:
        directory.rmdir()
    except OSError:
        # 目录里还有不认识的文件：留着，不硬删。
        logger.warning("media project dir not empty, kept: %s", directory)


def is_safe_rel_path(rel_path: str) -> bool:
    """路径必须是 ``<videos|materials|characters>/<数字 id>/<随机 id>.<白名单扩展名>``
    这一种形状。

    写入方是本模块、``services/materials/store.py`` 与 ``services/characters/store.py``，
    但这一列可能被任何途径改过（手工修库、旧数据），读出来拼接 URL 或读磁盘的一方
    不该假设它干净。三段都单独验。
    """
    parts = rel_path.split("/")
    if len(parts) != 3 or parts[0] not in _ROOTS:
        return False
    if not _NUMERIC_ID_RE.match(parts[1]):
        return False
    return bool(_EXT_RE.match(parts[2]))
