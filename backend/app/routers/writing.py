"""智能应用 · 创作（写小说 / 写文章）相关接口。

M1 提供手工写作的 CRUD、大纲对账与导出；M2 追加后台大纲生成——接口只建一条
认领作品的运行，模型调用跑在 ``BackgroundTasks`` 里，前端靠轮询状态感知进度，
与幻灯片生成同一形态。M3 的正文生成继续在本文件追加。
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    WritingOutlineGenerate,
    WritingOutlineSave,
    WritingPartCreate,
    WritingPartSave,
    WritingWorkCreate,
    WritingWorkDetail,
    WritingWorkItem,
    WritingWorkUpdate,
)
from app.services.writing import WritingService

logger = logging.getLogger("yangvis.writing")

router = APIRouter(prefix="/studio", tags=["studio"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> WritingService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return WritingService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---- 作品 ---------------------------------------------------------------------


@router.get("/works", response_model=APIResponse[ListResponse[WritingWorkItem]])
def list_works(
    kind: Optional[str] = Query(default=None, max_length=8),
    keyword: Optional[str] = Query(default=None, max_length=200),
    service: WritingService = Depends(get_service),
) -> APIResponse[ListResponse[WritingWorkItem]]:
    works = service.list_works(kind=kind, keyword=keyword)
    items = service.to_items(works)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/works", response_model=APIResponse[WritingWorkDetail])
def create_work(
    payload: WritingWorkCreate,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        work = service.create_work(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(work), message="已创建")


@router.get("/works/{work_id}", response_model=APIResponse[WritingWorkDetail])
def get_work(
    work_id: int,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        work = service.get_work(work_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=service.to_detail(work))


@router.put("/works/{work_id}", response_model=APIResponse[WritingWorkDetail])
def update_work(
    work_id: int,
    payload: WritingWorkUpdate,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        work = service.update_work(work_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(work))


@router.delete("/works/{work_id}", response_model=APIResponse[None])
def delete_work(
    work_id: int,
    service: WritingService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_work(work_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


# ---- 大纲 ---------------------------------------------------------------------


@router.post("/works/{work_id}/outline/generate", response_model=APIResponse[WritingWorkItem])
def generate_outline(
    work_id: int,
    background: BackgroundTasks,
    payload: WritingOutlineGenerate,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkItem]:
    """后台生成大纲：请求里只建运行并返回排队状态，模型调用交给 BackgroundTasks。

    模型未就绪抛 BusinessError（全局处理成 code=4004 的引导信封）；重复触发与
    找不到作品分别走业务信封与 404。
    """
    try:
        work = service.start_outline_generation(
            work_id, payload.requirement, payload.model_config_id
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_outline_task, service.owner_id, work.id)
    return APIResponse(data=service.to_item(work), message="已开始生成大纲")


@router.put("/works/{work_id}/outline", response_model=APIResponse[WritingWorkDetail])
def save_outline(
    work_id: int,
    payload: WritingOutlineSave,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    """整棵大纲保存：服务端清洗 cid/层级，并对账正文块（建新块、删孤儿、排顺序）。"""
    try:
        work = service.save_outline(work_id, [node.model_dump() for node in payload.nodes])
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(work))


# ---- 正文块 -------------------------------------------------------------------


@router.post("/works/{work_id}/parts", response_model=APIResponse[WritingWorkDetail])
def add_part(
    work_id: int,
    payload: WritingPartCreate,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        work, _cid = service.add_part(work_id, payload.title, payload.after_cid)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(work), message="已添加")


@router.put("/works/{work_id}/parts/{cid}", response_model=APIResponse[WritingWorkDetail])
def save_part(
    work_id: int,
    cid: str,
    payload: WritingPartSave,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        service.update_part(work_id, cid, title=payload.title, content=payload.content)
        work = service.get_work(work_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(work))


@router.delete("/works/{work_id}/parts/{cid}", response_model=APIResponse[WritingWorkDetail])
def delete_part(
    work_id: int,
    cid: str,
    service: WritingService = Depends(get_service),
) -> APIResponse[WritingWorkDetail]:
    try:
        work = service.delete_part(work_id, cid)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=service.to_detail(work), message="deleted")


# ---- 导出 ---------------------------------------------------------------------


@router.get("/works/{work_id}/export")
def export_work(
    work_id: int,
    format: str = Query(default="md"),
    service: WritingService = Depends(get_service),
) -> Response:
    """导出 markdown / 纯文本。文件名走 RFC 5987，中文不需要浏览器猜编码。"""
    try:
        work = service.get_work(work_id)
        if format == "txt":
            content = service.export_text(work)
            suffix, media_type = "txt", "text/plain; charset=utf-8"
        else:
            content = service.export_markdown(work)
            suffix, media_type = "md", "text/markdown; charset=utf-8"
    except LookupError as exc:
        raise _not_found(exc) from exc

    name = f"{(work.title or 'writing').strip()[:64] or 'writing'}.{suffix}"
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=content, media_type=media_type, headers=headers)


# ---- 后台任务 ------------------------------------------------------------------


async def _outline_task(owner_id: int, work_id: int) -> None:
    # 函数体内延迟导入：测试会 monkeypatch app.database.SessionLocal，模块顶层
    # import 进来的名字替换不掉（与 slides 路由同一约定）。
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = WritingService(db, owner_id)
        try:
            await service.run_outline_generation(work_id)
        except Exception:  # noqa: BLE001 - run_outline_generation 内部已带堆栈记录
            logger.warning("writing outline task crashed: owner=%s work=%s", owner_id, work_id, exc_info=True)
