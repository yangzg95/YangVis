"""运行的两条流：脱离响应把运行跑完，以及中途接上正在跑的运行。

:func:`stream_run` 把执行挂在自己的 ``asyncio`` 任务上，SSE 只做转发。旧实现让
响应直接消费执行生成器，客户端一断流，取消就打在建到一半的生成器上——运行行的
终态没人写，界面永远「执行中」。``_DETACHED_RUNS`` 不是多余的：asyncio 对任务只
持弱引用，没人 await 的分离任务可能跑到一半就被回收。

:func:`follow_run` 反过来一个东西都不执行，只读 ``workflow_run_step`` 里已经落库
的事实：先回放现状，再按 ``FOLLOW_POLL_SECONDS`` 推之后的变化，事件名与载荷形状跟
:func:`app.services.workflows.exec.execute_run` 一致，客户端不用为「跟着跑」和
「中途接上」写两套解析。能这么分，靠的是执行器每一步即时提交。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessError
from app.models.entities import ModelConfig, WorkflowRun, WorkflowRunStep, WorkflowStep
from app.services.agent_run import model_error_text
from app.services.agents import AgentService
from app.services.workflows.exec import execute_run
from app.services.workflows.runs import settle_run_status
from app.services.workflows import (
    RUN_PENDING,
    RUN_RUNNING,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
)

logger = logging.getLogger("yangvis.workflows")

# 跟随流多久读一次运行表。步骤状态本来就是每步一提交，这里只是把它读出来；
# 一次模型调用要几秒到几十秒，1.2 秒够跟得上，也不会让读变成压力。
FOLLOW_POLL_SECONDS = 1.2


# ---- 脱离响应执行运行 --------------------------------------------------------

# asyncio 只对任务持弱引用：没人 await 的分离任务可能跑到一半就被回收，这里吊着它。
_DETACHED_RUNS: set[asyncio.Task] = set()

# 转发队列的收尾哨兵：执行任务结束（跑完或炸完）后流就到头了。
_RUN_STREAM_END = object()


async def stream_run(
    owner_id: int, run_id: int
) -> AsyncIterator[Tuple[str, object]]:
    """执行一次运行并把事件转发出去，但执行挂在**自己的任务**上。

    旧实现让 SSE 响应直接消费执行生成器，客户端一断流，取消打在建到一半的
    生成器上——步骤是各自的任务能自己跑完，运行行的终态却没人写了，界面上
    永远「执行中」。现在断流只是这条转发流停了：后台任务照常跑到底、照常
    收尾，进度回头用 :func:`follow_run` 接。

    执行体自己没兜住的异常（多半是第一步之前的模型门槛）在这里落终态，
    运行不会停在跑一半。
    """
    from app.database import SessionLocal

    queue: asyncio.Queue = asyncio.Queue()

    async def _execute() -> None:
        try:
            async for event, payload in execute_run(owner_id=owner_id, run_id=run_id):
                await queue.put((event, payload))
        except BusinessError as exc:
            logger.info("workflow run %s refused before start: %s", run_id, exc.msg)
            message = exc.msg
            code = exc.code
            with SessionLocal() as db:
                settle_run_status(db, owner_id, run_id, error=message)
            await queue.put(("error", {"code": code, "message": message}))
        except Exception as exc:  # noqa: BLE001 - 不能让运行无声地停在跑一半
            logger.exception("workflow run %s crashed", run_id)
            message = model_error_text(exc)
            with SessionLocal() as db:
                settle_run_status(db, owner_id, run_id, error=message)
            await queue.put(("error", {"code": -1, "message": f"工作流执行失败：{message}"}))
        finally:
            await queue.put(_RUN_STREAM_END)

    task = asyncio.create_task(_execute())
    _DETACHED_RUNS.add(task)
    task.add_done_callback(_DETACHED_RUNS.discard)

    while True:
        item = await queue.get()
        if item is _RUN_STREAM_END:
            return
        yield item


# ---- 中途接上正在跑的运行 ----------------------------------------------------

# 步骤的这些取值算「跑完了」：剩下的都是进行中。
_STEP_SETTLED = (STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED)


def _follow_state(
    db: Session, owner_id: int, run_id: int
) -> Tuple[Optional[dict], Dict[str, dict]]:
    """读一次运行的现状：运行行加每一步的状态摘要（不含正文）。

    开头先 ``commit()`` 换事务——写这些行的是**另一条连接**（后台任务或另一个
    worker），留在旧事务里读，永远读不到它的提交。
    """
    db.commit()
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
        )
    )
    if run is None:
        return None, {}
    # 模型名长在配置上，步骤行只存 id：口径与 ``step_models`` 一致，配置被删掉的
    # 历史行这里就是 None。
    steps = {
        row.key: {
            "status": row.status,
            "group_key": row.group_key,
            "agent_slug": row.agent_slug,
            "error": row.error_msg,
            "output_chars": len(row.output or ""),
            "elapsed_ms": row.elapsed_ms,
            "model_name": model_name,
        }
        for row, model_name in db.execute(
            select(WorkflowRunStep, ModelConfig.model_name)
            .outerjoin(ModelConfig, ModelConfig.id == WorkflowRunStep.model_config_id)
            .where(
                WorkflowRunStep.owner_id == owner_id, WorkflowRunStep.run_id == run_id
            )
            .order_by(WorkflowRunStep.sort_order, WorkflowRunStep.id)
        ).all()
    }
    state = {
        "run_id": run.id,
        "workflow_id": run.workflow_id,
        "workflow_slug": run.workflow_slug,
        "status": run.status,
        "error": run.error_msg,
        "elapsed_ms": run.elapsed_ms,
    }
    return state, steps


async def follow_run(
    *,
    owner_id: int,
    run_id: int,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    poll_seconds: float = FOLLOW_POLL_SECONDS,
) -> AsyncIterator[Tuple[str, object]]:
    """接上一次已经在跑的运行：先回放现状，再把之后的变化推出去。

    发起它的那条流不保证还活着——页面会关、会换设备，功能侧的生成（简历分析、
    幻灯片）本来就不在任何流里跑。这里不执行任何东西，只读 ``workflow_run_step``
    里已经落库的事实，事件名与载荷形状跟 :func:`execute_run` 一致，所以客户端
    不用为「跟着跑」和「中途接上」写两套解析。

    运行进终态就收流；``is_disconnected`` 只管关掉这条观察的流，运行照跑。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        state, steps = _follow_state(db, owner_id, run_id)
        if state is None:
            raise LookupError("运行记录不存在")

        definitions = {
            step.key: step
            for step in db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_id == state["workflow_id"])
            ).all()
        }
        agent_names = {
            agent.slug: agent.name for agent in AgentService(db, owner_id).list()
        }
        # 定义可能在这之后被改过（内置流程每次启动都就地刷新），步骤行才是
        # 当时的事实；两者都有，定义只补上「这一步标不标可选、要什么格式」。
        yield "run_start", {
            "run_id": state["run_id"],
            "workflow_slug": state["workflow_slug"],
            "status": state["status"],
            "steps": [
                {
                    "key": key,
                    "group_key": info["group_key"],
                    "agent_slug": info["agent_slug"],
                    "agent_name": agent_names.get(info["agent_slug"]),
                    "output_kind": definitions[key].output_kind if key in definitions else None,
                    "optional": definitions[key].optional if key in definitions else False,
                    "status": info["status"],
                    "model_name": info["model_name"],
                }
                for key, info in steps.items()
            ],
        }

        seen = {key: info["status"] for key, info in steps.items()}
        while state["status"] in (RUN_PENDING, RUN_RUNNING):
            await asyncio.sleep(poll_seconds)
            if is_disconnected is not None and await is_disconnected():
                return
            state, steps = _follow_state(db, owner_id, run_id)
            if state is None:
                # 记录被清理掉了：接着转圈只会让界面卡在「执行中」。
                raise LookupError("运行记录不存在")
            if state["status"] in (RUN_PENDING, RUN_RUNNING) and steps and all(
                info["status"] in _STEP_SETTLED for info in steps.values()
            ):
                # 每一步都定下来了，运行行却还停在跑：执行体半路没了（旧实现把
                # 终态落库挂在发起的那条 SSE 上，断流就没人收尾）。按步骤行修
                # 一次——不修的话这里会永远转圈，界面也永远「执行中」。
                settle_run_status(db, owner_id, run_id)
                state, steps = _follow_state(db, owner_id, run_id)
                if state is None:
                    raise LookupError("运行记录不存在")
            for key, info in steps.items():
                if info["status"] == seen.get(key):
                    continue
                seen[key] = info["status"]
                if info["status"] in _STEP_SETTLED:
                    yield "step_end", {
                        "key": key,
                        "status": info["status"],
                        "error": info["error"],
                        "output_chars": info["output_chars"],
                        "elapsed_ms": info["elapsed_ms"],
                        "model_name": info["model_name"],
                    }
                else:
                    # pending / running 都算「这一步没定下来」。重跑会把已经跑完的
                    # 一步打回 pending，这条事件让客户端先清掉上一次的显示。
                    yield "step_start", {
                        "key": key,
                        "group_key": info["group_key"],
                        "status": info["status"],
                    }

        yield "run_end", {
            "run_id": run_id,
            "status": state["status"],
            "error": state["error"],
            "elapsed_ms": state["elapsed_ms"],
        }
