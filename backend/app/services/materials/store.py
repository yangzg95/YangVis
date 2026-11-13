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

import logging
import uuid
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.services.materials import KIND_IMAGE, KIND_VIDEO
from app.services.media.store import MediaFileError, sniff_media

logger = logging.getLogger("yangvis.materials")

settings: Settings = get_settings()

_DIR = "materials"


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
    return StoredMaterial(
        kind=KIND_VIDEO if is_video else KIND_IMAGE,
        mime=mime,
        size=len(raw),
        rel_path=relative,
        url=f"{settings.UPLOAD_MEDIA_URL}/{relative}",
    )
