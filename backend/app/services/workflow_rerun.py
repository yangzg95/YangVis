"""单步重跑与断点续跑：在一条终态运行上重做一步，或从某一组接着跑完。

两条路都是「prepare 在请求里把行打回 pending，执行交给路由的后台任务」。
``prepare_step_rerun`` / ``prepare_resume`` 是闸门所在地：还在跑的运行不能重发
（两个执行体会抢同一行），定义里已经没有的步骤不能重跑——prompt 模板只存在于
定义里，步骤行只存渲染出来的那一份。续跑重置的是目标组及之后各组的**全部步骤
行**，并行组不可分割，没法只接其中一步再往下跑。

``overrides`` 是这一次执行对上下文变量的临时覆盖值：prepare 只校验不写库，报错
必须发生在行被重置之前，不留半成品；真正应用由 :func:`rerun_step` /
:func:`resume_run` 按定义重新解析后叠加，两边共用同一个 helper。覆盖值本身不合法
（未知变量名、JSON 解析失败）抛 :class:`OverrideValidationError`，它仍是
``ValueError``，执行路径照旧按 ``ValueError`` 兜，只有路由需要把它与「运行还在跑」
的冲突分开。

进度不在这里发：步骤本来就是一步一提交，客户端用
:func:`app.services.workflow_stream.follow_run` 接上。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.entities import Workflow, WorkflowRun, WorkflowRunStep, WorkflowStep
from app.services.agent_run import OUTPUT_JSON, model_error_text
from app.services.mcp_tools import open_bundle, plan_step_tools, resolve_servers
from app.services.workflow_exec import _execute_step, _merge_servers, _rebuild_context, execute_run
from app.services.workflow_runs import settle_run_status
from app.services.workflow_template import _group_order
from app.services.workflows import RUN_PENDING, RUN_RUNNING, STEP_PENDING

logger = logging.getLogger("yangvis.workflows")

# ---- 单步重跑 ---------------------------------------------------------------

def _detach(db: Session, obj: object) -> object:
    """让一个对象带着已加载的列脱离 session。

    重跑要在**另一条连接**上写这一步，读它的 session 若还开着事务，SQLite 会
    被这个读快照挡住写。``commit()`` 会过期全部属性，所以脱离前先读一次把整行取回来。
    """
    db.commit()
    _ = obj.id  # noqa: B018 - 这次读就是要触发整行重新加载
    db.expunge(obj)
    return obj


def prepare_step_rerun(
    db: Session,
    owner_id: int,
    run_id: int,
    key: str,
    overrides: Optional[Dict[str, str]] = None,
) -> WorkflowRun:
    """把一次终态运行里的那一步打回待运行，现场交给调用方的后台任务去跑。

    闸门都在这儿：还在跑的运行不能重发（两个执行体会抢同一行），定义里已经没有的
    步骤不能重跑——prompt 模板只存在于定义里，步骤行只存渲染出来的那一份。

    ``overrides`` 是这次重跑对上下文变量的覆盖值，只校验不写库：报错要发生在
    行被重置**之前**，不留半成品；真正应用由 :func:`rerun_step` 完成。
    """
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
        )
    )
    if run is None:
        raise LookupError("运行记录不存在")
    if run.status in (RUN_PENDING, RUN_RUNNING):
        raise ValueError("这次运行还没结束，等它跑完再重跑单步")
    row = db.scalar(
        select(WorkflowRunStep).where(
            WorkflowRunStep.owner_id == owner_id,
            WorkflowRunStep.run_id == run_id,
            WorkflowRunStep.key == key,
        )
    )
    if row is None:
        raise ValueError("这次运行里没有这一步")
    definitions = _load_definitions(db, run.workflow_id)
    definition = next((step for step in definitions if step.key == key), None)
    if definition is None:
        raise ValueError("这条流程的定义里已经没有这一步，没有模板可以重跑")

    # 覆盖值先校验：与重置行同一请求里完成，失败就什么都不改。
    names, json_names = _context_fields(run, definitions, definition.group_key)
    _coerce_overrides(overrides, names, json_names)

    row.status = STEP_PENDING
    row.prompt = None
    row.output = None
    row.parsed = None
    row.error_msg = None
    row.elapsed_ms = None
    row.started_at = None
    row.finished_at = None
    # 整条运行回到执行中：这一步跑完之前，业务行上显示的是「分析中」，
    # 而不是上一次的结果——它确实正在被重做。
    run.status = RUN_RUNNING
    run.error_msg = None
    run.elapsed_ms = None
    run.finished_at = None
    db.commit()
    return run


def prepare_resume(
    db: Session,
    owner_id: int,
    run_id: int,
    key: str,
    overrides: Optional[Dict[str, str]] = None,
) -> Tuple[WorkflowRun, int]:
    """把一次终态运行从某一步所在组起重置为待运行，返回 ``(run, 起始组下标)``。

    闸门与单步重跑一致：还在跑的不能续（两个执行体抢行）；定义里已没有的步
    不能续——模板只存在定义里。重置范围是目标组及之后各组的**全部步骤行**：
    并行组不可分割，组内多步并行，没法只接其中一步再往下跑。
    """
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
        )
    )
    if run is None:
        raise LookupError("运行记录不存在")
    if run.status in (RUN_PENDING, RUN_RUNNING):
        raise ValueError("这次运行还没结束，等它跑完再从指定步继续")

    definitions = _load_definitions(db, run.workflow_id)
    groups = _group_order(definitions)
    target = next((step for step in definitions if step.key == key), None)
    if target is None:
        raise ValueError("这条流程的定义里已经没有这一步，没有模板可以继续")
    start_group_index = next(
        index
        for index, (group_key, _members) in enumerate(groups)
        if group_key == target.group_key
    )

    # 覆盖值同样先校验：重置是一条 bulk update，失败得在它之前。
    names, json_names = _context_fields(run, definitions, target.group_key)
    _coerce_overrides(overrides, names, json_names)

    reset_group_keys = [group_key for group_key, _members in groups[start_group_index:]]
    db.execute(
        update(WorkflowRunStep)
        .where(
            WorkflowRunStep.owner_id == owner_id,
            WorkflowRunStep.run_id == run_id,
            WorkflowRunStep.group_key.in_(reset_group_keys),
        )
        .values(
            status=STEP_PENDING,
            prompt=None,
            output=None,
            parsed=None,
            error_msg=None,
            elapsed_ms=None,
            started_at=None,
            finished_at=None,
            model_config_id=None,
        )
    )
    run.status = RUN_RUNNING
    run.error_msg = None
    # started_at 与 elapsed_ms 都保留：续跑是同一条运行的延续，累计耗时更接近事实。
    run.finished_at = None
    db.commit()
    return run, start_group_index


def _load_definitions(db: Session, workflow_id: int) -> List[WorkflowStep]:
    """一条流程当前的步骤定义，按执行顺序排列。"""
    return list(
        db.scalars(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
            .order_by(WorkflowStep.sort_order, WorkflowStep.id)
        ).all()
    )


def _context_fields(
    run: WorkflowRun,
    definitions: Sequence[WorkflowStep],
    stop_group_key: str,
) -> Tuple[Set[str], Set[str]]:
    """这一步可编辑/可覆盖的上下文变量名，形状 ``(全部名, JSON 产物名)``。

    名字集合＝运行输入 ＋ 严格早于本组的产物名；产物在定义里声明为 JSON 输出时，
    覆盖值要按 JSON 解析（与执行器放进上下文的形状一致）。
    """
    names: Set[str] = set(run.inputs or {})
    json_names: Set[str] = set()
    for group_key, members in _group_order(definitions):
        if group_key == stop_group_key:
            break
        for step in members:
            if not step.output_var:
                continue
            names.add(step.output_var)
            if step.output_kind == OUTPUT_JSON:
                json_names.add(step.output_var)
    return names, json_names


class OverrideValidationError(ValueError):
    """覆盖值本身不合法（未知变量名、JSON 解析失败）：是 400 类的请求错误。

    仍是 :class:`ValueError`——后台执行路径（:func:`rerun_step` / :func:`resume_run`）
    统一按 ValueError 捕获并结算运行，不必为此分流；只有路由需要把它与
    「运行还在跑」的 409 冲突区分开。
    """


def _coerce_overrides(
    raw: Optional[Dict[str, str]],
    names: Set[str],
    json_names: Set[str],
) -> Dict[str, object]:
    """把请求里的覆盖值整理成可进上下文的形状。

    名字不在这一步的上下文里一律拒绝：静默丢掉一个拼错的变量，用户会以为自己
    改过了，跑出来的 prompt 却还是旧的。JSON 产物的覆盖值在这里解析，格式错误
    也在请求期报，而不是等步骤跑到一半才失败。空串原样保留——与执行器里可选
    步骤失败的口径一致，代表「这段没有内容」。
    """
    if not raw:
        return {}
    overrides: Dict[str, object] = {}
    for name, value in raw.items():
        if name not in names:
            raise OverrideValidationError(
                f"无法覆盖变量 {name}：它不在这一步的上下文里"
            )
        text = value if isinstance(value, str) else str(value)
        if text.strip() and name in json_names:
            try:
                overrides[name] = json.loads(text)
            except json.JSONDecodeError as exc:
                raise OverrideValidationError(
                    f"变量 {name} 的覆盖值不是合法 JSON：{exc}"
                ) from exc
        else:
            overrides[name] = text
    return overrides


async def rerun_step(
    *,
    owner_id: int,
    run_id: int,
    key: str,
    overrides: Optional[Dict[str, str]] = None,
) -> None:
    """重跑一次运行里的单个步骤，跑完按全部步骤行重算运行状态。

    由路由的后台任务调用（``prepare_step_rerun`` 已经把它打回 pending 了）。
    进度不在这儿发：步骤状态本来就是一步一提交，客户端用 :func:`follow_run` 接上。
    ``overrides`` 在这儿按定义重新解析后叠加到上下文，与 prepare 用同一个 helper。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        run = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
            )
        )
        if run is None:
            logger.warning("rerun step %s: run %s has gone", key, run_id)
            return
        definitions = _load_definitions(db, run.workflow_id)
        definition = next((step for step in definitions if step.key == key), None)
        if definition is None:
            settle_run_status(db, owner_id, run_id, error="这条流程的定义里已经没有这一步")
            return
        context = _rebuild_context(db, run, definitions, definition.group_key)
        names, json_names = _context_fields(run, definitions, definition.group_key)
        context.update(_coerce_overrides(overrides, names, json_names))
        step = _detach(db, definition)

        # 单步重跑按「工作流级挂载 ∪ 目标节点挂载」建束——不必为别的节点
        # 挂的服务买单。连接失败就地结算运行，与 execute_run 的前置失败一致。
        workflow_row = db.get(Workflow, run.workflow_id)
        global_ids = list(dict.fromkeys(workflow_row.mcp_server_ids or []))
        global_servers = resolve_servers(db, owner_id, global_ids)
        target_servers = resolve_servers(
            db, owner_id, list(definition.mcp_server_ids or [])
        )
        try:
            bundle = await open_bundle(
                _merge_servers(global_servers, target_servers)
            )
        except Exception as exc:  # noqa: BLE001 - 不能让运行卡在 running
            settle_run_status(db, owner_id, run_id, error=model_error_text(exc))
            return
        step_tool_plan = plan_step_tools(bundle, global_ids, [definition])
        step_tools = step_tool_plan[definition.key]

    queue: asyncio.Queue = asyncio.Queue()
    try:
        try:
            await _execute_step(
                owner_id=owner_id, run_id=run_id, step=step,
                context=context, queue=queue, tools=step_tools,
            )
        finally:
            await bundle.__aexit__(None, None, None)
    except Exception as exc:  # noqa: BLE001 - 不能让运行卡在 running
        logger.warning("rerun step %s of run %s crashed: %s", key, run_id, exc, exc_info=True)
        with SessionLocal() as db:
            settle_run_status(db, owner_id, run_id, error=model_error_text(exc))
        return

    with SessionLocal() as db:
        settle_run_status(db, owner_id, run_id)


