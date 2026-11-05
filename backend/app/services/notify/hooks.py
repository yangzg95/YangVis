"""事件到通知的翻译层：五个挂载点共用的辅助函数。

dispatch 收纯文本（title/body），但触发点手里是 ORM 行；把「怎么拼文案、
怎么脱敏、超量怎么合并」收在这里，挂载点只留一行调用——散落在各家的
f-string 会越抄越歪（脱敏规则尤其不能有两份）。

所有函数都是**同步**的：内部经 ``_safe_fire`` 把 dispatch 挂成后台任务，
挂载点无论在请求路径、启动路径还是工作线程里都直接调，不需要 await。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Iterable, Optional, Sequence

from app.background import fire_and_forget
from app.services.notify.dispatch import dispatch

logger = logging.getLogger("yangvis.notify")

# 同一 owner 单次批量事件超过这个数就合并成一条摘要：进程中断收尾可能一把挂掉
# 几十条运行，逐条推送等于自己给自己轰炸。
_BULK_THRESHOLD = 3

# 合并摘要里最多点名的条数，其余只报数量。
_BULK_LISTED = 3


def _safe_fire(coro: Awaitable) -> None:
    """fire_and_forget 的兜底版。

    正常挂载点都在事件循环里（SSE 生成器、请求路径、lifespan）；但 ``fail_run``
    这类收尾可能被功能侧放进 ``to_thread`` 的线程里跑——那里没有事件循环，
    ``asyncio.create_task`` 会抛 RuntimeError 击穿主流程。通知的合同是绝不
    拖垮主流程，所以这里丢弃这条消息并记日志；``close()`` 释放协程资源，
    顺带消掉「coroutine never awaited」的噪音。
    """
    try:
        fire_and_forget(coro)
    except RuntimeError:
        coro.close()
        logger.warning("notify dropped: no running event loop in this thread")


def _command_first_word(command: Optional[str]) -> str:
    """命令只留首词进通知。

    审计表里有全文；通知正文可能进群、被截图，参数里的路径、token、密码
    不能跟着出去。空命令返回空串。
    """
    if not command or not command.strip():
        return ""
    return command.split(None, 1)[0]


def notify_workflow_finished(
    owner_id: int,
    *,
    run_id: int,
    slug: Optional[str],
    status: str,
    error: Optional[str],
    elapsed_ms: Optional[int],
) -> None:
    """一次运行的终态（成功或失败）。

    挂载点：stream_run 落终态、fail_run 降级、settle_run_status 重跑收尾、
    reset_stale_runs 重启收尾（单条时）。失败默认订阅、成功默认关——在事件
    词表里定义，这里不重复判断。
    """
    name = f"{slug or 'workflow'} #{run_id}"
    if status == "failed":
        _safe_fire(
            dispatch(
                owner_id,
                "workflow.failed",
                title=f"工作流运行失败：{name}",
                body=error or "步骤执行失败",
            )
        )
        return
    elapsed = f"{elapsed_ms / 1000:.1f}s" if elapsed_ms is not None else "未知耗时"
    _safe_fire(
        dispatch(
            owner_id,
            "workflow.succeeded",
            title=f"工作流运行成功：{name}",
            body=f"耗时 {elapsed}",
        )
    )


def notify_stale_runs_reset(rows: Sequence) -> None:
    """进程中断收尾：把遗留 running 物化为 failed 后的批量通知。

    触发方既包括容器/服务重启，也包括 gunicorn worker 到期回收
    （``--max-requests``）——新 worker 一样会跑 lifespan 收尾，所以文案
    统一只说「服务进程中断」，不写「服务重启」。

    ``rows`` 是 WorkflowRun 行（或含 owner_id/id/workflow_slug 的任意对象），
    按 owner 分组；组内超过阈值合并成一条摘要。
    """
    by_owner: dict[int, list] = {}
    for run in rows:
        by_owner.setdefault(run.owner_id, []).append(run)
    for owner_id, runs in by_owner.items():
        if len(runs) <= _BULK_THRESHOLD:
            for run in runs:
                notify_workflow_finished(
                    owner_id,
                    run_id=run.id,
                    slug=run.workflow_slug,
                    status="failed",
                    error="服务进程中断，本次运行未完成，请重新发起",
                    elapsed_ms=None,
                )
            continue
        listed = "、".join(f"{run.workflow_slug or 'workflow'} #{run.id}" for run in runs[:_BULK_LISTED])
        _safe_fire(
            dispatch(
                owner_id,
                "workflow.failed",
                title=f"{len(runs)} 条运行被中断",
                body=f"服务进程中断，涉及 {listed} 等 {len(runs)} 条运行，已统一标记为失败，请重新发起。",
            )
        )


def notify_ops_proposed(
    owner_id: int,
    *,
    action_id: int,
    server_name: Optional[str],
    reason: Optional[str],
    command: str,
) -> None:
    """AI 提议了一条写命令，等用户确认。

    正文脱敏口径与 chat_ops 的日志一致：服务器名、action id、模型理由、
    命令首词。命令全文留在审计表里。
    """
    parts = [f"action #{action_id}", f"服务器 {server_name or '?'}"]
    first = _command_first_word(command)
    if first:
        parts.append(f"命令 {first}")
    if reason:
        parts.append(f"理由：{reason[:120]}")
    _safe_fire(
        dispatch(owner_id, "ops.pending", title="AI 提议运维命令，等待确认", body="；".join(parts))
    )


def notify_stale_ops_expired(rows: Iterable) -> None:
    """进程中断收尾：遗留 pending 审批物化为 expired 后的批量通知。

    与 :func:`notify_stale_runs_reset` 同一个触发面——worker 回收并不重启
    服务，标题不写「服务重启」，只陈述过期事实。

    ``rows`` 含 owner_id/id/target_name/command 的行，按 owner 分组合并。
    """
    by_owner: dict[int, list] = {}
    for row in rows:
        by_owner.setdefault(row.owner_id, []).append(row)
    for owner_id, actions in by_owner.items():
        listed = "、".join(f"#{row.id}（{row.target_name or '?'}）" for row in actions[:_BULK_LISTED])
        summary = f"共 {len(actions)} 条" if len(actions) > _BULK_LISTED else f"{len(actions)} 条"
        _safe_fire(
            dispatch(
                owner_id,
                "ops.expired",
                title=f"{summary}待确认命令已过期",
                body=f"涉及 {listed}。确认等待已超时，原确认流已中断，请让 AI 重新发起。",
            )
        )
