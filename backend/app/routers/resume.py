"""智能办公 · 简历相关接口。"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    ResumeCompareRequest,
    ResumeComparisonDetail,
    ResumeComparisonItem,
    ResumeDetail,
    ResumeItem,
    ResumeToolkitCreate,
    ResumeToolkitDetail,
    ResumeToolkitItem,
    ResumeUpdate,
)
from app.services.chunking import UnsupportedFileType
from app.services.netdisk import NetdiskService
from app.services.resume import ResumeService, extract_resume_text

logger = logging.getLogger("yangvis.resume")

settings = get_settings()

router = APIRouter(prefix="/office", tags=["office"])

# 原件下载代理的读块大小。
_DOWNLOAD_CHUNK = 256 * 1024


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ResumeService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return ResumeService(db, user.user_id)


def get_netdisk_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> NetdiskService:
    # get_db 在一次请求内被 FastAPI 缓存，这里拿到的是与 ResumeService
    # 相同的 session。
    return NetdiskService(db, user.user_id)


# ---- 简历 -------------------------------------------------------------------

@router.get("/resumes", response_model=APIResponse[ListResponse[ResumeItem]])
async def list_resumes(
    service: ResumeService = Depends(get_service),
) -> APIResponse[ListResponse[ResumeItem]]:
    items = [ResumeService.to_item(resume) for resume in service.list_resumes()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/resumes", response_model=APIResponse[ResumeItem])
async def upload_resume(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    service: ResumeService = Depends(get_service),
    netdisk: NetdiskService = Depends(get_netdisk_service),
) -> APIResponse[ResumeItem]:
    """保存一份简历。文本抽取在这里同步完成（PDF/DOCX 解析是本地计算，
    很快）；真正慢的 AI 分析由用户之后手动触发。

    用户已绑定网盘时，原文件顺带同步一份过去。同步失败不阻塞保存——
    行里的网盘两列留 NULL 就是「未同步」的标记，之后也下载不了原件。"""
    raw = await file.read()
    if len(raw) > settings.KB_MAX_UPLOAD_BYTES:
        limit_mb = settings.KB_MAX_UPLOAD_BYTES / 1024 / 1024
        return APIResponse(code=-1, message=f"文件过大，上限 {limit_mb:.0f} MB")

    filename = file.filename or "untitled.pdf"
    try:
        text = extract_resume_text(filename, raw)
    except UnsupportedFileType as exc:
        return APIResponse(code=-1, message=str(exc))

    if not text:
        return APIResponse(code=-1, message="文件内容为空或无法解析出文本")

    resume = service.create_resume(
        title=(title or "").strip() or filename.rsplit(".", 1)[0],
        description=(description or "").strip() or None,
        filename=filename,
        mime=file.content_type,
        size=len(raw),
        content=text,
    )

    if netdisk.is_bound():
        try:
            fs_id, path = await netdisk.upload_file(f"resumes/{resume.id}_{filename}", raw)
            service.mark_netdisk(resume, fs_id, path)
            logger.info("resume %s synced to netdisk (%s)", resume.id, path)
        except Exception as exc:  # noqa: BLE001 - 网盘只是备份通道
            logger.warning("resume %s netdisk sync failed: %s", resume.id, exc)

    return APIResponse(data=ResumeService.to_item(resume))


@router.get("/resumes/{resume_id}", response_model=APIResponse[ResumeDetail])
async def get_resume(
    resume_id: int,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeDetail]:
    try:
        resume = service.get_resume(resume_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(resume))


@router.put("/resumes/{resume_id}", response_model=APIResponse[ResumeItem])
async def update_resume(
    resume_id: int,
    payload: ResumeUpdate,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeItem]:
    try:
        resume = service.update_resume(
            resume_id, title=payload.title, description=payload.description
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=ResumeService.to_item(resume))


@router.delete("/resumes/{resume_id}", response_model=APIResponse[None])
async def delete_resume(
    resume_id: int,
    service: ResumeService = Depends(get_service),
    netdisk: NetdiskService = Depends(get_netdisk_service),
) -> APIResponse[None]:
    try:
        resume = service.get_resume(resume_id)
        service.delete_resume(resume_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))

    # 联动删掉网盘里的原件。best-effort：网盘挂了不该让简历删不掉。
    if resume.netdisk_path and netdisk.is_bound():
        await netdisk.delete_file(resume.netdisk_path)
    return APIResponse(data=None, message="deleted")


@router.get("/resumes/{resume_id}/download")
async def download_resume(
    resume_id: int,
    service: ResumeService = Depends(get_service),
    netdisk: NetdiskService = Depends(get_netdisk_service),
) -> StreamingResponse:
    """从用户自己的网盘代理下载简历原件。

    刻意做成代理流而不是 302 到 dlink：dlink 必须拼上 access_token 才有
    效，一旦重定向，token 就会留在浏览器历史和沿途日志里。"""
    try:
        resume = service.get_resume(resume_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if resume.netdisk_fs_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该简历未同步到网盘，没有可下载的原件",
        )

    url, headers = await netdisk.open_download(resume.netdisk_fs_id)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True
    )
    try:
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("netdisk proxy download of resume %s failed to connect", resume_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"连接网盘下载服务失败：{exc}",
        ) from exc
    if response.status_code != 200:
        await response.aclose()
        await client.aclose()
        logger.warning(
            "netdisk proxy download of resume %s got http %s", resume_id, response.status_code
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"网盘返回了 HTTP {response.status_code}，下载失败",
        )

    async def _stream():
        try:
            async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK):
                yield chunk
        except httpx.HTTPError:
            # 中途断流：客户端只收到半截文件，原因必须留下来。
            logger.warning("netdisk proxy download of resume %s broke mid-stream", resume_id)
            raise
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        media_type=resume.mime or "application/octet-stream",
        headers={
            # RFC 5987：前端 utils/download.ts 按这个约定解析文件名。
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(resume.filename)}"
            )
        },
    )


@router.post("/resumes/{resume_id}/analyze", response_model=APIResponse[ResumeItem])
async def analyze_resume(
    resume_id: int,
    background: BackgroundTasks,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeItem]:
    """触发 AI 分析。先做配置检查，让用户在排队之前就知道还差什么。"""
    service.require_chat_config()
    try:
        resume = service.start_analysis(resume_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_analyze_task, service.owner_id, resume_id)
    return APIResponse(data=ResumeService.to_item(resume), message="已开始分析")


async def _analyze_task(owner_id: int, resume_id: int) -> None:
    # 函数体内延迟导入：测试会 monkeypatch app.database.SessionLocal，
    # 模块顶层 import 进来的名字是替换不掉的（与 knowledge 路由同一约定）。
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = ResumeService(db, owner_id)
        try:
            await service.run_analysis(resume_id)
        except Exception:  # noqa: BLE001 - run_analysis 内部已带堆栈记录
            pass


# ---- 简历对比 ---------------------------------------------------------------

@router.post("/comparisons", response_model=APIResponse[ResumeComparisonItem])
async def create_comparison(
    payload: ResumeCompareRequest,
    background: BackgroundTasks,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeComparisonItem]:
    service.require_chat_config()
    try:
        comparison = service.create_comparison(payload.resume_ids, payload.title)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_compare_task, service.owner_id, comparison.id)
    return APIResponse(data=service.to_comparison_item(comparison), message="已开始对比分析")


@router.get("/comparisons", response_model=APIResponse[ListResponse[ResumeComparisonItem]])
async def list_comparisons(
    service: ResumeService = Depends(get_service),
) -> APIResponse[ListResponse[ResumeComparisonItem]]:
    items = [service.to_comparison_item(item) for item in service.list_comparisons()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get("/comparisons/{comparison_id}", response_model=APIResponse[ResumeComparisonDetail])
async def get_comparison(
    comparison_id: int,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeComparisonDetail]:
    try:
        comparison = service.get_comparison(comparison_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_comparison_detail(comparison))


@router.delete("/comparisons/{comparison_id}", response_model=APIResponse[None])
async def delete_comparison(
    comparison_id: int,
    service: ResumeService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_comparison(comparison_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


async def _compare_task(owner_id: int, comparison_id: int) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = ResumeService(db, owner_id)
        try:
            await service.run_comparison(comparison_id)
        except Exception:  # noqa: BLE001 - 同上
            pass


# ---- 求职助手（toolkit）-------------------------------------------------------

@router.post("/toolkit", response_model=APIResponse[ResumeToolkitItem])
async def create_toolkit_task(
    payload: ResumeToolkitCreate,
    background: BackgroundTasks,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeToolkitItem]:
    """发起一次求职助手生成（简历优化 / 匹配审计 / 面试准备等 7 类）。"""
    service.require_chat_config()
    try:
        task = service.create_toolkit_task(payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_toolkit_task, service.owner_id, task.id)
    return APIResponse(data=ResumeService.to_toolkit_item(task), message="已开始生成")


@router.get("/toolkit", response_model=APIResponse[ListResponse[ResumeToolkitItem]])
async def list_toolkit_tasks(
    service: ResumeService = Depends(get_service),
) -> APIResponse[ListResponse[ResumeToolkitItem]]:
    items = [ResumeService.to_toolkit_item(task) for task in service.list_toolkit_tasks()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get("/toolkit/{task_id}", response_model=APIResponse[ResumeToolkitDetail])
async def get_toolkit_task(
    task_id: int,
    service: ResumeService = Depends(get_service),
) -> APIResponse[ResumeToolkitDetail]:
    try:
        task = service.get_toolkit_task(task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_toolkit_detail(task))


@router.delete("/toolkit/{task_id}", response_model=APIResponse[None])
async def delete_toolkit_task(
    task_id: int,
    service: ResumeService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_toolkit_task(task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


async def _toolkit_task(owner_id: int, task_id: int) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = ResumeService(db, owner_id)
        try:
            await service.run_toolkit_task(task_id)
        except Exception:  # noqa: BLE001 - 同上
            pass
