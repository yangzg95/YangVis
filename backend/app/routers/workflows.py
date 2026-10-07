"""工作流接口：定义的编辑与清单、运行进度流、运行现场快照。

这一层是给功能用的流水线配置，不是给用户自由拼图的编排产品：内置流程随代码
seed，这里把它们「有哪些步骤、每步喂什么模板」暴露出来可调，再提供一次运行的
执行与回放。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    RunStatus,
    WorkflowCreate,
    WorkflowItem,
    WorkflowRunDetail,
    WorkflowRunItem,
    WorkflowRunPruneRequest,
    WorkflowRunPruneResult,
    WorkflowRunRequest,
    WorkflowUpdate,
)
from app.services.workflows import (
    WorkflowService,
    follow_run,
    prepare_step_rerun,
    prune_runs,
    rerun_step,
    stream_run,
)
from app.sse import SSE_HEADERS, sse_event, with_heartbeat

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
    run_status: Optional[RunStatus] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[ListResponse[WorkflowRunItem]]:
    """调用方最近的运行，新的在前。不带 ``workflow_id`` 就是跨流程的一趟清单。

    ``subject_kind`` + ``subject_ref`` 换成按业务对象收敛——功能页面上的「运行记录」
    要的是「这份简历跑过哪几次」。两个参数成对出现：整数 id 在各张表上各自编号，
    只给 ref 会把「简历 3」和「deck 3」混进同一页。

    ``status`` 按运行状态筛，``page`` / ``page_size`` 翻页；``total`` 是符合条件的
    总条数，不是当页条数。
    """
    if (subject_kind is None) != (subject_ref is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_kind 与 subject_ref 要一起给",
        )
    subject = None if subject_kind is None else (subject_kind, subject_ref)
    runs, total = service.list_runs(
        workflow_id,
        subject=subject,
        run_status=run_status,
        page=page,
        page_size=page_size,
    )
    # 模型名一次问完整页，逐行去查会把列表接口变成 N+1。
    names = service.run_model_names([run.id for run in runs])
    items = [service.to_run_item(run, names.get(run.id, [])) for run in runs]
    return APIResponse(data=ListResponse(items=items, total=total))


@router.post("/runs/prune", response_model=APIResponse[WorkflowRunPruneResult])
async def prune_run_history(
    payload: WorkflowRunPruneRequest,
    db: Session = Depends(get_db),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowRunPruneResult]:
    """清掉早于保留窗口、又不再是谁的当前状态的运行记录。

    同一个口径在启动时也跑一次（``bootstrap.prune_workflow_runs``）：这里给的是
    「不想等到下次重启」的手动入口，只清调用方自己的记录。
    """
    deleted_runs, deleted_steps = prune_runs(
        db, owner_id=service.owner_id, keep_days=payload.keep_days
    )
    return APIResponse(
        data=WorkflowRunPruneResult(deleted_runs=deleted_runs, deleted_steps=deleted_steps),
        message=f"已清理 {deleted_runs} 次运行",
    )


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


@router.get("/runs/{run_id}/stream")
async def follow_run_stream(
    run_id: int,
    request: Request,
    service: WorkflowService = Depends(get_service),
) -> StreamingResponse:
    """接上一次已经在跑的运行，把之后的进度以 SSE 推回来。

    这条流只观察不执行：进度读的是已经落库的步骤行。功能侧发起的生成（简历分析、
    幻灯片）本来就不在任何流里跑，页面中途打开也要看得见进度，事件形状两边一致。

    先查一次归属：404 得发生在响应变成流之前，那时还有状态码可给。
    """
    try:
        service.get_run(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return _follow_response(service.owner_id, run_id, request)


def _follow_response(owner_id: int, run_id: int, request: Request) -> StreamingResponse:
    """把一次运行的进度以 SSE 观察出去：发起的那条流与中途接上共用这一份。"""

    async def stream() -> AsyncIterator[str]:
        try:
            async for event, data in follow_run(
                owner_id=owner_id,
                run_id=run_id,
                is_disconnected=request.is_disconnected,
            ):
                yield sse_event(event, data)
        except LookupError as exc:
            yield sse_event("error", {"code": status.HTTP_404_NOT_FOUND, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 流不能只停不掉
            logger.exception("following workflow run %s failed", run_id)
            yield sse_event("error", {"code": -1, "message": f"跟随进度失败：{exc}"})

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


@router.post("/runs/{run_id}/steps/{key}/rerun", response_model=APIResponse[WorkflowRunItem])
async def rerun_run_step(
    run_id: int,
    key: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[WorkflowRunItem]:
    """重跑一次运行里的单个步骤，进度用 ``GET /runs/{run_id}/stream`` 接。

    用的是流程**当前定义**里的那一步（prompt 模板只存在定义里，运行行只存渲染结果），
    上游产物的上下文则按当时落库的步骤行重建。跑通这一步不会重写业务行上的产物——
    后处理在功能侧，那里只认整条流程跑完，要新产物请回功能页面重新发起。
    """
    try:
        # 门槛与简历/幻灯片同一处：配置没就绪就别排队。
        service.require_chat_config()
        run = prepare_step_rerun(db, service.owner_id, run_id, key)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BusinessError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    background.add_task(rerun_step, owner_id=service.owner_id, run_id=run_id, key=key)
    return APIResponse(data=service.to_run_item(run), message="已重新开始这一步")


@router.get("", response_model=APIResponse[ListResponse[WorkflowItem]])
async def list_workflows(
    enabled_only: bool = Query(default=False),
    service: WorkflowService = Depends(get_service),
) -> APIResponse[ListResponse[WorkflowItem]]:
    """内置工作流加上调用方自己的，按展示顺序排列。"""
    rows = service.list(enabled_only=enabled_only)
    # 最近一次运行一次查完再逐行分派：交给 to_item 自己查就是每行一次往返。
    last_runs = service.last_runs([row.id for row in rows])
    items = [service.to_item(row, last_runs.get(row.id)) for row in rows]
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
    service: WorkflowService = Depends(get_service),
) -> StreamingResponse:
    """发起一次运行并把进度以 SSE 推回来。

    执行挂在自己的后台任务上，这条响应只是转发：关掉页面只会断掉流，不会把
    运行留在「执行中」——旧实现让响应直接消费执行生成器，断流的取消让它跑不到
    终态落库那一段。

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
        async for event, data in stream_run(service.owner_id, run.id):
            yield sse_event(event, data)

    return StreamingResponse(
        with_heartbeat(stream()),
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
