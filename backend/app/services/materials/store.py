"""素材条目的落盘。

三道防线与 :mod:`app.services.media.store` 完全同一套（魔数定扩展名、随机文件名、
``rel_path`` 用之前先重新解析一次），所以这里只写不一样的那部分：**目录形状与大小
闸门**。读取、拼 URL、删文件继续用媒体层那三个函数——它们的守卫已经认
``materials/`` 这一种前缀，另写一份只会让同一份磁盘布局有两个校验口径。

按账号分目录（``materials/<owner_id>/``）而不是按项目：库是跨项目复用的，删一条
短视频项目绝不该顺手带走用户的人物形象图；反过来，删一条素材要能反查谁在引用它，
那件事在 :class:`~app.services.materials.service.MaterialService` 里做。

``/uploads`` 不带鉴权（``<img>`` 与 ``<video>`` 发不出 Authorization 头），靠随机 id
保证不可猜；界面上给用户的名字只进数据库，永远不参与路径拼接。
"""
from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass

from PIL import Image

from app.config import Settings, get_settings
from app.services.materials import KIND_IMAGE, KIND_VIDEO
from app.services.media.store import MediaFileError, is_safe_rel_path, sniff_media

logger = logging.getLogger("yangvis.materials")

settings: Settings = get_settings()

_DIR = "materials"

# 缩略图最长边：卡片显示宽约 180~220px，2x 屏 400px 打住；再大就白传字节了。
_THUMB_MAX_EDGE = 320


@dataclass(frozen=True)
class StoredMaterial:
    kind: str
    mime: str
    size: int
    rel_path: str
    url: str


def store_material(owner_id: int, raw: bytes) -> StoredMaterial:
    """把一段上传字节写到 ``materials/<账号 id>/<随机 id>.<真实扩展名>``。

    超限与格式不认识都抛 :class:`MediaFileError`：路由按业务拒绝（``code=-1``）
    呈现，而拒绝必须在写盘之前——半个文件留在盘上没有任何一行数据库指向它。
    """
    mime, extension = sniff_media(raw)
    is_video = mime.startswith("video/")
    limit = settings.MATERIAL_MAX_VIDEO_BYTES if is_video else settings.MATERIAL_MAX_IMAGE_BYTES
    if len(raw) > limit:
        raise MediaFileError(f"素材超过 {limit // 1024 // 1024}MB 上限")

    relative = f"{_DIR}/{owner_id}/{uuid.uuid4().hex[:16]}{extension}"
    target = settings.upload_media_path / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as exc:
        logger.error("material blob write failed: %s (%s)", target, exc)
        raise MediaFileError(f"素材写入服务器失败：{exc}") from exc

    logger.info(
        "material blob stored: owner=%s mime=%s bytes=%d -> %s", owner_id, mime, len(raw), relative
    )
    # 图片顺手生成列表缩略图：传输本身就要等几秒，多几十毫秒没人察觉，而它一落库
    # 就可能出现在列表里，那张卡不该再等第一次列表来补。存量旧素材没有这一步，
    # 由列表读到的那个「按需补」兜底。
    if not is_video:
        ensure_thumbnail(relative, KIND_IMAGE)
    return StoredMaterial(
        kind=KIND_VIDEO if is_video else KIND_IMAGE,
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


def thumbnail_url(rel_path: str, kind: str) -> str:
    """缩略图已在盘上 → 浏览器 URL；不在（或这条不该有）→ 空串。

    只做一次 ``stat``，绝不生成：列表一页要过几十条，「查有没有」和「没有就补一张」
    得是两个动作，调用方才补得起预算（见路由层）。
    """
    thumb = thumbnail_rel_path(rel_path)
    if not thumb or kind != KIND_IMAGE:
        return ""
    if not (settings.upload_media_path / thumb).exists():
        return ""
    return f"{settings.UPLOAD_MEDIA_URL}/{thumb}"


def ensure_thumbnail(rel_path: str, kind: str) -> str:
    """确保这张素材的缩略图已在盘上，返回可直接给浏览器的 URL。

    为什么列表必须走缩略图：首帧是 2K PNG，一张好几 MB，一屏几十张原图同时进
    网络就是卡的根因；320px 的 WebP 一张几十 KB，差出两个数量级。已在盘上直接
    返回；没有就生成一张，之后走 StaticFiles，与静态文件同一速度。视频不生成：
    ``<video preload=metadata>`` 本来就只取首帧元数据，列表上那一屏没有一个视频
    走全量字节。

    任何一步失败都退回空串，界面自己回退到原图 URL——缩略图是提速件，坏了
    不该把列表也带走。先攒进内存再一次落盘，写一半的文件不会留在盘上被当成
    好缩略图；并发下两边各生成一次也只是同字节写两遍，无害。
    """
    url = thumbnail_url(rel_path, kind)
    if url:
        return url
    if kind != KIND_IMAGE:
        return ""
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
        logger.warning("material thumbnail failed: %s (%s)", rel_path, exc)
        return ""
    try:
        target = settings.upload_media_path / thumb
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(buffer.getvalue())
    except OSError as exc:
        logger.warning("material thumbnail write failed: %s (%s)", target, exc)
        return ""
    logger.info("material thumbnail stored: %s -> %s", rel_path, thumb)
    return f"{settings.UPLOAD_MEDIA_URL}/{thumb}"
