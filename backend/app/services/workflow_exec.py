"""执行器：把一次运行按组跑完，逐组并行、逐步落现场。

**每步现场都落库**。渲染后的 prompt、原始输出、解析结果、耗时、用的哪份模型
配置，全进 ``workflow_run_step``，而且每一步一进来就先落 ``running``。以前一个
后台任务只在业务行上留个状态，「当时模型到底收到了什么」根本问不出来；即时
提交也让进度不依赖这条流——换设备打开历史详情照样看得到跑到哪了，最后一起
提交那种整段丢失不会有。

**步骤并发写各自的行**。同组步骤是 ``asyncio.gather`` 并发跑的，SQLAlchemy 的
Session 不是并发安全的，共用请求 session 会写出互相覆盖的脏数据，所以每个步骤
自己开一条。

可选步骤失败按空串进上下文（下游模板里「如果有配图建议就附上」那句自然不附上，
``optional`` 才不是形同虚设）；非可选步骤失败就停在这一组，后面没轮到的步骤标成
``skipped``——历史里「没跑到」和「跑挂了」分得开。``start_group_index`` > 0 是断点
续跑，上游组既不执行也不发事件，上下文由 :func:`_rebuild_context` 按当时落库的
产物重建。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.entities import McpServer, Workflow, WorkflowRun, WorkflowRunStep, WorkflowStep
from app.services.agent_run import model_error_text, parse_output, require_chat_model, run_agent
from app.services.agents import AgentService
from app.services.mcp_tools import open_bundle, plan_step_tools, resolve_servers
from app.services.workflow_template import _group_order, render_prompt
from app.services.workflows import (
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.workflows")

# ---- 执行 ------------------------------------------------------------------

def _merge_servers(*groups: Sequence[McpServer]) -> List[McpServer]:
    """把多组已解析的服务按 id 去重合并：同一服务两级都挂只连接一次。"""
    merged: Dict[int, McpServer] = {}
    for group in groups:
        for server in group:
            merged.setdefault(server.id, server)
    return list(merged.values())


@dataclass
class _StepOutcome:
    """一个步骤的执行结果，供编排协程判断要不要继续往下走。"""

    key: str
    status: str
    optional: bool
    output_var: Optional[str] = None
    output: Optional[str] = None
    parsed: Optional[object] = None
    error: Optional[str] = None


def _context_value(output: Optional[str], parsed: Optional[object]) -> object:
    """一步的产物进上下文的样子：整段文本，JSON 步换成解析出来的对象。

    这样 ``{{x}}`` 引用 JSON 步骤时喂给下游的是按契约筛过的结构（模型原文里混着
    的解释性文字不喂），``{{x.字段}}`` 也才有的可取（见 :func:`_lookup`）。
    """
    return parsed if parsed is not None else (output or "")


def _rebuild_context(
    db: Session,
    run: WorkflowRun,
    definitions: Sequence[WorkflowStep],
    stop_group_key: str,
) -> Dict[str, object]:
    """按当时落库的上游产物重建上下文：运行输入 ＋ 严格早于 ``stop_group_key`` 的各组产物。

    只带目标组**之前**那些组的输出：同组并行步骤第一次跑的时候也看不见彼此的
    产物，重跑/续跑要复刻的就是当时那张 prompt，不能因为这次跑得晚就多喂一段。
    """
    earlier: Set[str] = set()
    for group_key, _members in _group_order(definitions):
        if group_key == stop_group_key:
            break
        earlier.add(group_key)

    by_key = {step.key: step for step in definitions}
    context: Dict[str, object] = dict(run.inputs or {})
    for row in db.scalars(
        select(WorkflowRunStep)
        .where(WorkflowRunStep.owner_id == run.owner_id, WorkflowRunStep.run_id == run.id)
        .order_by(WorkflowRunStep.sort_order, WorkflowRunStep.id)
    ).all():
        step = by_key.get(row.key)
        if step is None or not step.output_var or row.group_key not in earlier:
            continue
        if row.status == STEP_SUCCEEDED:
            context[step.output_var] = _context_value(row.output, row.parsed)
        elif row.status == STEP_FAILED and step.optional:
            # 与执行器同一处理：可选步骤的产物按空串进上下文，下游自己跳过。
            context[step.output_var] = ""
    return context


_GROUP_DONE = "__group_done__"


async def _execute_step(
    *,
    owner_id: int,
    run_id: int,
    step: WorkflowStep,
    context: Dict[str, object],
    queue: asyncio.Queue,
    tools: Optional[Sequence] = None,
) -> _StepOutcome:
    """跑一个步骤：渲染 prompt → 调模型 → 按契约解析 → 落这一行。

    用独立的 session：同组的步骤是并发跑的，共用一个 Session 会互相覆盖。
    每一步一进来就先把 ``running`` 落库并发事件，这样卡在模型上的那一步
    在界面上看得见是卡在哪，而不是整条流程一片空白地转圈。
    """
    from app.database import SessionLocal

    started = time.monotonic()
    await queue.put(("step_start", {"key": step.key, "group_key": step.group_key}))

    with SessionLocal() as db:
        row = db.scalar(
            select(WorkflowRunStep).where(
                WorkflowRunStep.owner_id == owner_id,
                WorkflowRunStep.run_id == run_id,
                WorkflowRunStep.key == step.key,
            )
        )
        if row is None:
            # 发起运行时这些行就建齐了，缺行说明运行记录被中途删过。
            outcome = _StepOutcome(step.key, STEP_FAILED, step.optional, error="步骤记录不存在")
            await queue.put(("step_end", _step_event(outcome)))
            return outcome

        row.status = STEP_RUNNING
        row.started_at = db_now()
        db.commit()

        try:
            prompt = render_prompt(step.prompt_tpl, context)
            row.prompt = prompt
            db.commit()
        except ValueError as exc:
            return await _finish_step(db, row, step, queue, str(exc), started)

        model_name: Optional[str] = None
        try:
            # 落这一步**实际**用的模型：定义里的 model_config_id 只写按步骤的覆盖，
            # 空着的是运行时的默认对话模型。历史行只存前者的话，绝大多数运行在
            # 界面上就是「没用什么模型」。
            config = require_chat_model(db, owner_id, step.model_config_id)
            row.model_config_id = config.id
            model_name = config.model_name
            db.commit()
            text = await run_agent(
                db,
                owner_id,
                step.agent_slug,
                prompt,
                config_id=config.id,
                tools=tools,
            )
            plain, parsed = parse_output(text, step.output_kind, step.output_keys)
        except Exception as exc:  # noqa: BLE001 - 步骤的失败就是这一步的状态
            logger.warning(
                "workflow step %s of run %s failed: %s", step.key, run_id, exc, exc_info=True
            )
            return await _finish_step(
                db, row, step, queue, model_error_text(exc), started, model_name=model_name
            )

        return await _finish_step(
            db, row, step, queue, None, started,
            output=plain, parsed=parsed, model_name=model_name,
        )


async def _finish_step(
    db: Session,
    row: WorkflowRunStep,
    step: WorkflowStep,
    queue: asyncio.Queue,
    error: Optional[str],
    started: float,
    *,
    output: Optional[str] = None,
    parsed: Optional[dict] = None,
    model_name: Optional[str] = None,
) -> _StepOutcome:
    """写终态并补一条 ``step_end`` 事件。"""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    row.status = STEP_FAILED if error else STEP_SUCCEEDED
    row.output = output
    row.parsed = parsed
    row.error_msg = error
    row.elapsed_ms = elapsed_ms
    row.finished_at = db_now()
    db.commit()

    outcome = _StepOutcome(
        key=step.key,
        status=row.status,
        optional=step.optional,
        output_var=step.output_var,
        output=output,
        parsed=parsed,
        error=error,
    )
    event = _step_event(outcome, elapsed_ms=elapsed_ms, parsed=parsed, model_name=model_name)
    await queue.put(("step_end", event))
    return outcome


def _step_event(
    outcome: _StepOutcome,
    *,
    elapsed_ms: Optional[int] = None,
    parsed: Optional[dict] = None,
    model_name: Optional[str] = None,
) -> dict:
    return {
        "key": outcome.key,
        "status": outcome.status,
        "error": outcome.error,
        # 正文只带摘要级的长度信息：完整输出走运行详情接口按步取，
        # 塞进事件里会让一次多步骤流程的 SSE 载荷翻几倍。
        "output_chars": len(outcome.output or ""),
        "elapsed_ms": elapsed_ms,
        "parsed": parsed,
        # 跑完才说得出用了谁：这一步的模型是运行时才解析出来的（定义里空着就是
        # 当时的默认对话模型），不带上就得等整条跑完展开现场才知道。
        "model_name": model_name,
    }


async def execute_run(
    *,
    owner_id: int,
    run_id: int,
    start_group_index: int = 0,
    overrides: Optional[Dict[str, object]] = None,
) -> AsyncIterator[Tuple[str, object]]:
    """执行一次已经建好的运行，产出 ``(event, payload)`` 供 SSE 转发。

    事件顺序：``run_start`` →（每组）若干 ``step_start`` / ``step_end`` →
    ``run_end``。并行组内的事件按各步骤实际完成的先后到达，不保证成对相邻。

    每步的写入都是即时提交的，所以进度既不依赖这条流（换设备打开历史详情也
    能看到跑到哪了），也不会因为最后一起提交而整段丢失。

    ``start_group_index`` > 0 是断点续跑：该下标之前的组既不执行也不发事件，
    上下文按上游当时落库的产物重建后再叠加 ``overrides``（只影响这一次执行的
    临时覆盖值）。

    调用方必须是**消费到底**的一方（:func:`stream_run` 的后台任务或功能侧的
    后台任务）：运行的终态在生成器走完时才会落库，半路丢掉生成器会把运行
    永远留在 running。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        run = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
            )
        )
        if run is None:
            raise LookupError("运行记录不存在")
        steps = list(
            db.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_id == run.workflow_id)
                .order_by(WorkflowStep.sort_order, WorkflowStep.id)
            ).all()
        )
        if not steps:
            raise ValueError("该工作流没有可执行的步骤")

        groups = _group_order(steps)
        if not 0 <= start_group_index < len(groups):
            raise ValueError("续跑起点超出了这条流程的步骤范围")
        # 续跑只有起点及之后的组要执行：MCP 节点挂载只解析这些步骤的，不必为
        # 上游组挂的服务建立连接。
        active_group_keys = {key for key, _members in groups[start_group_index:]}
        active_steps = [step for step in steps if step.group_key in active_group_keys]

        # 模型门槛提前测：没配好对话模型时，第一条事件就该是明确的「去配置」，
        # 而不是让每个步骤各自失败一遍。按步骤指定的配置在步骤里各自校验。
        require_chat_model(db, owner_id)

        # MCP 连接束：连接集合 = 工作流级挂载 ∪ 待执行步骤的节点级挂载（并集），
        # 在这里统一连接、每个服务只 initialize 一次。连接失败发生在首个事件
        # 之前，由 stream_run 的既有异常路径把 run 落成 failed（与「模型未
        # 就绪」的前置失败同路径）。
        workflow = db.get(Workflow, run.workflow_id)
        global_ids = list(dict.fromkeys(workflow.mcp_server_ids or []))
        # 两级分别解析：节点级的停用/越权错误要带得出处，而不是一句通用提示。
        global_servers = resolve_servers(db, owner_id, global_ids)
        step_servers: List[McpServer] = []
        for step in active_steps:
            try:
                step_servers.extend(
                    resolve_servers(db, owner_id, list(step.mcp_server_ids or []))
                )
            except (LookupError, ValueError) as exc:
                raise ValueError(
                    f"节点 {step.key} 挂载的 MCP 服务有问题：{exc}"
                ) from exc
        bundle = await open_bundle(_merge_servers(global_servers, step_servers))
        # 预计算每步的工具子集（冲突在同节点生效集合内判），同样在首事件之前。
        step_tool_plan = plan_step_tools(bundle, global_ids, active_steps)

        try:
            run.status = RUN_RUNNING
            if run.started_at is None:
                # 续跑保留原始开始时间：这条运行确实是从那一刻发起的。
                run.started_at = db_now()
            run.error_msg = None
            db.commit()

            agent_names = {
                agent.slug: agent.name
                for agent in AgentService(db, owner_id).list()
            }
            yield "run_start", {
                "run_id": run.id,
                "workflow_slug": run.workflow_slug,
                "steps": [
                    {
                        "key": step.key,
                        "group_key": step.group_key,
                        "agent_slug": step.agent_slug,
                        "agent_name": agent_names.get(step.agent_slug),
                        "output_kind": step.output_kind,
                        "optional": step.optional,
                    }
                    for step in steps
                ],
            }

            context: Dict[str, object] = (
                # 续跑：上游产物按当时落库的行重建；从头跑：只有运行输入。
                _rebuild_context(db, run, steps, groups[start_group_index][0])
                if start_group_index
                else dict(run.inputs or {})
            )
            # 本次执行的临时覆盖：不写回运行输入与上游行，只改变重跑步骤的 prompt。
            if overrides:
                context.update(overrides)
            queue: asyncio.Queue = asyncio.Queue()
            started = time.monotonic()
            failed: Optional[str] = None

            for group_index, (group_key, group) in enumerate(groups):
                if group_index < start_group_index:
                    # 上游组保持原样：不执行、不发事件、不合并产物。
                    continue

                async def _drive(members: List[WorkflowStep]) -> None:
                    results = await asyncio.gather(
                        *[
                            _execute_step(
                                owner_id=owner_id, run_id=run_id, step=member,
                                context=context, queue=queue,
                                tools=step_tool_plan[member.key],
                            )
                            for member in members
                        ],
                        # 步骤内部的异常已经在 ``_execute_step`` 里落成状态了；
                        # return_exceptions 只是最后一道网，免得一个意外把同组的
                        # 兄弟步骤拖成没人收尸的挂起任务。
                        return_exceptions=True,
                    )
                    await queue.put((_GROUP_DONE, results))

                task = asyncio.create_task(_drive(group))
                while True:
                    payload = await queue.get()
                    if isinstance(payload, tuple) and payload[0] == _GROUP_DONE:
                        results = payload[1]
                        break
                    yield payload[0], payload[1]
                await task

                for outcome in results:
                    if isinstance(outcome, _StepOutcome):
                        if outcome.status == STEP_SUCCEEDED and outcome.output_var:
                            context[outcome.output_var] = _context_value(
                                outcome.output, outcome.parsed
                            )
                        elif outcome.status == STEP_FAILED and outcome.optional and outcome.output_var:
                            # 可选步骤挂了不算完：它的产物按空串进上下文，下游那句
                            # 「如果有配图建议就附上」自然就不附上。不补这一手的话，
                            # optional 形同虚设——下一步会因为没有定义的元素报错，
                            # 整条流程照样停摆。
                            context[outcome.output_var] = ""
                    else:  # 不该发生：_execute_step 自己兜住了异常。
                        logger.error(
                            "workflow step of run %s raised outside its own handling: %s",
                            run_id,
                            outcome,
                        )

                broken = [
                    outcome
                    for outcome in results
                    if isinstance(outcome, _StepOutcome)
                    and outcome.status == STEP_FAILED
                    and not outcome.optional
                ]
                if broken:
                    failed = broken[0].error or "步骤执行失败"
                    break

            # 剩下的步骤标成 skipped：并行组里「没轮到跑」和「跑挂了」在历史里
            # 得区分得开，否则一次半途失败看起来像全盘失败。
            db.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.owner_id == owner_id,
                    WorkflowRunStep.run_id == run_id,
                    WorkflowRunStep.status.in_((STEP_PENDING, STEP_RUNNING)),
                )
                .values(status=STEP_SKIPPED, finished_at=db_now())
            )

            run = db.get(WorkflowRun, run_id)
            run.status = RUN_FAILED if failed else RUN_SUCCEEDED
            run.error_msg = failed
            # 续跑把这一段的耗时累加在原来的 elapsed_ms 上：从头跑时原值是 None。
            segment_elapsed_ms = int((time.monotonic() - started) * 1000)
            run.elapsed_ms = (run.elapsed_ms or 0) + segment_elapsed_ms
            run.finished_at = db_now()
            db.commit()

            yield "run_end", {
                "run_id": run_id,
                "status": run.status,
                "error": run.error_msg,
                "elapsed_ms": run.elapsed_ms,
            }
        finally:
            # LIFO：先关各 ClientSession 再关传输，stdio 子进程在此被回收。
            await bundle.__aexit__(None, None, None)
