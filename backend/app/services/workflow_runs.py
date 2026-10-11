"""运行的账本：遗留收尾、留存清理，以及运行 ↔ 业务对象的状态翻译。

**一条运行认领一个业务对象**。``subject_kind`` / ``subject_ref`` 把运行挂到简历、
deck、面试题上，业务行的 ``status`` / ``error_msg`` 由 :func:`subject_state` 从
最近一次运行翻译出来——以前同一件事在业务表和运行表各存一份，两套状态机各自
收尾，重启时 ``reset_stale_runs`` 与功能侧的收尾互不知情，同一件事能给出两个答案。

执行体活在 web worker 里，一次重启就把它丢掉，所以启动时把还挂在 ``pending`` /
``running`` 的运行统一写成失败（原因固定是 ``STALE_RUN_ERROR``）。历史运行按
``RUN_RETENTION_DAYS`` 清理，但「正在跑的」与「它是某个业务对象的最近一次」两条
是硬豁免——删了后者等于删掉那份简历现在的状态。

列表页要的最近一次运行一次查询取回（:func:`latest_runs`），简历与幻灯片列表
不是每行查一次。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.entities import WorkflowRun, WorkflowRunStep
from app.services.workflows import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_READY,
    SUBJECT_UPLOADED,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.workflows")

# 运行记录的默认保留天数：比这更早、又不再是谁的当前状态的运行，启动时清掉。
RUN_RETENTION_DAYS = 30

# 上个进程遗留的运行统一写这个原因（见 ``reset_stale_runs``）。
STALE_RUN_ERROR = "服务重启导致本次运行中断，请重新发起"


def reset_stale_runs(db: Session) -> None:
    """把上个进程遗留的运行收尾。

    执行体活在 web worker 里，一次重启就把它丢掉：不收拾的话这些运行永远停在
    ``running``，进行中的步骤永远转圈，用户既看不出它已经死了，也没法重发。
    """
    stale = db.scalars(
        select(WorkflowRun).where(WorkflowRun.status.in_((RUN_PENDING, RUN_RUNNING)))
    ).all()
    if not stale:
        return
    run_ids = [run.id for run in stale]
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id.in_(run_ids))
        .values(status=RUN_FAILED, error_msg=STALE_RUN_ERROR, finished_at=db_now())
    )
    db.execute(
        update(WorkflowRunStep)
        .where(WorkflowRunStep.run_id.in_(run_ids))
        .where(WorkflowRunStep.status.in_((STEP_PENDING, STEP_RUNNING)))
        .values(status=STEP_FAILED, error_msg=STALE_RUN_ERROR, finished_at=db_now())
    )
    db.commit()
    logger.info("reset %s stale workflow run(s) after a restart", len(stale))


def prune_runs(
    db: Session, *, owner_id: Optional[int] = None, keep_days: int = RUN_RETENTION_DAYS
) -> Tuple[int, int]:
    """清掉「既太老、又不是任何人的当前状态」的运行记录，返回 ``(运行数, 步骤数)``。

    一条运行被删要同时满足三条：早于保留窗口、不在跑、不是它那个业务对象的最近一次。
    后两条是硬要求——业务行的状态由认领它的那次运行翻译出来（见 :func:`subject_state`），
    把「最近一次」删掉等于把那份简历现在的状态删了；进行中的删掉则留下一堆没人跑完的
    失败。没认领业务对象的那几次（在工作流页面上手动发起的）没有状态要担，过窗口就删。

    ``owner_id`` 省略就是全站的：启动时那一次走的就是这个口径。
    """
    cutoff = db_now() - timedelta(days=keep_days)
    stmt = select(
        WorkflowRun.id, WorkflowRun.status, WorkflowRun.subject_kind, WorkflowRun.subject_ref
    ).where(WorkflowRun.created_at < cutoff)
    if owner_id is not None:
        stmt = stmt.where(WorkflowRun.owner_id == owner_id)
    candidates = db.execute(stmt).all()
    if not candidates:
        return 0, 0

    # 每个业务对象的最近一次（id 最大）单独查：候选集里只有过了窗口的老运行，
    # 在这儿比「谁最新」会把该删的那次算成保留的那次。
    latest_ids = set(
        db.execute(
            select(func.max(WorkflowRun.id))
            .where(WorkflowRun.subject_kind.is_not(None))
            .group_by(WorkflowRun.subject_kind, WorkflowRun.subject_ref)
        ).scalars().all()
    )
    doomed = [
        run_id
        for run_id, status, _kind, _ref in candidates
        if status not in (RUN_PENDING, RUN_RUNNING) and run_id not in latest_ids
    ]
    if not doomed:
        return 0, 0

    deleted_steps = int(
        db.execute(delete(WorkflowRunStep).where(WorkflowRunStep.run_id.in_(doomed))).rowcount or 0
    )
    deleted_runs = int(
        db.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(doomed))).rowcount or 0
    )
    db.commit()
    logger.info(
        "pruned %s workflow run(s) (%s step row(s)) older than %s day(s)",
        deleted_runs,
        deleted_steps,
        keep_days,
    )
    return deleted_runs, deleted_steps


# ---- 运行 ↔ 业务对象 --------------------------------------------------------

# 运行状态 → 业务行状态的翻译表。业务表上那份状态从来不是自己写的，是从认领它的
# 那次运行读出来的（见 :func:`subject_state`）。
_RUN_TO_SUBJECT = {
    RUN_PENDING: SUBJECT_ANALYZING,
    RUN_RUNNING: SUBJECT_ANALYZING,
    RUN_SUCCEEDED: SUBJECT_READY,
    RUN_FAILED: SUBJECT_ERROR,
    RUN_CANCELLED: SUBJECT_ERROR,
}


def latest_runs(
    db: Session, owner_id: int, subject_kind: str, refs: Sequence[object]
) -> Dict[str, WorkflowRun]:
    """每个业务对象的**最近一次**运行，按 ``subject_ref`` 取。

    「最近一次」=  id 最大：重新分析、重新生成都是新起一条运行，旧记录只做历史。
    一次查询把整页列表要的状态都取回来，简历/幻灯片列表不是每行一次查询。
    """
    keys = [str(ref) for ref in refs]
    if not keys:
        return {}
    newest = (
        select(WorkflowRun.subject_ref, func.max(WorkflowRun.id).label("run_id"))
        .where(
            WorkflowRun.owner_id == owner_id,
            WorkflowRun.subject_kind == subject_kind,
            WorkflowRun.subject_ref.in_(keys),
        )
        .group_by(WorkflowRun.subject_ref)
        .subquery()
    )
    rows = db.scalars(
        select(WorkflowRun).join(newest, WorkflowRun.id == newest.c.run_id)
    ).all()
    return {row.subject_ref: row for row in rows}


def latest_run(
    db: Session, owner_id: int, subject_kind: str, subject_ref: object
) -> Optional[WorkflowRun]:
    return latest_runs(db, owner_id, subject_kind, [subject_ref]).get(str(subject_ref))


def subject_state(
    run: Optional[WorkflowRun],
    *,
    has_product: bool,
    idle: str = SUBJECT_UPLOADED,
) -> Tuple[str, Optional[str]]:
    """把最近一次运行翻译成业务行的 ``(状态, 错误信息)``。

    业务表上不再有 ``status`` / ``error_msg``：以前一次生成要往两处写，重启时
    ``reset_stale_runs`` 与业务侧各自的收尾互不知情，同一件事能给出两个答案。

    没有运行记录时按「有没有产物」判断——本次改造之前发起的存量任务就是靠这个
    仍然显示为已完成。``idle`` 是那份产物也没有时的取值：简历用 ``uploaded``
    （传上来还没分析），创建即发起生成的 deck / 任务用自己的初始态。

    反过来也要对上：**运行成功却没有产物**不能报「已完成」。单步重跑就会造出这种
    组合——那一步重新跑通了，业务行上的产物却不会被一个步骤重写（后处理在功能侧），
    照 ``succeeded → ready`` 直译就是一页什么都没有的「已完成」。
    """
    if run is None:
        return (SUBJECT_READY if has_product else idle), None
    status = _RUN_TO_SUBJECT.get(run.status, SUBJECT_ERROR)
    if status == SUBJECT_READY and not has_product:
        return SUBJECT_ERROR, "工作流跑完了，但结果没有写回这条记录，请重新发起一次完整任务"
    return status, run.error_msg if status == SUBJECT_ERROR else None


def running_subjects(
    db: Session, owner_id: int, subject_kind: str, refs: Sequence[object]
) -> List[str]:
    """这些业务对象里**还在跑**的（pending/running）——删除与重复触发的闸门。

    只看最近一次运行：历史里失败的运行不该挡着用户删东西。
    """
    return [
        ref
        for ref, run in latest_runs(db, owner_id, subject_kind, refs).items()
        if run.status in (RUN_PENDING, RUN_RUNNING)
    ]


def fail_run(db: Session, owner_id: int, run_id: int, message: str) -> None:
    """把「每一步都跑通了、产物却没法用」的运行降级为失败。

    业务侧的后处理（清洗页面结构、校验报告非空）在引擎之外：那时运行本身是
    ``succeeded`` 的，模型的现场要留着，但这次任务确实没成——状态得跟着改，
    否则界面上会出现一份什么都没有的「已完成」。
    """
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
        )
    )
    if run is None or run.status != RUN_SUCCEEDED:
        return
    run.status = RUN_FAILED
    run.error_msg = message
    db.commit()
    logger.info("run %s marked failed by its caller: %s", run_id, message)


def settle_run_status(db: Session, owner_id: int, run_id: int, *, error: Optional[str] = None) -> None:
    """按现在的全部步骤行重算一次运行的状态。

    重跑只换这一步的产物，功能侧的后处理（写回简历报告、deck 页面）只认整条流程
    跑完，所以「这一步成功」不等于「业务行有结果」。那一层由 :func:`subject_state`
    兜住：运行成功却没有产物，界面上报的是明确的错。
    """
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
        )
    )
    if run is None:
        return
    rows = list(
        db.scalars(
            select(WorkflowRunStep).where(
                WorkflowRunStep.owner_id == owner_id, WorkflowRunStep.run_id == run_id
            )
        ).all()
    )
    failed = [row for row in rows if row.status == STEP_FAILED]
    unfinished = [
        row for row in rows if row.status in (STEP_PENDING, STEP_RUNNING, STEP_SKIPPED)
    ]
    if error:
        run.status, run.error_msg = RUN_FAILED, error
    elif failed:
        run.status, run.error_msg = RUN_FAILED, failed[0].error_msg or "步骤执行失败"
    elif unfinished:
        run.status, run.error_msg = RUN_FAILED, f"重跑的这一步之后还有 {len(unfinished)} 步没有跑"
    else:
        run.status, run.error_msg = RUN_SUCCEEDED, None
    run.finished_at = db_now()
    db.commit()
