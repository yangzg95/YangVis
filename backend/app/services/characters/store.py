"""人物形象图的落盘。

三道防线与 :mod:`app.services.materials.store` 完全同一套（魔数定扩展名、随机文件名、
``rel_path`` 用之前先过媒体层的路径守卫），只留两处差异：

* 目录形状是 ``characters/<owner_id>/`` 而不是 ``materials/``——角色按账号归属、
  跨项目复用，删一条短视频不该带走人物形象图，反之亦然；
* **只收图片**：角色要的是一张稳定的人脸/穿搭参考，视频容器在这条链路上没有入口，
  魔数判出视频直接拒绝。

缩略图沿用素材库的命名（``<stem>_thumb.webp``）与 320px 档位：卡片网格里不直接拉
原图，删除主文件时媒体层的 :func:`delete_blob` 会顺手把缩略图一起清掉。
"""
from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass

from PIL import Image

from app.config import Settings, get_settings
from app.services.media.store import MediaFileError, is_safe_rel_path, sniff_media

logger = logging.getLogger("yangvis.characters")

settings: Settings = get_settings()

_DIR = "characters"

# 缩略图最长边：与素材库同一档，卡片显示宽约 180~220px，2x 屏 320px 打住。
_THUMB_MAX_EDGE = 320


@dataclass(frozen=True)
class StoredCharacterImage:
    mime: str
    size: int
    rel_path: str
    url: str


def store_character_image(owner_id: int, raw: bytes) -> StoredCharacterImage:
    """把上传字节写到 ``characters/<账号 id>/<随机 id>.<真实扩展名>``。

    只认图片：视频与识别不出的内容都抛 :class:`MediaFileError`，由路由按业务拒绝
    （``code=-1``）呈现。拒绝必须在写盘之前——半个文件留在盘上没有任何一行数据库
    指向它。
    """
    mime, extension = sniff_media(raw)
    if not mime.startswith("image/"):
        raise MediaFileError("人物形象图只支持 PNG / JPEG / GIF / WebP / BMP 图片")
    if len(raw) > settings.MATERIAL_MAX_IMAGE_BYTES:
        limit_mb = settings.MATERIAL_MAX_IMAGE_BYTES // 1024 // 1024
        raise MediaFileError(f"形象图超过 {limit_mb}MB 上限")

    relative = f"{_DIR}/{owner_id}/{uuid.uuid4().hex[:16]}{extension}"
    target = settings.upload_media_path / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as exc:
        logger.error("character image write failed: %s (%s)", target, exc)
        raise MediaFileError(f"形象图写入服务器失败：{exc}") from exc

    logger.info(
        "character image stored: owner=%s mime=%s bytes=%d -> %s",
        owner_id,
        mime,
        len(raw),
        relative,
    )
    # 与素材库同一取舍：落库后立刻可能出现在卡片网格里，当场生成缩略图，别让第一屏
    # 去拉原图。失败只回空串，不阻断主流程（ensure_thumbnail 内部已兜底）。
    ensure_thumbnail(relative)
    return StoredCharacterImage(
        mime=mime,
        size=len(raw),
        rel_path=relative,
        url=f"{settings.UPLOAD_MEDIA_URL}/{relative}",
    )


def thumbnail_rel_path(rel_path: str) -> str | None:
    """主文件 ``rel_path`` → 缩略图 ``rel_path``；主路径形状不合法返回 ``None``。"""
    if not is_safe_rel_path(rel_path):
        return None
    directory, _, filename = rel_path.rpartition("/")
    stem, _, _ = filename.partition(".")
    return f"{directory}/{stem}_thumb.webp"


def thumbnail_url(rel_path: str) -> str:
    """缩略图已在盘上 → 浏览器 URL；不在（或路径不合法）→ 空串，界面回退原图。"""
    thumb = thumbnail_rel_path(rel_path)
    if not thumb:
        return ""
    if not (settings.upload_media_path / thumb).exists():
        return ""
    return f"{settings.UPLOAD_MEDIA_URL}/{thumb}"


def ensure_thumbnail(rel_path: str) -> str:
    """确保这张形象图的缩略图已在盘上，返回可直接给浏览器的 URL；任何失败回退空串。

    与素材库同一份克制：先攒进内存再一次落盘，写一半的文件不会被当成好缩略图；
    缩略图是提速件，坏了不该把列表带走。
    """
    url = thumbnail_url(rel_path)
    if url:
        return url
    thumb = thumbnail_rel_path(rel_path)
    if not thumb:
        return ""

    try:
        with Image.open(settings.upload_media_path / rel_path) as image:
            image.thumbnail((_THUMB_MAX_EDGE, _THUMB_MAX_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, "WEBP", quality=80)
    except (OSError, ValueError, KeyError) as exc:
        # UnidentifiedImageError 是 OSError 的子类，损坏图与文件丢失一并落在这里。
        logger.warning("character thumbnail failed: %s (%s)", rel_path, exc)
        return ""
    try:
        target = settings.upload_media_path / thumb
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(buffer.getvalue())
    except OSError as exc:
        logger.warning("character thumbnail write failed: %s (%s)", target, exc)
        return ""
    logger.info("character thumbnail stored: %s -> %s", rel_path, thumb)
    return f"{settings.UPLOAD_MEDIA_URL}/{thumb}"
