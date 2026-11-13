"""智能应用 · 素材库接口：浏览、上传、删除跨项目复用的图片/视频素材。

路由只做三件事：把调用者绑到服务上、把上传体挡在磁盘闸门之外、把服务层的
``LookupError`` / ``ValueError`` 翻译成界面看得懂的答复。素材的归属、条数闸门与
引用解绑都在 :class:`MaterialService` 里。

**上传为什么按整个字节读进来**：格式必须由文件头魔数判定，而判定要在写盘之前——
落半个不认识的文件到磁盘上，没有任何一行数据库指向它，就只能靠人肉清理。读满
上限再拒也是同一个理由，代价是一次内存占用，换的是磁盘不会被人喂脏。

视频素材这一期只是**存下来并能被镜头引用**：渲染只到首帧，而参考视频要发给上游
就得给公网可达的地址（base64 内联那条路对视频不成立，见 ``services/media/jobs.py``
的 ``to_ark_image_url``）。链路还没接通，所以界面与这里的答复都不说「已经用上了」。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    MaterialAssetOut,
    MaterialListOut,
)
from app.services.materials import MATERIAL_KINDS
from app.services.materials.service import MaterialService
from app.services.materials.store import store_material
from app.services.media import store
from app.services.media.store import MediaFileError
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)

logger = logging.getLogger("yangvis.materials")

settings = get_settings()

router = APIRouter(prefix="/office/materials", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> MaterialService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return MaterialService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _out(asset) -> MaterialAssetOut:
    return MaterialAssetOut(
        id=asset.id,
        kind=asset.kind,
        name=asset.name,
        mime=asset.mime,
        size=asset.size,
        # URL 由 rel_path 现拼：文件被手工挪走时这里给空串，界面按「素材已丢失」处理。
        url=store.resolve_url(asset.rel_path),
        created_at=asset.created_at,
    )


@router.get("", response_model=APIResponse[MaterialListOut])
def list_materials(
    kind: str | None = Query(default=None, max_length=8),
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialListOut]:
    """一屏素材 + 全库用量。``kind`` 不在白名单就按「全部」处理，不报错。"""
    rows, total, bytes_total = service.list_assets(
        kind=kind if kind in MATERIAL_KINDS else None, keyword=q, limit=limit, offset=offset
    )
    return APIResponse(
        data=MaterialListOut(
            items=[_out(row) for row in rows],
            total=total,
            bytes_total=bytes_total,
            max_per_owner=settings.MATERIAL_MAX_PER_OWNER,
        )
    )


@router.post("", response_model=APIResponse[MaterialAssetOut])
async def upload_material(
    request: Request,
    file: UploadFile = File(...),
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialAssetOut]:
    """上传一条素材：按内容魔数判图/视频，落进本账号的素材目录。

    文件名不参与任何判定（扩展名由魔数决定），它只作为界面上看得出的名字存着。
    """
    # 上限按最宽的那一档先挡一次请求体：真正决定拒不拒的是文件头，但 Content-Length
    # 已经能拦下明显超量的整段传输，不必把 200MB 读进内存才知道不行。
    limit = settings.MATERIAL_MAX_VIDEO_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))

    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))

    try:
        # 落盘是同步 IO，放线程里。
        stored = await asyncio.to_thread(store_material, service.owner_id, raw)
    except MediaFileError as exc:
        return APIResponse(code=-1, message=str(exc))

    try:
        asset = service.add(stored, file.filename or "素材")
    except ValueError as exc:
        # 落库没成功的话，刚才写下的文件就是孤儿，顺手清掉。
        store.delete_blob(stored.rel_path)
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=_out(asset), message="已存入素材库")


@router.delete("/{asset_id}", response_model=APIResponse[None])
def delete_material(
    asset_id: int,
    service: MaterialService = Depends(get_service),
) -> APIResponse[None]:
    """删一条素材，并解掉引用它的镜头。"""
    try:
        rel_path, unbound = service.remove(asset_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    if rel_path:
        store.delete_blob(rel_path)
    message = f"已删除，{unbound} 个镜头去掉这条引用" if unbound else "已删除"
    return APIResponse(message=message)
