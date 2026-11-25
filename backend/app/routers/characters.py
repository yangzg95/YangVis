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
    Response,
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
    CharacterTrashItemOut,
    CharacterTrashListOut,
    CharacterUpdateIn,
    CharacterUsageOut,
    CurrentUser,
    ModelPurpose,
)
from app.services.characters.prompts import build_turnaround_prompt
from app.services.characters.service import CharacterService
from app.services.characters.store import ensure_thumbnail, store_character_image
from app.services.media import ark, store
from app.services.media.store import MediaFileError, sniff_media
from app.services.model_config import default_model_config
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)
from app.timeutil import db_now

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


def _out(row, project_count: int = 0) -> CharacterOut:
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
        project_count=project_count,
    )


@router.get("", response_model=APIResponse[CharacterListOut])
def list_characters(
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterListOut]:
    """一页角色 + 筛选后的总数。``q`` 只搜角色名；每条带项目引用数与回收站角标。"""
    rows, total = service.list_characters(keyword=q, limit=limit, offset=offset)
    usage_map = service.project_usage_map([row.id for row in rows])
    return APIResponse(
        data=CharacterListOut(
            items=[_out(row, usage_map.get(row.id, 0)) for row in rows],
            total=total,
            max_per_owner=settings.CHARACTER_MAX_PER_OWNER,
            trash_count=service.trash_count(),
            trash_ttl_seconds=service.trash_ttl_seconds,
        )
    )


@router.get("/trash", response_model=APIResponse[CharacterTrashListOut])
def list_trash_characters(
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterTrashListOut]:
    """回收站一屏：只列撤销窗口内的角色，带距真删的剩余秒数。"""
    rows, total = service.list_trash(keyword=q, limit=limit, offset=offset)
    ttl = service.trash_ttl_seconds
    now = db_now()
    items: list[CharacterTrashItemOut] = []
    for row in rows:
        base = _out(row).model_dump()
        remaining = ttl
        if row.deleted_at is not None:
            remaining = max(0, ttl - int((now - row.deleted_at).total_seconds()))
        items.append(
            CharacterTrashItemOut(
                **base,
                deleted_at=row.deleted_at,
                expires_in_seconds=remaining,
            )
        )
    return APIResponse(
        data=CharacterTrashListOut(items=items, total=total, trash_ttl_seconds=ttl)
    )


@router.post("/batch-restore", response_model=APIResponse[CharacterIdsIn])
def restore_characters(
    body: CharacterIdsIn,
    service: CharacterService = Depends(get_service),
) -> APIResponse[CharacterIdsIn]:
    """撤销删除：把回收期里的角色原样恢复，超额时只放回预算内那批。"""
    restored = service.restore_many(body.ids)
    return APIResponse(
        data=CharacterIdsIn(ids=body.ids[:restored]),
        message=f"已恢复 {restored} 个人物" if restored else "没有可恢复的人物，可能已过撤销期限",
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


@router.post("/portrait-preview")
async def preview_portrait(
    request: Request,
    prompt: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """AI 生成三视图形象：可带参考图的同步图生图，二进制直接回，不落盘不写库。

    与短视频的 TTS 单句试听同口径——真计费、只由界面按钮显式触发、no-store：
    用户可能连点几次挑一张，只有点保存时那张才随建档/换图 multipart 落盘，
    取消或换图不留任何孤儿文件。参考图与描述至少给一个：有参考图时走图生图，
    data URL 内联进请求体（与首帧渲染的参考图同一条通道），描述降为可空的补充
    要求；没参考图时退化为纯文生图，描述必填。准入门与渲染作业一致：默认图片
    模型必须存在且已通过连通性测试，否则凭证错误要等上游报错才暴露，白烧抵扣。
    """
    config = default_model_config(db, user.user_id, ModelPurpose.IMAGE)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="还没有配置用途为「图片」的默认模型：先在「模型与集成」里添加并测通",
        )
    if not config.last_test_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="图片模型还没通过连通性测试：先在「模型与集成」里测通再生成",
        )

    description = (prompt or "").strip()
    reference_urls: list[str] = []
    if file is not None and file.filename:
        # 参考图与形象图同一道闸门：先挡请求体，再按文件头认型，视频与非媒体直接拒。
        limit = settings.MATERIAL_MAX_IMAGE_BYTES
        if content_length_exceeds(request.headers.get("content-length"), limit):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=oversize_message(limit)
            )
        raw_upload = await read_upload_limited(file, limit)
        if raw_upload is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=oversize_message(limit)
            )
        try:
            ref_mime, _ext = sniff_media(raw_upload)
        except MediaFileError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"参考图无法识别：{exc}"
            ) from exc
        if not ref_mime.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="参考图只支持 PNG / JPEG / GIF / WebP / BMP 图片",
            )
        reference_urls.append(ark.image_data_url(raw_upload, ref_mime))

    if not reference_urls and not description:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请上传参考图或填写角色描述，至少给一个生成依据",
        )

    full_prompt = build_turnaround_prompt(
        description, has_reference=bool(reference_urls)
    )
    try:
        result = await ark.generate_image(
            config, prompt=full_prompt, size="2K", images=reference_urls
        )
        raw = await ark.download(result.url, max_bytes=settings.MEDIA_IMAGE_MAX_BYTES)
    except ark.ArkError as exc:
        # 上游错误已经是中文人话（含内容审核、限流、404 型号错误等），原样交给界面。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.friendly()
        ) from exc

    # 防御一道：上游理论上只回图片，但落进编辑器之前仍按文件头认一次，
    # 别让一份非图片字节伪装成形象图进 multipart。
    try:
        mime, _ext = sniff_media(raw)
    except MediaFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上游返回的不是图片内容：{exc}",
        ) from exc
    if not mime.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上游返回的不是图片内容，换个描述再试一次",
        )
    logger.info(
        "character portrait preview generated: owner=%s model=%s reference=%s bytes=%d",
        user.user_id,
        config.model_name,
        bool(reference_urls),
        len(raw),
    )
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "no-store"})


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
    """移进回收期：行与形象图都保留，窗口内可在回收站撤销；回执引用数仅作界面提示。

    项目里残留的角色 id 不在这儿改写——视频侧保存与渲染时按「id 已不存在」跳过。
    """
    try:
        references = service.remove(character_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    message = f"已移入回收站，{service.trash_ttl_seconds // 60} 分钟内可撤销"
    if references:
        message += f"；{references} 个短视频项目仍引用它（保存或渲染时自动跳过）"
    return APIResponse(data=CharacterDeleteOut(id=character_id, references=references), message=message)
