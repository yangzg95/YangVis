"""智能办公 · 短视频相关接口。

两阶段在这份接口上分得很清楚，因为它们的**执行体**不一样：

* 脚本（想法 → 创作卡 → 分镜）走工作流引擎，请求里建一条认领本项目的运行、
  ``BackgroundTasks`` 跑完它，前端轮询详情接口看进度——与幻灯片、简历同一形态；
* 渲染（镜头 → 首帧图）只往 ``media_job`` 入队，执行体是 lifespan 里那个常驻循环，
  接口既不等待也不占用请求级任务。所以这里没有任何「等结果」的接口：详情接口
  把每镜最近一次作业的状态一起带回来，前端刷新它就是进度。

媒体调用的门槛放在最前面（``require_image_config``）：没通过连通性测试就入队，
只会留下一批停在「排队中」、白烧额度的作业。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    MediaCapacityOut,
    VideoProjectCreate,
    VideoProjectDetail,
    VideoProjectItem,
    VideoProjectUpdate,
    VideoRenderRequest,
    VideoShotsSave,
)
from app.services.media import store
from app.services.videos import VideoService

logger = logging.getLogger("yangvis.videos")

settings = get_settings()

router = APIRouter(prefix="/office/videos", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> VideoService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return VideoService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---- 项目 -------------------------------------------------------------------


@router.get("", response_model=APIResponse[ListResponse[VideoProjectItem]])
def list_projects(
    service: VideoService = Depends(get_service),
) -> APIResponse[ListResponse[VideoProjectItem]]:
    items = service.to_items(service.list_projects())
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get("/capacity", response_model=APIResponse[MediaCapacityOut])
def render_capacity(
    service: VideoService = Depends(get_service),
) -> APIResponse[MediaCapacityOut]:
    """渲染前那屏确认要的数字：各类作业在途几个、上限几个。

    单独一条接口而不是塞进详情：确认屏在选镜头之前就要显示，那时还没有「这次
    要发几条」的上下文。
    """
    return APIResponse(
        data=MediaCapacityOut(
            jobs=service.capacity(), image_transport=settings.MEDIA_IMAGE_TRANSPORT
        )
    )


@router.post("", response_model=APIResponse[VideoProjectItem])
def create_project(
    background: BackgroundTasks,
    payload: VideoProjectCreate,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectItem]:
    """新建一个短视频项目并开始生成脚本。

    对话模型和想法都在这里同步校验，模型调用交给后台任务：一次两阶段的流程要
    几十秒，留给 ``BackgroundTasks`` 才是能跑完的地方。
    """
    service.require_chat_config()
    try:
        project = service.create_project(
            title=payload.title,
            platform=payload.platform,
            idea=payload.idea,
            duration_s=payload.duration_s,
            requirement=payload.requirement,
            style=payload.style.model_dump() if payload.style else None,
            params=payload.params.model_dump() if payload.params else None,
        )
    except ValueError as exc:
        # MediaJobError 也在这一档：它是 ValueError 的子类，媒体门槛的拒绝与
        # 参数校验的拒绝在界面上是同一种东西。
        return APIResponse(code=-1, message=str(exc))

    background.add_task(_script_task, service.owner_id, project.id)
    return APIResponse(data=service.to_item(project), message="已开始生成脚本")


@router.get("/{project_id}", response_model=APIResponse[VideoProjectDetail])
def get_project(
    project_id: int,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    """工作台要的完整数据：创作卡 + 分镜 + 参数 + 每镜最近一次作业的现状。

    前端轮询的就是这一条：渲染进度在作业行上，详情把它们一起带回来，就不必再为
    「哪几镜出片了」发明一个接口。
    """
    try:
        return APIResponse(data=service.to_detail(service.get_project(project_id)))
    except LookupError as exc:
        raise _not_found(exc) from exc


@router.put("/{project_id}", response_model=APIResponse[VideoProjectDetail])
def save_project(
    project_id: int,
    payload: VideoProjectUpdate,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    try:
        project = service.update_project(
            project_id,
            title=payload.title,
            platform=payload.platform,
            idea=payload.idea,
            requirement=payload.requirement,
            brief=payload.brief.model_dump() if payload.brief is not None else None,
            script=payload.script,
            style=payload.style.model_dump() if payload.style is not None else None,
            params=payload.params.model_dump() if payload.params is not None else None,
            publish=payload.publish.model_dump() if payload.publish is not None else None,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(project))


@router.delete("/{project_id}", response_model=APIResponse[None])
def delete_project(
    project_id: int,
    service: VideoService = Depends(get_service),
) -> APIResponse[None]:
    try:
        deleted_id = service.delete_project(project_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))

    # 记录已经删了，磁盘上那棵目录树 best-effort 跟着清：失败只留日志，
    # 不能让用户删不掉一个项目。
    store.delete_project_dir(deleted_id)
    return APIResponse(data=None, message="deleted")


# ---- 脚本阶段 ---------------------------------------------------------------


@router.post("/{project_id}/script", response_model=APIResponse[VideoProjectItem])
def regenerate_script(
    project_id: int,
    background: BackgroundTasks,
    payload: VideoProjectUpdate,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectItem]:
    """重新生成创作卡与分镜。

    只收 ``requirement`` 与 ``brief.duration_s`` 这两个会影响输出的字段：其余改动
    走整包保存，重跑一次脚本不该顺手把用户刚改过的分镜覆盖掉却没留下任何要求。
    """
    service.require_chat_config()
    try:
        project = service.start_script(
            project_id,
            requirement=payload.requirement,
            duration_s=payload.brief.duration_s if payload.brief else None,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_script_task, service.owner_id, project.id)
    return APIResponse(data=service.to_item(project), message="已开始生成脚本")


@router.put("/{project_id}/shots", response_model=APIResponse[VideoProjectDetail])
def save_shots(
    project_id: int,
    payload: VideoShotsSave,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    """整批保存镜头：数组顺序即镜头顺序，``start_s`` 由时长累加重算。

    返回 Detail 而不是「保存成功」：新插入镜头的 id 与重算过的时间轴都在服务端，
    前端要拿回来的那份对齐自己，不能假设发出去的 id 都活着。
    """
    try:
        project = service.save_shots(
            project_id, [shot.model_dump() for shot in payload.shots]
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(project))


# ---- 渲染阶段 ---------------------------------------------------------------


@router.post("/{project_id}/frames", response_model=APIResponse[VideoProjectDetail])
def render_frames(
    project_id: int,
    payload: VideoRenderRequest,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    """批量渲染首帧：每个选中镜头一条图片作业，``shot_ids`` 空就是全部。

    这里不等待也不返回作业列表：入队成功与否看详情接口里的 ``image_job.status``，
    而「排队中」本来就是这个动作唯一的诚实答案——真正出片要几十秒到几分钟。
    """
    try:
        service.render_frames(project_id, payload.shot_ids)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(
        data=service.to_detail(service.get_project(project_id)),
        message="已开始渲染首帧，出片需要几十秒到几分钟",
    )


@router.post("/{project_id}/cover", response_model=APIResponse[VideoProjectDetail])
def render_cover(
    project_id: int,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    try:
        service.render_cover(project_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(
        data=service.to_detail(service.get_project(project_id)), message="已开始生成封面"
    )


@router.post(
    "/{project_id}/jobs/{job_id}/cancel", response_model=APIResponse[VideoProjectDetail]
)
async def cancel_job(
    project_id: int,
    job_id: int,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    """取消一条作业。已完成的取消不掉（那是「重做」，不是「取消」）。"""
    try:
        service.get_project(project_id)
        await service.cancel_job(job_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(service.get_project(project_id)))


@router.post(
    "/{project_id}/shots/{shot_id}/to-material",
    response_model=APIResponse[VideoProjectDetail],
)
def save_frame_to_material(
    project_id: int,
    shot_id: int,
    service: VideoService = Depends(get_service),
) -> APIResponse[VideoProjectDetail]:
    """把这一镜渲出来的首帧复制进素材库，从此跨项目复用。

    这里不带任何正文：素材的名字与归属都由服务端定（见服务层），而复制过去的字节与
    产物是两份文件，删项目不会带走库里那条。``MediaFileError`` 是 ``ValueError`` 的
    子类，所以产物读不出来、素材超限、库满三档拒绝都落在同一个答复里。
    """
    try:
        service.save_frame_to_material(project_id, shot_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(
        data=service.to_detail(service.get_project(project_id)), message="已存进素材库"
    )


async def _script_task(owner_id: int, project_id: int) -> None:
    """跑完发起时建好的那条脚本运行。

    函数体内延迟导入 ``SessionLocal``：测试会 monkeypatch 它，模块顶层 import
    进来的名字是替换不掉的（与 knowledge / resume / slides 路由同一约定）。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = VideoService(db, owner_id)
        try:
            await service.run_script(project_id)
        except Exception:  # noqa: BLE001 - run_script 内部已带堆栈记录
            logger.exception("video script task crashed: owner=%s project=%s", owner_id, project_id)
