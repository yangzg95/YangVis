"""工作流接口：定义的编辑与清单、运行进度流、运行现场快照。

这一层是给功能用的流水线配置，不是给用户自由拼图的编排产品：内置流程随代码
seed，这里把它们「有哪些步骤、每步喂什么模板」暴露出来可调，再提供一次运行的
执行与回放。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    WorkflowCreate,
    WorkflowItem,
    WorkflowRunDetail,
    WorkflowRunItem,
    WorkflowRunRequest,
    WorkflowUpdate,
)
from app.services.workflows import WorkflowService, execute_run
from app.sse import SSE_HEADERS, sse_event

logger = logging.getLogger("yangvis.workflows")

router = APIRouter(prefix="/workflows", tags=["workflows"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> WorkflowService:
    return WorkflowService(db, user.user_id)


# ---- 定义 ------------------------------------------------------------------

# /runs 先声明：FastAPI 按注册顺序匹配，写在 /{workflow_id} 之后就永远查不到了。
@router.get("/runs", response_model=APIResponse[ListResponse[WorkflowRunItem]])
async def list_runs(
    workflow_id: Optional[int] = Query(default=None),
    subject_kind: Optional[str] = Query(default=None),
    subject_ref: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[ListResponse[WorkflowRunItem]]:
    """调用方最近的运行，新的在前。不带 ``workflow_id`` 就是跨流程的一趟清单。

    ``subject_kind`` + ``subject_ref`` 换成按业务对象收敛——功能页面上的「运行记录」
    要的是「这份简历跑过哪几次」。两个参数成对出现：整数 id 在各张表上各自编号，
    只给 ref 会把「简历 3」和「deck 3」混进同一页。
    """
    if (subject_kind is None) != (subject_ref is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_kind 与 subject_ref 要一起给",
        )
    subject = None if subject_kind is None else (subject_kind, subject_ref)
    items = [
        service.to_run_item(run)
        for run in service.list_runs(workflow_id, limit, subject=subject)
    ]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get("/runs/{run_id}", response_model=APIResponse[WorkflowRunDetail])
async def get_run(
    run_id: int,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowRunDetail]:
    """一次运行的现场：输入快照加每一步的 prompt、输出与耗时。

    SSE 只是进度通知，正文一律从这里按步取——换设备、中途断开，都能看到跑到哪
    一步、那一步收到了什么。
    """
    try:
        run = service.get_run(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_run_detail(run))


@router.get("", response_model=APIResponse[ListResponse[WorkflowItem]])
async def list_workflows(
    enabled_only: bool = Query(default=False),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[ListResponse[WorkflowItem]]:
    """内置工作流加上调用方自己的，按展示顺序排列。"""
    items = [service.to_item(row) for row in service.list(enabled_only=enabled_only)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get("/{workflow_id}", response_model=APIResponse[WorkflowItem])
async def get_workflow(
    workflow_id: int,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowItem]:
    try:
        row = service.get(workflow_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_item(row))


# ---- 定义编辑 --------------------------------------------------------------

# 内置流程不可直接改（改了下次启动就被代码里的定义覆盖），要改就复制一份：
# 这条规则与智能体完全一致，界面上的说法也照搬。

@router.post("", response_model=APIResponse[WorkflowItem])
async def create_workflow(
    payload: WorkflowCreate,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowItem]:
    try:
        row = service.create(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(row))


@router.put("/{workflow_id}", response_model=APIResponse[WorkflowItem])
async def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdate,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowItem]:
    """整份替换定义（含步骤列表与输入变量声明）。

    不做补丁式更新：步骤之间是相互约束的，逐字段改没法判断改完还合不合法，
    而编辑器的表单本来就是一次提交整份。
    """
    try:
        row = service.update(workflow_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        return APIResponse(code=-1, message=str(exc))
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(row))


@router.delete("/{workflow_id}", response_model=APIResponse[None])
async def delete_workflow(
    workflow_id: int,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(workflow_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


@router.post("/{workflow_id}/duplicate", response_model=APIResponse[WorkflowItem])
async def duplicate_workflow(
    workflow_id: int,
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowItem]:
    """把一条工作流复制到调用方名下；这是定制内置流程的方式。"""
    try:
        row = service.duplicate(workflow_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_item(row))


# ---- 运行 ------------------------------------------------------------------

@router.post("/{workflow_id}/runs/stream")
async def run_workflow_stream(
    workflow_id: int,
    payload: WorkflowRunRequest,
    request: Request,
    service: WorkflowService = Depends(get_service),
) -> StreamingResponse:
    """发起一次运行并把进度以 SSE 推回来。

    与对话的 ``/chat/completions`` 同一条约定：响应是流，没有单一 JSON 体可套通
    用信封，所以错误也是 ``error`` 事件（这时 200 和它的头多半已经发出去了）。
    """
    try:
        service.require_chat_config()
        run = service.create_run(workflow_id, payload.inputs)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BusinessError as exc:
        return _error_stream(exc.code, exc.msg)
    except ValueError as exc:
        return _error_stream(-1, str(exc))

    async def stream() -> AsyncIterator[str]:
        try:
            async for event, data in execute_run(
                owner_id=service.owner_id,
                run_id=run.id,
                is_disconnected=request.is_disconnected,
            ):
                yield sse_event(event, data)
        except BusinessError as exc:
            yield sse_event("error", {"code": exc.code, "message": exc.msg})
        except Exception as exc:  # noqa: BLE001 - 流不能只停不掉
            logger.exception("workflow run %s failed to start streaming", run.id)
            yield sse_event("error", {"code": -1, "message": f"工作流执行失败：{exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


def _error_stream(code: int, message: str) -> StreamingResponse:
    """还没开始流就失败，也用事件协议回：客户端只需解析一种流。"""

    async def stream() -> AsyncIterator[str]:
        yield sse_event("error", {"code": code, "message": message})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )
