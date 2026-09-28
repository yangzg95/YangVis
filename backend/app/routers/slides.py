"""智能办公 · 幻灯片相关接口。"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    SlideDeckDetail,
    SlideDeckItem,
    SlideDeckRegenerate,
    SlideDeckSave,
)
from app.services.chunking import UnsupportedFileType
from app.services.resume import extract_resume_text
from app.services.slides import SlideService
from app.services.slides_store import (
    SlideImageError,
    delete_image,
    delete_image_dir,
    store_image,
)
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)

logger = logging.getLogger("yangvis.slides")

settings = get_settings()

router = APIRouter(prefix="/office/slides", tags=["office"])

# 材料全文的入库上限。喂模型时还会再按 SLIDE_MAX_SOURCE_CHARS 截一次——
# 这里是「别把 MEDIUMTEXT 撑爆」的量级闸门，不是窗口闸门。
_MAX_SOURCE_STORE_CHARS = 200_000


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> SlideService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return SlideService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---- 幻灯片本体 ---------------------------------------------------------------


@router.get("", response_model=APIResponse[ListResponse[SlideDeckItem]])
async def list_decks(
    service: SlideService = Depends(get_service),
) -> APIResponse[ListResponse[SlideDeckItem]]:
    items = [SlideService.to_item(deck) for deck in service.list_decks()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[SlideDeckItem])
async def create_deck(
    request: Request,
    background: BackgroundTasks,
    files: Optional[List[UploadFile]] = File(default=None),
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    requirement: Optional[str] = Form(default=None),
    text: Optional[str] = Form(default=None),
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckItem]:
    """新建一份幻灯片并开始生成。

    文档解析在这里同步完成（PDF/DOCX 是本地计算，很快），解析出的文本与用户
    粘贴的文案合成 ``source_text`` 存进记录；慢的模型调用交给后台任务。
    图片不走这条路 —— 它们是建好之后按 ``/assets`` 逐张上传的素材。
    """
    # 配置门槛放在最前面：没有可用的模型，排队之后只会留下一行永远停在
    # 「生成中」的记录，用户既删不掉也等不到结果。
    service.require_chat_config()

    # 先按 Content-Length 提前拒绝，再有界读取：read() 不设上限会把整个上传体
    # 物化进内存。
    limit = settings.KB_MAX_UPLOAD_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))

    chunks: List[str] = []
    pasted = (text or "").strip()
    if pasted:
        chunks.append(pasted)

    for upload in files or []:
        raw = await read_upload_limited(upload, limit)
        if raw is None:
            return APIResponse(code=-1, message=oversize_message(limit))
        filename = upload.filename or "untitled"
        try:
            extracted = extract_resume_text(filename, raw)
        except UnsupportedFileType as exc:
            return APIResponse(code=-1, message=str(exc))
        if extracted:
            chunks.append(f"=== 文件：{filename} ===\n{extracted}")

    source_text = "\n\n".join(chunks)
    if len(source_text) > _MAX_SOURCE_STORE_CHARS:
        return APIResponse(
            code=-1,
            message=f"材料过长（{len(source_text)} 字符），上限 {_MAX_SOURCE_STORE_CHARS}；"
            "请精简文案或减少文件",
        )

    try:
        deck = service.create_deck(
            title=(title or "").strip(),
            description=description,
            requirement=requirement,
            source_text=source_text,
        )
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))

    background.add_task(_generate_task, service.owner_id, deck.id)
    return APIResponse(data=SlideService.to_item(deck), message="已开始生成")


@router.get("/{deck_id}", response_model=APIResponse[SlideDeckDetail])
async def get_deck(
    deck_id: int,
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckDetail]:
    try:
        deck = service.get_deck(deck_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=service.to_detail(deck))


@router.put("/{deck_id}", response_model=APIResponse[SlideDeckDetail])
async def save_deck(
    deck_id: int,
    payload: SlideDeckSave,
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckDetail]:
    """整包保存（工作台保存、放映页就地改内容都走这里）。

    返回 Detail 而不是 Item：新插入的页面其 ``sid`` 由服务端发放，前端要拿
    规范化后的列表重新对齐自己那份，不能假设发出去的 sid 都活着。
    """
    try:
        deck = service.save_deck(
            deck_id,
            title=payload.title,
            description=payload.description,
            theme=payload.theme.model_dump() if payload.theme else None,
            slides=[page.model_dump() for page in payload.slides] if payload.slides is not None else None,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(deck))


@router.delete("/{deck_id}", response_model=APIResponse[None])
async def delete_deck(
    deck_id: int,
    service: SlideService = Depends(get_service),
) -> APIResponse[None]:
    rel_paths: List[str] = []
    try:
        rel_paths = service.delete_deck(deck_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))

    # 记录已经删了，磁盘上的图片 best-effort 跟着清；失败只留日志，
    # 不能让用户删不掉一份幻灯片。
    for rel_path in rel_paths:
        delete_image(rel_path)
    delete_image_dir(deck_id)
    return APIResponse(data=None, message="deleted")


@router.post("/{deck_id}/generate", response_model=APIResponse[SlideDeckItem])
async def regenerate_deck(
    deck_id: int,
    background: BackgroundTasks,
    payload: SlideDeckRegenerate,
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckItem]:
    """按当时的材料快照重新生成一次，可顺带补一句要求。

    材料本身不变（``source_text`` 是创建时的快照），所以重新生成不需要重传文件。
    """
    service.require_chat_config()
    try:
        deck = service.start_generation(deck_id, payload.requirement)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_generate_task, service.owner_id, deck_id)
    return APIResponse(data=SlideService.to_item(deck), message="已开始生成")


# ---- 预览 / 导出 --------------------------------------------------------------


def _html_response(content: str, *, attachment_name: Optional[str] = None) -> Response:
    headers = {
        # 内容改完就要立刻看得见，不能让浏览器或反代缓存住上一版。
        "Cache-Control": "no-store",
        # 这份文档里跑的是本模块自带的固定脚本，内容全转过义；两道 CSP 头是给
        # 「有人直接把 URL 敲进地址栏」那种场景兜底：不许它加载任何外部脚本、
        # 不发任何请求。
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if attachment_name:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(attachment_name)}"
    return Response(content=content, media_type="text/html; charset=utf-8", headers=headers)


@router.get("/{deck_id}/html")
async def preview_html(
    deck_id: int,
    service: SlideService = Depends(get_service),
) -> Response:
    """渲染好的自包含 HTML 文档，供前端塞进 iframe 预览与放映。

    图片是 ``/uploads`` 下的站内相对路径：``srcdoc`` 文档继承父页面的 base
    URL，所以浏览器会按当前 origin 解析，直接命中那条静态挂载，不需要这里
    代理，也不必带 token。
    """
    try:
        deck = service.get_deck(deck_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return _html_response(service.deck_html(deck))


@router.get("/{deck_id}/export")
async def export_html(
    request: Request,
    deck_id: int,
    service: SlideService = Depends(get_service),
) -> Response:
    """下载同一份 HTML（含导航脚本，双击即可在自己浏览器里放映）。

    图片改成绝对地址：相对路径在 ``file://`` 下解析不出来，导出的单文件打开
    就是一堆破图。除此之外与预览逐字一致——同一份 ``html`` 列。
    """
    try:
        deck = service.get_deck(deck_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    content = service.deck_html(deck)
    origin = str(request.base_url).rstrip("/")
    content = content.replace(
        f'src="{settings.UPLOAD_MEDIA_URL}/', f'src="{origin}{settings.UPLOAD_MEDIA_URL}/'
    )
    name = f"{(deck.title or 'slides').strip()[:64] or 'slides'}.html"
    return _html_response(content, attachment_name=name)


# ---- 配图素材 ------------------------------------------------------------------


@router.post("/{deck_id}/assets", response_model=APIResponse[SlideDeckDetail])
async def upload_asset(
    request: Request,
    deck_id: int,
    file: UploadFile = File(...),
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckDetail]:
    """上传一张配图：按内容魔数判定格式，落到服务器本地磁盘，网页可直接浏览。

    扩展名由魔数决定，客户端报的 ``Content-Type`` 与文件名都不参与路径和类型
    判定（详见 :mod:`app.services.slides_store`）。
    """
    limit = settings.SLIDE_MAX_IMAGE_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))
    try:
        service.get_deck(deck_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))

    try:
        stored = store_image(deck_id, file.filename or "image", raw)
    except SlideImageError as exc:
        return APIResponse(code=-1, message=str(exc))

    try:
        deck = service.add_asset(deck_id, stored)
    except (ValueError, LookupError) as exc:
        # 落库没成功的话，刚才写下的文件就是孤儿，顺手清掉。
        delete_image(stored.rel_path)
        if isinstance(exc, LookupError):
            raise _not_found(exc) from exc
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(deck), message="已上传")


@router.delete("/{deck_id}/assets/{asset_id}", response_model=APIResponse[SlideDeckDetail])
async def delete_asset(
    deck_id: int,
    asset_id: str,
    service: SlideService = Depends(get_service),
) -> APIResponse[SlideDeckDetail]:
    """删一张配图，同时解掉引用它的页面。"""
    rel_path = ""
    try:
        deck, rel_path = service.remove_asset(deck_id, asset_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    if rel_path:
        delete_image(rel_path)
    return APIResponse(data=service.to_detail(deck), message="deleted")


async def _generate_task(owner_id: int, deck_id: int) -> None:
    # 函数体内延迟导入：测试会 monkeypatch app.database.SessionLocal，
    # 模块顶层 import 进来的名字是替换不掉的（与 knowledge/resume 路由同一约定）。
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = SlideService(db, owner_id)
        try:
            await service.run_generation(deck_id)
        except Exception:  # noqa: BLE001 - run_generation 内部已带堆栈记录
            pass