async def resume_run(
    *,
    owner_id: int,
    run_id: int,
    start_group_index: int,
    overrides: Optional[Dict[str, str]] = None,
) -> None:
    """从指定组继续跑完一条运行：驱动 :func:`execute_run` 跑到底，事件丢弃。

    由路由的后台任务调用（``prepare_resume`` 已重置后续行）。正文逐行落库，
    客户端的 follow 流直接读表观察进度，所以这里不转发事件。整体兜住异常：
    执行体半路炸了也得给运行一个终态，不能让它永远 running。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        run = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.owner_id == owner_id, WorkflowRun.id == run_id
            )
        )
        if run is None:
            logger.warning("resume run %s has gone", run_id)
            return
        definitions = _load_definitions(db, run.workflow_id)
        groups = _group_order(definitions)
        if not 0 <= start_group_index < len(groups):
            settle_run_status(
                db, owner_id, run_id, error="续跑起点已失效，请重新打开运行记录"
            )
            return
        names, json_names = _context_fields(
            run, definitions, groups[start_group_index][0]
        )
        try:
            coerced = _coerce_overrides(overrides, names, json_names)
        except ValueError as exc:
            settle_run_status(db, owner_id, run_id, error=str(exc))
            return

    try:
        async for _event, _payload in execute_run(
            owner_id=owner_id,
            run_id=run_id,
            start_group_index=start_group_index,
            overrides=coerced,
        ):
            # 进度由 follow 流读表：这里的事件只用于驱动执行，不转发。
            pass
    except Exception as exc:  # noqa: BLE001 - 不能让运行卡在 running
        logger.warning("resume run %s crashed: %s", run_id, exc, exc_info=True)
        with SessionLocal() as db:
            settle_run_status(db, owner_id, run_id, error=model_error_text(exc))
