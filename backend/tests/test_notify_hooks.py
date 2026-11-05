"""通知挂载点：文案拼接、脱敏、合并摘要与「绝不击穿主流程」的合同。

hooks 是五个挂载点共用的翻译层，这里钉住三件事：

1. **脱敏**：命令只出首词（参数里的路径/token/密码不进群消息）。
2. **合并**：同一 owner 批量事件超过阈值合并成一条摘要，不同 owner 互不混流。
3. **合同**：在工作线程（无事件循环）里调用也不抛——通知丢了可以，主流程
   丢了不行。

dispatch 本身在 test_notify_dispatch.py 验过，这里整模块打桩收集调用，
不碰 DB 也不出网。
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.services.notify import hooks
from app.services.notify.hooks import (
    _command_first_word,
    _safe_fire,
    notify_ops_proposed,
    notify_stale_ops_expired,
    notify_stale_runs_reset,
    notify_workflow_finished,
)


class _Dispatch:
    """收集 dispatch 调用的假实现（模块级打桩）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, owner_id: int, event: str, *, title: str, body: str = ""):
        self.calls.append(
            {"owner_id": owner_id, "event": event, "title": title, "body": body}
        )
        # 返回一个已完成的协程占位：_safe_fire 会把它当 coro 处理。
        return _completed_coro()

    def of(self, event: str) -> list[dict]:
        return [call for call in self.calls if call["event"] == event]


async def _completed_coro():
    return None


@pytest.fixture()
def dispatched(monkeypatch) -> _Dispatch:
    stub = _Dispatch()
    monkeypatch.setattr(hooks, "dispatch", stub)
    return stub


# ---- 运行终态 ---------------------------------------------------------------


def test_workflow_failed_copy(dispatched):
    notify_workflow_finished(
        7, run_id=123, slug="resume-batch", status="failed", error="模型超时", elapsed_ms=None
    )
    call = dispatched.of("workflow.failed")[0]
    assert call["owner_id"] == 7
    assert call["title"] == "工作流运行失败：resume-batch #123"
    assert call["body"] == "模型超时"


def test_workflow_succeeded_has_elapsed(dispatched):
    notify_workflow_finished(
        7, run_id=9, slug=None, status="succeeded", error=None, elapsed_ms=1500
    )
    call = dispatched.of("workflow.succeeded")[0]
    # slug 缺失时用通用词，不给 None 拼进标题。
    assert call["title"] == "工作流运行成功：workflow #9"
    assert call["body"] == "耗时 1.5s"


def test_workflow_failed_without_error_uses_fallback(dispatched):
    notify_workflow_finished(
        7, run_id=1, slug="x", status="failed", error=None, elapsed_ms=None
    )
    assert dispatched.of("workflow.failed")[0]["body"] == "步骤执行失败"


# ---- 重启收尾（运行） ---------------------------------------------------------


def _run_row(owner_id, run_id, slug):
    """与 WorkflowRun 行同形的轻量对象：hooks 只同步读这三个属性。"""

    class _Row:
        pass

    row = _Row()
    row.owner_id = owner_id
    row.id = run_id
    row.workflow_slug = slug
    return row


def test_stale_runs_small_batch_sends_individually(dispatched):
    notify_stale_runs_reset(
        [_run_row(1, 11, "resume-batch"), _run_row(1, 12, "deck-gen")]
    )
    failed = dispatched.of("workflow.failed")
    assert len(failed) == 2
    assert failed[0]["title"] == "工作流运行失败：resume-batch #11"
    assert "服务进程中断" in failed[0]["body"]
    # worker 到期回收并不重启服务，文案不许再出现「服务重启」。
    assert "服务重启" not in failed[0]["body"]


def test_stale_runs_large_batch_merges_into_summary(dispatched):
    rows = [_run_row(1, i, f"wf-{i}") for i in range(1, 6)]  # 5 > 3
    notify_stale_runs_reset(rows)
    failed = dispatched.of("workflow.failed")
    assert len(failed) == 1
    assert failed[0]["title"] == "5 条运行被中断"
    assert "服务进程中断" in failed[0]["body"]
    assert "服务重启" not in failed[0]["title"]
    # 摘要点名前 3 条，其余只报数量。
    assert "wf-1 #1" in failed[0]["body"] and "wf-3 #3" in failed[0]["body"]
    assert "wf-4 #4" not in failed[0]["body"]
    assert "5 条运行" in failed[0]["body"]


def test_stale_runs_groups_by_owner(dispatched):
    # 每个 owner 各自 2 条：都没到合并阈值，各自收到自己的 2 条。
    notify_stale_runs_reset(
        [_run_row(1, 11, "a"), _run_row(1, 12, "b"), _run_row(2, 21, "c"), _run_row(2, 22, "d")]
    )
    by_owner: dict[int, int] = {}
    for call in dispatched.of("workflow.failed"):
        by_owner[call["owner_id"]] = by_owner.get(call["owner_id"], 0) + 1
    assert by_owner == {1: 2, 2: 2}


