"""智能应用 · 素材库接口：浏览、上传、删除跨项目复用的图片/视频素材。

路由只做三件事：把调用者绑到服务上、把上传体挡在磁盘闸门之外、把服务层的
``LookupError`` / ``ValueError`` 翻译成界面看得懂的答复。素材的归属、条数闸门与
引用解绑都在 :class:`MaterialService` 里。

**上传为什么按整个字节读进来**：格式必须由文件头魔数判定，而判定要在写盘之前——
落半个不认识的文件到磁盘上，没有任何一行数据库指向它，就只能靠人肉清理。读满
上限再拒也是同一个理由，代价是一次内存占用，换的是磁盘不会被人喂脏。

视频素材这一期只是**存下来并能被镜头引用**：参考视频要发给上游就得给公网可达的地址
（base64 内联那条路对视频不成立，见 ``services/media/jobs.py`` 的 ``to_ark_video_url``）。
那条传输路已经为短视频「参考上一版片段」修好了，但**素材库里的视频还没接进去**，
所以界面与这里的答复都不说「已经用上了」。
"""
from __future__ import annotations

import asyncio
import logging
import threading

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
    MaterialBatchDeleteOut,
    MaterialBatchRestoreOut,
    MaterialBatchTagIn,
    MaterialBatchTagOut,
    MaterialIdsIn,
    MaterialListOut,
    MaterialRenameIn,
    MaterialTagIn,
    MaterialTagResult,
    MaterialTrashItemOut,
    MaterialTrashListOut,
    MaterialUsageOut,
)
from app.services.materials import MATERIAL_KINDS
from app.services.materials.service import MaterialService
from app.services.materials.store import ensure_thumbnail, store_material, thumbnail_url
from app.services.media import store
from app.services.media.store import MediaFileError
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)
from app.timeutil import db_now

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


def _undo_hint(seconds: int) -> str:
    """把撤销窗口写成界面文案：整分钟就说分钟，否则说秒。"""
    if seconds <= 0:
        return "删除不可撤销"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟内可撤销"
    return f"{seconds} 秒内可撤销"


# 一页里当场生成缩略图的上限：每张几十毫秒，24 张是「最坏也就多等一秒」的档位。
_THUMB_GEN_BUDGET = 24


def _resolve_thumbs(rows) -> list[str]:
    """按行的顺序解析缩略图 URL：已在盘上的直接给；缺的当场补最新的一批，剩下的
    丢进后台线程慢慢补。

    缺口只来自存量旧素材（新上传的在落盘时已经顺手生成过）。旧库第一次被读到时
    全部当场补的话，几百张图能把一个列表请求拖成几十秒；按最新优先补一批，这一屏
    最坏也就多等一秒，余下的在后台补完，下一次列表就全是纯静态文件的速度。后台
    那一趟只碰文件（路径全部验过形状），不碰数据库，重复补同一张也只是同字节写两遍。
    """
    urls: list[str] = []
    pending: list[tuple[str, str]] = []
    budget = _THUMB_GEN_BUDGET
    for row in rows:
        rel, kind = str(row.rel_path or ""), str(row.kind or "")
        url = thumbnail_url(rel, kind)
        if url:
            urls.append(url)
        elif budget > 0:
            budget -= 1
            urls.append(ensure_thumbnail(rel, kind))
        else:
            urls.append("")
            pending.append((rel, kind))
    if pending:

        def _backfill() -> None:
            for rel, kind in pending:
                ensure_thumbnail(rel, kind)

        threading.Thread(target=_backfill, daemon=True).start()
    return urls


