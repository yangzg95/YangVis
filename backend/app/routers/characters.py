"""智能应用 · 人物库接口：角色档案与形象图的建档、浏览、改档、换图与删除。

路由只做三件事（与素材库路由同一分工）：把调用者绑到服务上、把上传体挡在磁盘
闸门之外、把服务层的 ``LookupError`` / ``ValueError`` 翻译成界面看得懂的答复。
角色的归属、条数闸门与引用反查都在 :class:`CharacterService` 里。

**上传为什么按整个字节读进来**：形象图只收图片，格式必须由文件头魔数判定，而判定
要在写盘之前——落半个不认识的文件到磁盘上，没有任何一行数据库指向它，只能靠人肉
清理。视频与非媒体内容在存储层直接拒，不进库。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
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
    CharacterDeleteOut,
    CharacterIdsIn,
    CharacterListOut,
    CharacterOut,
    CharacterUpdateIn,
    CharacterUsageOut,
    CurrentUser,
)
from app.services.characters.service import CharacterService
from app.services.characters.store import ensure_thumbnail, store_character_image
from app.services.media import store
from app.services.media.store import MediaFileError
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)

logger = logging.getLogger("yangvis.characters")

settings = get_settings()

router = APIRouter(prefix="/office/characters", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> CharacterService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return CharacterService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _out(row) -> CharacterOut:
    rel = str(row.rel_path or "")
    return CharacterOut(
        id=row.id,
        name=row.name,
        persona=row.persona or "",
        outfit=row.outfit or "",
        features=row.features or "",
        mime=row.mime or "",
        size=row.size or 0,
        # URL 由 rel_path 现拼：文件被手工挪走时给空串，界面按「形象图已丢失」处理。
        url=store.resolve_url(rel),
        thumb_url=ensure_thumbnail(rel),
        created_at=row.created_at,
    )


@router.get("", response_model=APIResponse[CharacterListOut])
def list_characters(
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterListOut]:
    """一页角色 + 筛选后的总数。``q`` 只搜角色名。"""
    rows, total = service.list_characters(keyword=q, limit=limit, offset=offset)
    return APIResponse(
        data=CharacterListOut(
            items=[_out(row) for row in rows],
            total=total,
            max_per_owner=settings.CHARACTER_MAX_PER_OWNER,
        )
    )


@router.post("", response_model=APIResponse[CharacterOut])
async def create_character(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    persona: str = Form(default=""),
    outfit: str = Form(default=""),
    features: str = Form(default=""),
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterOut]:
    """建档：multipart 一次收形象图与文字档案，魔数判型在写盘之前。"""
    # 图片按素材库图片档先挡一次请求体，真正决定拒不拒的是文件头。
    limit = settings.MATERIAL_MAX_IMAGE_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))

    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))

    try:
        # 落盘是同步 IO，放线程里；非图片内容在这一层被拒。
        stored = await asyncio.to_thread(store_character_image, service.owner_id, raw)
    except MediaFileError as exc:
        return APIResponse(code=-1, message=str(exc))

    try:
        row = service.create(
            name=name, persona=persona, outfit=outfit, features=features, stored=stored
        )
    except ValueError as exc:
        # 落库没成功的话，刚写下的文件就是孤儿，顺手清掉。
        store.delete_blob(stored.rel_path)
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=_out(row), message="已建立人物档案")


@router.put("/{character_id}", response_model=APIResponse[CharacterOut])
def update_character(
    character_id: int,
    body: CharacterUpdateIn,
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterOut]:
    """改文字档案（人设/衣着/特色/名字），不动形象图。"""
    try:
        row = service.update(
            character_id,
            name=body.name,
            persona=body.persona,
            outfit=body.outfit,
            features=body.features,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(data=_out(row), message="已更新人物档案")


@router.put("/{character_id}/portrait", response_model=APIResponse[CharacterOut])
async def replace_portrait(
    character_id: int,
    request: Request,
    file: UploadFile = File(...),
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterOut]:
    """换形象图：新图先落盘并过魔数判定，库里的事实提交后旧文件才 best-effort 删。"""
    limit = settings.MATERIAL_MAX_IMAGE_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))

    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))

    try:
        stored = await asyncio.to_thread(store_character_image, service.owner_id, raw)
    except MediaFileError as exc:
        return APIResponse(code=-1, message=str(exc))

    try:
        row = service.replace_portrait(character_id, stored)
    except LookupError as exc:
        # 角色不存在：新图没有任何一行数据库指向它，清掉再 404。
        store.delete_blob(stored.rel_path)
        raise _not_found(exc) from exc
    return APIResponse(data=_out(row), message="已更换形象图")


@router.post("/usage", response_model=APIResponse[list[CharacterUsageOut]])
def character_usage(
    body: CharacterIdsIn,
    service: CharacterService = Depends(get_service),
) -> APIResponse[list[CharacterUsageOut]]:
    """删除前的影响面：每个角色被哪些短视频项目选进了角色组。"""
    return APIResponse(data=service.usage(body.ids))


@router.delete("/{character_id}", response_model=APIResponse[CharacterDeleteOut])
def delete_character(
    character_id: int,
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterDeleteOut]:
    """硬删除：删行 + best-effort 删形象图；回执里的引用数仅作界面提示。

    项目里残留的角色 id 不在这儿改写——视频侧保存与渲染时按「id 已不存在」跳过。
    """
    try:
        references = service.remove(character_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    message = "已删除人物"
    if references:
        message += f"，{references} 个短视频项目仍引用它（保存或渲染时自动跳过）"
    return APIResponse(data=CharacterDeleteOut(id=character_id, references=references), message=message)