# ---- 运维审批 ---------------------------------------------------------------


def test_ops_proposed_masks_command_arguments(dispatched):
    notify_ops_proposed(
        7,
        action_id=55,
        server_name="prod-web",
        reason="重启 nginx 以应用新配置",
        command="systemctl restart nginx --password=hunter2",
    )
    call = dispatched.of("ops.pending")[0]
    assert call["title"] == "AI 提议运维命令，等待确认"
    # 只出首词：命令参数（含密码）绝不进通知。
    assert "hunter2" not in call["body"]
    assert "restart nginx" not in call["body"]
    assert "命令 systemctl" in call["body"]
    assert "action #55" in call["body"] and "prod-web" in call["body"]
    assert "重启 nginx" in call["body"]  # reason 原文保留


def test_ops_proposed_truncates_long_reason(dispatched):
    notify_ops_proposed(
        7, action_id=1, server_name="s", reason="理" * 500, command="rm"
    )
    # reason 截 120 字，正文兜底截 1024——两道都过。
    assert "理" * 500 not in dispatched.of("ops.pending")[0]["body"]


def test_ops_proposed_empty_fields(dispatched):
    notify_ops_proposed(7, action_id=1, server_name=None, reason=None, command="   ")
    body = dispatched.of("ops.pending")[0]["body"]
    assert "服务器 ?" in body
    # 空命令不出「命令 」段。
    assert "命令" not in body


def test_stale_ops_expired_merges_by_owner(dispatched):
    def _action_row(owner_id, action_id, target_name):
        class _Row:
            pass

        row = _Row()
        row.owner_id = owner_id
        row.id = action_id
        row.target_name = target_name
        return row

    rows = [
        _action_row(1, 31, "prod-db"),
        _action_row(1, 32, "prod-web"),
        _action_row(2, 41, "dev"),
    ]
    notify_stale_ops_expired(rows)
    calls = dispatched.of("ops.expired")
    assert len(calls) == 2
    owner1 = next(c for c in calls if c["owner_id"] == 1)
    assert owner1["title"] == "2 条待确认命令已过期"
    assert "服务重启" not in owner1["title"] and "服务重启" not in owner1["body"]
    assert "确认等待已超时" in owner1["body"]
    assert "#31（prod-db）" in owner1["body"] and "#32（prod-web）" in owner1["body"]


# ---- 命令首词 ---------------------------------------------------------------


def test_command_first_word():
    assert _command_first_word("systemctl restart nginx --password=x") == "systemctl"
    assert _command_first_word("  ls -la  ") == "ls"
    assert _command_first_word("") == ""
    assert _command_first_word(None) == ""
    assert _command_first_word("   ") == ""


# ---- 合同：无事件循环不抛 -----------------------------------------------------


def test_safe_fire_in_thread_without_event_loop(caplog):
    """挂载点合同：to_thread 线程里没有事件循环，通知只能丢，不能炸。"""

    async def coro():
        raise AssertionError("这个协程不该被调度")  # pragma: no cover

    # 在无事件循环的线程里调用；返回值证明异常没有穿透出来。
    result: dict = {}

    def run():
        try:
            _safe_fire(coro())
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - 合同就是不许到这
            result["ok"] = False
            result["error"] = repr(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert result.get("ok") is True


def test_safe_fire_with_loop_schedules_task():
    """有事件循环时走正常 fire_and_forget 路径，协程被真正调度。"""
    ran = asyncio.Event()

    async def coro():
        ran.set()

    async def main():
        _safe_fire(coro())
        # 让出控制权，给后台任务一个执行窗口。
        await asyncio.sleep(0)

    asyncio.run(main())
    assert ran.is_set()


def test_hooks_call_dispatch_without_raising_in_plain_thread():
    """五个挂载点形态的端到端合同：同步上下文（无循环）里直接调也不抛。"""
    calls = []

    # dispatch 打桩：记录调用即可，不真的发。
    async def fake_dispatch(owner_id, event, *, title, body=""):
        calls.append((owner_id, event))  # pragma: no cover - 不会被调度

    original = hooks.dispatch
    hooks.dispatch = fake_dispatch
    try:

        class _Row:
            pass

        row = _Row()
        row.owner_id, row.id, row.target_name = 1, 1, "s"

        def run():
            # 模拟 to_thread 里的挂载点调用。
            notify_workflow_finished(
                1, run_id=1, slug="x", status="failed", error="e", elapsed_ms=None
            )
            notify_stale_ops_expired([row])

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
    finally:
        hooks.dispatch = original

    # 消息被丢弃（无循环），但调用没抛——这就是合同。
    assert calls == []