def _out(asset, thumb_url: str = "") -> MaterialAssetOut:
    return MaterialAssetOut(
        id=asset.id,
        kind=asset.kind,
        name=asset.name,
        tag=asset.tag,
        mime=asset.mime,
        size=asset.size,
        # URL 由 rel_path 现拼：文件被手工挪走时这里给空串，界面按「素材已丢失」处理。
        url=store.resolve_url(asset.rel_path),
        # 缩略图由调用方给（列表按预算批量解析，单条的场景传 ensure_thumbnail 的结果）；
        # 空串时界面回退原图——提速件坏了不该把列表也带走。
        thumb_url=thumb_url,
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
    """一屏素材 + 全库用量。``kind`` 不在白名单就按「全部」处理，不报错；``q`` 同时搜名字与分组标签。"""
    rows, total, bytes_total = service.list_assets(
        kind=kind if kind in MATERIAL_KINDS else None, keyword=q, limit=limit, offset=offset
    )
    thumbs = _resolve_thumbs(rows)
    return APIResponse(
        data=MaterialListOut(
            items=[_out(row, thumb) for row, thumb in zip(rows, thumbs)],
            total=total,
            bytes_total=bytes_total,
            max_per_owner=settings.MATERIAL_MAX_PER_OWNER,
            trash_ttl_seconds=service.trash_ttl_seconds,
            # 列表读取本来就会先懒清理过期项，这里顺手数一遍，入口角标不用单开请求。
            trash_count=service.trash_count(),
        )
    )


@router.get("/trash", response_model=APIResponse[MaterialTrashListOut])
def list_trash_materials(
    kind: str | None = Query(default=None, max_length=8),
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialTrashListOut]:
    """回收站一屏：只列撤销窗口内的素材，带距真删的剩余秒数。

    必须声明在 ``/{asset_id}`` 那组路由之前——虽然目前没有 GET 单条路由，
    显式固定顺序能防后来人加一条 ``GET /{asset_id}`` 把 ``trash`` 吃成 id。
    """
    rows, total = service.list_trash(
        kind=kind if kind in MATERIAL_KINDS else None,
        keyword=q,
        limit=limit,
        offset=offset,
    )
    thumbs = _resolve_thumbs(rows)
    ttl = service.trash_ttl_seconds
    now = db_now()
    items: list[MaterialTrashItemOut] = []
    for row, thumb_url in zip(rows, thumbs):
        base = _out(row, thumb_url).model_dump()
        remaining = ttl
        if row.deleted_at is not None:
            remaining = max(0, ttl - int((now - row.deleted_at).total_seconds()))
        items.append(
            MaterialTrashItemOut(
                **base,
                deleted_at=row.deleted_at,
                expires_in_seconds=remaining,
            )
        )
    return APIResponse(
        data=MaterialTrashListOut(items=items, total=total, trash_ttl_seconds=ttl)
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
    return APIResponse(
        data=_out(
            asset,
            ensure_thumbnail(str(asset.rel_path or ""), str(asset.kind or "")),
        ),
        message="已存入素材库",
    )


@router.put("/{asset_id}/tag", response_model=APIResponse[MaterialAssetOut])
def set_material_tag(
    asset_id: int,
    body: MaterialTagIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialAssetOut]:
    """改一条素材的打标分组；空串摘掉标签，归一回「按时间分组」那一边。"""
    try:
        asset = service.set_tag(asset_id, body.tag)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(
        data=_out(
            asset,
            ensure_thumbnail(str(asset.rel_path or ""), str(asset.kind or "")),
        ),
        message="已更新分组",
    )


@router.put("/{asset_id}/name", response_model=APIResponse[MaterialAssetOut])
def rename_material(
    asset_id: int,
    body: MaterialRenameIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialAssetOut]:
    """给素材改名；空名（含全空格）拒绝。"""
    try:
        asset = service.rename(asset_id, body.name)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(
        data=_out(
            asset,
            ensure_thumbnail(str(asset.rel_path or ""), str(asset.kind or "")),
        ),
        message="已重命名",
    )


@router.post("/batch-tag", response_model=APIResponse[MaterialBatchTagOut])
def batch_tag_materials(
    body: MaterialBatchTagIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialBatchTagOut]:
    """一批素材改成同一个分组：整组改名、批量整理都走这一条，不再逐条发请求。"""
    try:
        rows = service.set_tag_many(body.ids, body.tag)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(
        data=MaterialBatchTagOut(
            items=[MaterialTagResult(id=row.id, tag=row.tag) for row in rows]
        ),
        message=f"已更新 {len(rows)} 条" if rows else "没有找到要更新的素材",
    )


@router.post("/usage", response_model=APIResponse[list[MaterialUsageOut]])
def material_usage(
    body: MaterialIdsIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[list[MaterialUsageOut]]:
    """删除前的影响面：每条素材被哪些项目的哪些镜头引用。

    批量删除时把整批 id 一次带过来，返回与入参同序，界面按项目聚合即可。
    """
    return APIResponse(data=service.usage(body.ids))


@router.post("/batch-delete", response_model=APIResponse[MaterialBatchDeleteOut])
def batch_delete_materials(
    body: MaterialIdsIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialBatchDeleteOut]:
    """批量删除素材：先进回收期，窗口内可撤销；过期才删文件、解掉镜头引用。"""
    deleted, references = service.remove_many(body.ids)
    message = f"已删除 {deleted} 条素材，{_undo_hint(service.trash_ttl_seconds)}"
    if references:
        message += f"；{references} 个镜头还在引用"
    return APIResponse(
        data=MaterialBatchDeleteOut(
            deleted=deleted,
            references=references,
            undo_seconds=service.trash_ttl_seconds,
        ),
        message=message,
    )


@router.post("/batch-restore", response_model=APIResponse[MaterialBatchRestoreOut])
def restore_materials(
    body: MaterialIdsIn,
    service: MaterialService = Depends(get_service),
) -> APIResponse[MaterialBatchRestoreOut]:
    """撤销删除：把还在撤销窗口里的素材原样恢复。"""
    restored = service.restore_many(body.ids)
    return APIResponse(
        data=MaterialBatchRestoreOut(restored=restored),
        message=f"已恢复 {restored} 条素材" if restored else "没有可恢复的素材，可能已过撤销期限",
    )


@router.delete("/{asset_id}", response_model=APIResponse[None])
def delete_material(
    asset_id: int,
    service: MaterialService = Depends(get_service),
) -> APIResponse[None]:
    """删一条素材：先进回收期，窗口内可撤销；引用到期才解绑。"""
    try:
        references = service.remove(asset_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    message = f"已删除，{_undo_hint(service.trash_ttl_seconds)}"
    if references:
        message += f"；{references} 个镜头还在引用"
    return APIResponse(message=message)
