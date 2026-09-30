"""工作流：多步骤智能体编排、执行与运行现场。

一条工作流 = 若干**步骤组**，组之间顺序执行、组内并行；每个步骤就是一次
:func:`app.services.agent_run.run_agent` 调用。这是把简历、幻灯片、面试那些
「功能里硬编码的一次模型调用」搬进配置的地基。

三件事说清楚：

* **变量替换不做条件判断**。模板只有 ``{{name}}`` 与 ``{{name.字段}}`` 两种引用
  （后者只对 JSON 步骤的解析结果取字段）；「有补充描述才加这一段」这类条件拼装
  由调用方的输入构造代码完成（见 ``services/resume.py`` 的 ``_analysis_inputs``）。
  想让模板表达条件，就得为它发明一门小语言，而那段逻辑本来就是 Python 里更好读、
  更好测的地方。
* **每步现场都落库**。渲染后的 prompt、原始输出、解析结果、耗时、用的哪份模型
  配置，全进 ``workflow_run_step``。以前一个后台任务只在业务行上留个状态，
  「当时模型到底收到了什么」根本问不出来。
* **一条运行认领一个业务对象**。``subject_kind`` / ``subject_ref`` 把运行挂到
  简历、deck、面试题上，业务行的 ``status`` / ``error_msg`` 由此翻译出来（见
  :func:`subject_state`）——以前同一件事在业务表和运行表各存一份，两套状态机
  各自收尾。
* **步骤并发写各自的行**。每个步骤用自己的 session：SQLAlchemy 的 Session
  不是并发安全的，一组并行步骤共用请求 session 会写出互相覆盖的脏数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session

from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import (
    ModelConfig,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowStep,
)
from app.models.schemas import (
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowInputField,
    WorkflowItem,
    WorkflowRunDetail,
    WorkflowRunItem,
    WorkflowRunStepItem,
    WorkflowStepItem,
    WorkflowUpdate,
)
from app.services.agent_run import (
    OUTPUT_JSON,
    OUTPUT_MARKDOWN,
    model_error_text,
    parse_output,
    require_chat_model,
    run_agent,
)
from app.services.agents import BUILTIN_OWNER_ID, AgentService
from app.timeutil import db_now

logger = logging.getLogger("yangvis.workflows")

# 运行与步骤的状态值。跟 ``resume.status`` 一样用裸字符串：实体和 schema
# 里都没有枚举，加一层只让读写两边各多一套转换。
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

# 一次运行为哪个业务对象跑的，写进 ``workflow_run.subject_kind``。值的形状与
# workflow.slug 一致（小写-连字符），业务侧只认这几个名字。
SUBJECT_RESUME = "resume"
SUBJECT_RESUME_COMPARISON = "resume-comparison"
SUBJECT_RESUME_TOOLKIT = "resume-toolkit"
SUBJECT_SLIDE_DECK = "slide-deck"
SUBJECT_INTERVIEW_QUESTION = "interview-question"

# subject_label 的列宽，与实体定义一致：业务标题再长也只存到这儿，超长由
# create_run 统一截，调用方不必各自防着 MySQL 的长度错误。
SUBJECT_LABEL_MAX = 128

# 业务行的状态词表：以前直接长在业务表上，现在由最近一次运行翻译出来。
# 取值一个都没改——前端那几套 statusText 映射不用跟着动。
SUBJECT_UPLOADED = "uploaded"  # 还没有过任何运行
SUBJECT_ANALYZING = "analyzing"
SUBJECT_READY = "ready"
SUBJECT_ERROR = "error"

_RUN_TO_SUBJECT = {
    RUN_PENDING: SUBJECT_ANALYZING,
    RUN_RUNNING: SUBJECT_ANALYZING,
    RUN_SUCCEEDED: SUBJECT_READY,
    RUN_FAILED: SUBJECT_ERROR,
    RUN_CANCELLED: SUBJECT_ERROR,
}

# 并行组内同时执行几个步骤。步骤是「发一次非流式请求然后等」，瓶颈在上游模型
# 和它的并发额度，不是本地 CPU；上限存在是为了别让一次 6 个方向的批量生成
# 把账号的 QPS 打满，连累同一时间别人正在打的对话。
STEP_CONCURRENCY = 4

# 跟随流多久读一次运行表。步骤状态本来就是每步一提交，这里只是把它读出来；
# 一次模型调用要几秒到几十秒，1.2 秒够跟得上，也不会让读变成压力。
FOLLOW_POLL_SECONDS = 1.2

# 运行记录的默认保留天数：比这更早、又不再是谁的当前状态的运行，启动时清掉。
RUN_RETENTION_DAYS = 30


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

# 变量名、步骤 key 与分组名的合法字符。必须与上面 ``_PLACEHOLDER`` 的字符集对齐：
# 一个声明成 ``my-var`` 的变量模板里永远引用不到，静默少喂一段材料比保存时报错
# 难查得多。不含点号——``a.b`` 里的 ``a`` 是变量名，点号后面是它的字段（见
# :func:`_lookup`）。
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``{{a.b.c}}`` 里每一段的形状：对象的键，或数组的下标。形状不对（``a..b``、
# ``a.b-c``）在保存时就说清楚，而不是等运行时报「取不到字段」——那时错误挂在
# 运行记录里，比编辑器里一行红字难定位得多。
_FIELD_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|\d+)$")


# ---- 内置工作流 -------------------------------------------------------------

@dataclass(frozen=True)
class BuiltinInput:
    """内置工作流声明的一个输入变量。"""

    name: str
    label: str
    required: bool = True


@dataclass(frozen=True)
class BuiltinStep:
    """内置工作流的一个步骤。

    ``group_key`` 相同的步骤并行；不同组按各组最小 ``sort_order`` 排序。单步流程
    就是「一步一组的例外情况」，不需要为它另开一条执行路径。
    """

    key: str
    agent_slug: str
    prompt_tpl: str
    group_key: Optional[str] = None
    output_kind: str = OUTPUT_MARKDOWN
    output_keys: Optional[tuple] = None
    output_var: Optional[str] = None
    optional: bool = False
    sort_order: int = 0
    model_config_id: Optional[int] = None


@dataclass(frozen=True)
class BuiltinWorkflow:
    slug: str
    name: str
    description: str
    inputs: tuple[BuiltinInput, ...]
    steps: tuple[BuiltinStep, ...]
    sort_order: int


# 内置工作流随代码 seed，与内置智能体同一套规则：所有人可见、不可直接修改、
# 可复制成自己的版本再改。
#
# 这里只写「框住材料的骨架」——主题、职位描述这些标签和三引号边界；条件段落
# （「有补充说明才加那一段」）由调用方拼成整块变量传进来。于是 prompt 里既看得
# 出这条流程在讲什么，又不会把 Python 里的拼装逻辑抄第二份。
BUILTIN_WORKFLOWS: tuple[BuiltinWorkflow, ...] = (
    BuiltinWorkflow(
        slug="resume-analysis",
        name="简历分析",
        description="读一份简历全文，产出分析报告与改进意见（「办公 · 简历」的单份分析）",
        inputs=(
            BuiltinInput("resume_content", "简历全文"),
            # 条件段落由调用方拼好再传进来：没有补充描述时就是空串。
            BuiltinInput("description_note", "补充描述段落", required=False),
        ),
        steps=(
            BuiltinStep(
                key="analyze",
                agent_slug="resume-analyzer",
                prompt_tpl="{{description_note}}简历全文：\n\"\"\"\n{{resume_content}}\n\"\"\"",
                group_key="analyze",
                output_kind=OUTPUT_JSON,
                output_keys=("report", "suggestions"),
                output_var="analysis",
                sort_order=10,
            ),
        ),
        sort_order=10,
    ),
    BuiltinWorkflow(
        slug="resume-comparison",
        name="简历对比",
        description="把多份简历全文放在一起做横向对比，产出一份对比报告",
        inputs=(
            BuiltinInput("resumes_block", "多份简历全文"),
            BuiltinInput("resume_count", "简历份数"),
        ),
        steps=(
            BuiltinStep(
                key="compare",
                agent_slug="resume-compare",
                prompt_tpl=(
                    "下面共 {{resume_count}} 份候选人的简历全文，请做一次横向对比分析。\n\n"
                    "{{resumes_block}}"
                ),
                group_key="compare",
                output_var="comparison",
                sort_order=10,
            ),
        ),
        sort_order=20,
    ),
    BuiltinWorkflow(
        slug="slide-generation",
        name="幻灯片生成",
        description="把一份材料整理成整份幻灯片的 JSON 结构（「办公 · 幻灯片」的生成）",
        inputs=(
            BuiltinInput("title", "主题"),
            BuiltinInput("description_note", "补充说明段落", required=False),
            BuiltinInput("requirement_note", "用户要求段落", required=False),
            BuiltinInput("source_block", "材料全文段落", required=False),
            BuiltinInput("asset_block", "可用配图清单段落", required=False),
            BuiltinInput("max_pages", "页数上限"),
        ),
        steps=(
            BuiltinStep(
                key="compose",
                agent_slug="slide-architect",
                prompt_tpl=(
                    "主题：{{title}}\n\n"
                    "{{description_note}}{{requirement_note}}{{source_block}}{{asset_block}}"
                    "请依据以上材料产出整份幻灯片的 JSON 结构。"
                    "页数不超过 {{max_pages}} 页。"
                ),
                group_key="compose",
                sort_order=10,
            ),
        ),
        sort_order=30,
    ),
    BuiltinWorkflow(
        slug="interview-answer",
        name="面试参考答案",
        description="按面试背景与题目给出一条参考答案（面试记录页的「生成答案」）",
        inputs=(
            BuiltinInput("company", "应聘公司"),
            BuiltinInput("position", "面试岗位"),
            BuiltinInput("round_note", "轮次说明", required=False),
            BuiltinInput("question", "面试问题"),
            BuiltinInput("answer_note", "求职者当时的回答段落", required=False),
        ),
        steps=(
            BuiltinStep(
                key="answer",
                agent_slug="interview-answerer",
                prompt_tpl=(
                    "应聘公司：{{company}}\n"
                    "面试岗位：{{position}}{{round_note}}\n\n"
                    "面试问题：\n\"\"\"\n{{question}}\n\"\"\"\n\n{{answer_note}}"
                ),
                group_key="answer",
                sort_order=10,
            ),
        ),
        sort_order=40,
    ),
    BuiltinWorkflow(
        slug="toolkit-optimize",
        name="简历优化",
        description="求职助手：按目标岗位重写并优化一份简历",
        inputs=(
            BuiltinInput("position_note", "目标岗位段落", required=False),
            BuiltinInput("resume_content", "简历全文"),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="resume-optimizer",
                prompt_tpl="{{position_note}}简历全文：\n\"\"\"\n{{resume_content}}\n\"\"\"",
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=50,
    ),
    BuiltinWorkflow(
        slug="toolkit-career-match",
        name="职业匹配分析",
        description="求职助手：从简历与个人背景看适合哪些岗位方向",
        inputs=(
            BuiltinInput("resume_block", "简历全文段落", required=False),
            BuiltinInput("background_block", "个人背景段落", required=False),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="career-matcher",
                prompt_tpl="{{resume_block}}{{background_block}}",
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=55,
    ),
    BuiltinWorkflow(
        slug="toolkit-jd-match",
        name="简历匹配审计",
        description="求职助手：拿一份简历对着具体职位描述逐条审计",
        inputs=(
            BuiltinInput("job_description", "职位描述"),
            BuiltinInput("resume_content", "简历全文"),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="jd-match-auditor",
                prompt_tpl=(
                    "职位描述：\n\"\"\"\n{{job_description}}\n\"\"\"\n\n"
                    "简历全文：\n\"\"\"\n{{resume_content}}\n\"\"\""
                ),
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=60,
    ),
    BuiltinWorkflow(
        slug="toolkit-interview-prep",
        name="面试准备策略",
        description="求职助手：按岗位与简历列出一份面试准备清单",
        inputs=(
            BuiltinInput("position", "岗位名称"),
            BuiltinInput("job_description_block", "职位描述段落", required=False),
            BuiltinInput("resume_block", "简历全文段落", required=False),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="interview-coach",
                prompt_tpl="目标岗位：{{position}}\n\n{{job_description_block}}{{resume_block}}",
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=65,
    ),
    BuiltinWorkflow(
        slug="toolkit-portfolio-plan",
        name="证明构建计划",
        description="求职助手：规划能证明目标职位能力的作品与经历",
        inputs=(
            BuiltinInput("position", "目标职位"),
            BuiltinInput("resume_block", "简历全文段落", required=False),
            BuiltinInput("background_block", "个人背景段落", required=False),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="portfolio-planner",
                prompt_tpl="目标职位：{{position}}\n\n{{resume_block}}{{background_block}}",
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=70,
    ),
    BuiltinWorkflow(
        slug="toolkit-salary-negotiation",
        name="薪资最大化框架",
        description="求职助手：围绕一个 offer 给出薪资谈判框架",
        inputs=(
            BuiltinInput("offer_amount", "offer 年薪"),
            BuiltinInput("position_note", "岗位说明", required=False),
            BuiltinInput("notes_note", "补充说明", required=False),
        ),
        steps=(
            BuiltinStep(
                key="report",
                agent_slug="salary-negotiator",
                prompt_tpl=(
                    "我刚刚收到一份工作邀请，年薪：{{offer_amount}}"
                    "{{position_note}}{{notes_note}}"
                ),
                group_key="report",
                sort_order=10,
            ),
        ),
        sort_order=75,
    ),
)


def _steps_of(builtin: BuiltinWorkflow) -> List[dict]:
    """把声明的步骤转成待落库的行（补上 group_key 与 sort_order 的默认值）。"""
    rows = []
    for index, step in enumerate(builtin.steps):
        rows.append(
            {
                "key": step.key,
                "group_key": step.group_key or step.key,
                "sort_order": step.sort_order or (index + 1) * 10,
                "agent_slug": step.agent_slug,
                "prompt_tpl": step.prompt_tpl,
                "output_kind": step.output_kind,
                "output_keys": list(step.output_keys) if step.output_keys else None,
                "output_var": step.output_var,
                "optional": step.optional,
                "model_config_id": step.model_config_id,
            }
        )
    return rows


def seed_builtin_workflows(db: Session) -> None:
    """插入或刷新内置工作流。

    每次启动就地更新：步骤按 ``key`` 对齐，改了的覆盖、消失的删掉，好让 prompt
    与步骤结构的改进随代码一起上线。但 ``enabled`` 保持不动——把一条流程藏起来
    是用户自己的决定，应该扛得过一次部署。

    用户复制出来的同名流程不在改动范围内：那个 slug 属于他，内置项只管
    ``owner_id = 0`` 那一行。
    """
    existing = {
        workflow.slug: workflow
        for workflow in db.scalars(
            select(Workflow).where(Workflow.owner_id == BUILTIN_OWNER_ID)
        ).all()
    }

    created = 0
    for builtin in BUILTIN_WORKFLOWS:
        current = existing.get(builtin.slug)
        if current is None:
            current = Workflow(
                owner_id=BUILTIN_OWNER_ID,
                slug=builtin.slug,
                name=builtin.name,
                description=builtin.description,
                input_schema=[field.__dict__ for field in builtin.inputs],
                is_builtin=True,
                enabled=True,
                sort_order=builtin.sort_order,
            )
            db.add(current)
            db.flush()
            created += 1
        else:
            current.name = builtin.name
            current.description = builtin.description
            current.input_schema = [field.__dict__ for field in builtin.inputs]
            current.sort_order = builtin.sort_order
            current.is_builtin = True

        wanted = _steps_of(builtin)
        stored = {
            step.key: step
            for step in db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_id == current.id)
            ).all()
        }
        for row in wanted:
            current_step = stored.pop(row["key"], None)
            if current_step is None:
                db.add(WorkflowStep(workflow_id=current.id, **row))
            else:
                for field, value in row.items():
                    setattr(current_step, field, value)
        # 代码里删掉的步骤要跟着删：留着它，历史运行记录还能对上，新运行则不会再
        # 被一个已经不存在于定义里的步骤拖住。
        for stale in stored.values():
            db.delete(stale)

    db.commit()
    if created:
        logger.info("seeded %s built-in workflow(s)", created)


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


# ---- 模板 ------------------------------------------------------------------

# 取不到值的哨兵：``None`` 是一种合法的值（JSON 里的 null），不能拿来当缺失。
_MISSING = object()


def render_prompt(tpl: str, context: Dict[str, object]) -> str:
    """把 ``{{name}}`` 与 ``{{name.字段}}`` 替换成上下文里的值。

    引用了取不到的东西直接报错，而不是替换成空串：静悄悄少喂一段简历全文，
    模型照样能编出一份看着挺完整的报告，而那份报告是废的，代价是一整趟调用的
    时间和 token。
    """
    problems: List[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        value = _lookup(context, name)
        if value is _MISSING:
            problems.append(_missing_reason(context, name))
            return match.group(0)
        return _stringify(value)

    rendered = _PLACEHOLDER.sub(replace, tpl)
    if problems:
        raise ValueError(f"模板取值失败：{'、'.join(sorted(set(problems)))}")
    return rendered


def _lookup(context: Dict[str, object], name: str) -> object:
    """按 ``a`` 或 ``a.b.c`` 从上下文取值；取不到返回 :data:`_MISSING`。

    ``{{a}}`` 拿到的永远是这一步的整段产物；点号路径只有 JSON 步骤的产物走得通
    ——执行时进上下文的是它按契约解析出来的对象（见 ``execute_run``），整段文本
    里混着的解释性文字不该被下游当成结构体引用。
    """
    root, _, path = name.partition(".")
    if root not in context:
        return _MISSING
    value = context[root]
    if not path:
        return value
    for segment in path.split("."):
        if isinstance(value, dict):
            if segment not in value:
                return _MISSING
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            # 字符串、数字、越界的下标、``a..b`` 里的空段——都没有再往里走一层的道理。
            return _MISSING
    return value


def _missing_reason(context: Dict[str, object], name: str) -> str:
    """报错时说得出是哪一种取不到：根变量根本没有，还是那条路径走不通。"""
    root, _, path = name.partition(".")
    if root not in context:
        return f"未定义的变量 {root}"
    return f"{name}（{root} 的产物里没有这个字段）"


def _stringify(value: object) -> str:
    """把取到的值变成喂进 prompt 的文本。

    对象与数组按 JSON 序列化，不是 Python 的 repr：单引号和 ``None`` 模型会照抄
    进它的输出里，下游再按 JSON 解析就崩。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _group_order(steps: Sequence[WorkflowStep]) -> List[Tuple[str, List[WorkflowStep]]]:
    """按首次出现顺序把步骤分进并行组。"""
    groups: List[Tuple[str, List[WorkflowStep]]] = []
    index: Dict[str, int] = {}
    for step in steps:
        slot = index.get(step.group_key)
        if slot is None:
            index[step.group_key] = len(groups)
            groups.append((step.group_key, [step]))
        else:
            groups[slot][1].append(step)
    return groups


def _input_schema(fields: Sequence[WorkflowInputField]) -> List[dict]:
    """把声明的输入变量落成 ``workflow.input_schema`` 的 JSON 值。

    标签留空时用变量名兜底：界面上少填一格不该让运行历史里出现一个没有名字的
    变量。形状与内置工作流 seed 出来的完全一致，读的一侧不用分两种情况。
    """
    return [
        {
            "name": field.name,
            "label": field.label or field.name,
            "required": field.required,
        }
        for field in fields
    ]


# 用户自建工作流的展示位次：内置项是功能自己的流水线，排在前面更贴合使用顺序。
_USER_SORT_ORDER = 100


# ---- 定义存取 --------------------------------------------------------------

class WorkflowService:
    """一个用户能看到的工作流：共享的内置项，加上他自己的。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._agents = AgentService(db, owner_id)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(Workflow.owner_id.in_((BUILTIN_OWNER_ID, self._owner_id)))

    def list(self, *, enabled_only: bool = False) -> List[Workflow]:
        stmt = self._scope(select(Workflow))
        if enabled_only:
            stmt = stmt.where(Workflow.enabled.is_(True))
        return list(self._db.scalars(stmt.order_by(Workflow.sort_order, Workflow.id)).all())

    def get(self, workflow_id: int) -> Workflow:
        row = self._db.scalar(self._scope(select(Workflow)).where(Workflow.id == workflow_id))
        if row is None:
            # 404 而不是 403：确认别人的工作流存在本身就是泄露。
            raise LookupError("工作流不存在")
        return row

    def get_by_slug(self, slug: str) -> Optional[Workflow]:
        return self._db.scalar(
            self._scope(select(Workflow)).where(Workflow.slug == slug)
        )

    def require_by_slug(self, slug: str) -> Workflow:
        """取功能自带的那条内置流程（简历、幻灯片这类后台调用的）。

        与智能体同一条规则：用户停用了它照样能用——那是功能自己的流水线，
        不是界面上的可选项。查不到说明 seed 没跑过，是初始化损坏的信号。
        """
        workflow = self.get_by_slug(slug)
        if workflow is None:
            logger.warning("built-in workflow %s is missing", slug)
            raise BusinessError(
                CODE_CHAT_NOT_READY, f"内置工作流 {slug} 缺失，请重启服务重新初始化"
            )
        return workflow

    def require_chat_config(self) -> ModelConfig:
        """发起运行前的门槛：与简历/幻灯片同一份默认对话模型检查。

        放在建运行记录**之前**：没配模型就落一行 pending，它永远跑不起来，只能
        等下次启动被收尾成失败——那是一条没人发起过也解释不了的运行。
        """
        return require_chat_model(self._db, self._owner_id)

    def steps_of(self, workflow_id: int) -> List[WorkflowStep]:
        return list(
            self._db.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_id == workflow_id)
                .order_by(WorkflowStep.sort_order, WorkflowStep.id)
            ).all()
        )

    def to_item(self, workflow: Workflow) -> WorkflowItem:
        steps = self.steps_of(workflow.id)
        agent_names = self._agent_names(step.agent_slug for step in steps)
        return WorkflowItem(
            id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            description=workflow.description,
            inputs=[WorkflowInputField(**field) for field in (workflow.input_schema or [])],
            steps=[
                WorkflowStepItem(
                    key=step.key,
                    group_key=step.group_key,
                    sort_order=step.sort_order,
                    agent_slug=step.agent_slug,
                    agent_name=agent_names.get(step.agent_slug),
                    prompt_tpl=step.prompt_tpl,
                    output_kind=step.output_kind,
                    output_keys=list(step.output_keys) if step.output_keys else None,
                    output_var=step.output_var,
                    model_config_id=step.model_config_id,
                    optional=step.optional,
                )
                for step in steps
            ],
            is_builtin=workflow.is_builtin,
            enabled=workflow.enabled,
            sort_order=workflow.sort_order,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )

    def _agent_names(self, slugs) -> Dict[str, str]:
        """把步骤引用到的人设名取出来，省得界面为每个步骤再查一次。"""
        wanted = {slug for slug in slugs}
        if not wanted:
            return {}
        return {
            agent.slug: agent.name
            for agent in self._agents.list()
            if agent.slug in wanted
        }

    # -- 定义编辑 -------------------------------------------------------------

    def _owned(self, workflow_id: int) -> Workflow:
        """取出调用者有权修改的工作流。"""
        workflow = self.get(workflow_id)
        if workflow.is_builtin or workflow.owner_id != self._owner_id:
            raise PermissionError("内置工作流不可修改，请复制后再编辑")
        return workflow

    def create(self, payload: WorkflowCreate) -> Workflow:
        if self.get_by_slug(payload.slug) is not None:
            raise ValueError("标识已存在")

        steps = self._normalize(payload)
        workflow = Workflow(
            owner_id=self._owner_id,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            input_schema=_input_schema(payload.inputs),
            is_builtin=False,
            enabled=True,
            sort_order=_USER_SORT_ORDER,
        )
        self._db.add(workflow)
        self._db.flush()
        for step in steps:
            step.workflow_id = workflow.id
            self._db.add(step)
        self._db.commit()
        self._db.refresh(workflow)
        logger.info(
            "owner %s created workflow %s (%s, %d step(s))",
            self._owner_id,
            workflow.slug,
            workflow.id,
            len(steps),
        )
        return workflow

    def update(self, workflow_id: int, payload: WorkflowUpdate) -> Workflow:
        workflow = self._owned(workflow_id)
        steps = self._normalize(payload)

        workflow.name = payload.name
        workflow.description = payload.description
        workflow.input_schema = _input_schema(payload.inputs)
        if payload.enabled is not None:
            workflow.enabled = payload.enabled

        # 步骤整份重建，不逐行对齐：跨行的约束（模板只能引用前面组的产物）在
        # 「只改了这一行」的视角里判不出改完还合不合法。删除先执行、新行后 add，
        # (workflow_id, key) 的唯一索引才不会撞。
        self._db.execute(
            delete(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id)
        )
        for step in steps:
            step.workflow_id = workflow.id
            self._db.add(step)
        self._db.commit()
        self._db.refresh(workflow)
        logger.info(
            "owner %s updated workflow %s (%s, %d step(s))",
            self._owner_id,
            workflow.slug,
            workflow.id,
            len(steps),
        )
        return workflow

    def delete(self, workflow_id: int) -> None:
        """删掉定义和它的步骤行；运行记录留着。

        一次运行是「当时那份定义」的现场，定义没了也该看得懂——``workflow_run``
        上冗余的 ``workflow_slug`` 与每步的 prompt 快照就是为这一刻。
        """
        workflow = self._owned(workflow_id)
        slug = workflow.slug
        self._db.execute(
            delete(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id)
        )
        self._db.delete(workflow)
        self._db.commit()
        logger.info("owner %s deleted workflow %s (%s)", self._owner_id, slug, workflow_id)

    def duplicate(self, workflow_id: int) -> Workflow:
        """复制到调用者名下——定制内置工作流的唯一途径。"""
        source = self.get(workflow_id)
        slug = f"{source.slug}-copy"
        suffix = 2
        while self.get_by_slug(slug) is not None:
            slug = f"{source.slug}-copy{suffix}"
            suffix += 1

        clone = Workflow(
            owner_id=self._owner_id,
            slug=slug,
            name=f"{source.name} 副本",
            description=source.description,
            # 逐份拷 dict：直接引用同一个 list 实例，两条行的 JSON 会互相影响。
            input_schema=[dict(field) for field in (source.input_schema or [])],
            is_builtin=False,
            enabled=True,
            sort_order=_USER_SORT_ORDER,
        )
        self._db.add(clone)
        self._db.flush()
        for step in self.steps_of(source.id):
            self._db.add(
                WorkflowStep(
                    workflow_id=clone.id,
                    key=step.key,
                    group_key=step.group_key,
                    sort_order=step.sort_order,
                    agent_slug=step.agent_slug,
                    prompt_tpl=step.prompt_tpl,
                    output_kind=step.output_kind,
                    output_keys=list(step.output_keys) if step.output_keys else None,
                    output_var=step.output_var,
                    model_config_id=step.model_config_id,
                    optional=step.optional,
                )
            )
        self._db.commit()
        self._db.refresh(clone)
        logger.info(
            "owner %s duplicated workflow %s as %s (%s)",
            self._owner_id,
            source.slug,
            clone.slug,
            clone.id,
        )
        return clone

    def _normalize(self, payload: WorkflowDefinition) -> List[WorkflowStep]:
        """校验整份定义，产出待落库的步骤行（数组顺序就是执行顺序）。

        这里挡下来的全是**跑起来必然失败**的写法：引用没声明的变量、引用同组
        并行步骤的产物、指向看不见的人设、选了不属于自己的模型配置。保存时报错
        只花一次请求；放过去就是一次运行的时间和 token，外加运行历史里一行谁也
        看不懂原因的失败。
        """
        declared: List[str] = []
        for field in payload.inputs:
            if not _NAME.match(field.name):
                raise ValueError(
                    f"输入变量名 {field.name} 不合法：只能用字母、下划线和数字，且不能以数字开头"
                )
            if field.name in declared:
                raise ValueError(f"输入变量重复：{field.name}")
            declared.append(field.name)

        steps: List[WorkflowStep] = []
        keys: List[str] = []
        output_vars: List[str] = []
        for index, step in enumerate(payload.steps, start=1):
            where = f"第 {index} 步"
            for label, value in (("步骤标识", step.key), ("并行分组", step.group_key)):
                if not _NAME.match(value):
                    raise ValueError(
                        f"{where}的{label} {value} 不合法：只能用字母、下划线和数字，且不能以数字开头"
                    )
            if step.key in keys:
                raise ValueError(f"步骤标识重复：{step.key}")
            keys.append(step.key)

            if step.output_kind not in (OUTPUT_MARKDOWN, OUTPUT_JSON):
                raise ValueError(f"{where}的输出形态只支持 markdown 与 json")
            if step.output_kind != OUTPUT_JSON and step.output_keys:
                raise ValueError(f"{where}的输出不是 json，声明期望字段不会生效")
            if step.output_var:
                if not _NAME.match(step.output_var):
                    raise ValueError(
                        f"{where}的产物变量名 {step.output_var} 不合法：只能用字母、下划线和数字，且不能以数字开头"
                    )
                if step.output_var in output_vars:
                    raise ValueError(f"产物变量名重复：{step.output_var}")
                output_vars.append(step.output_var)

            if self._agents.get_by_slug(step.agent_slug) is None:
                raise ValueError(f"{where}引用了不存在的智能体 {step.agent_slug}")
            # 与运行同一条检查：宁可保存时就告诉用户「这份配置没测通」，也别等
            # 一次运行跑到这一步才失败。BusinessError（4004）照原样抛给接口层。
            if step.model_config_id is not None:
                try:
                    require_chat_model(self._db, self._owner_id, step.model_config_id)
                except LookupError as exc:
                    raise ValueError(f"{where}的模型配置不存在") from exc

            steps.append(
                WorkflowStep(
                    # workflow_id 先占 0，落库前才补真值：校验失败时这些实例从未
                    # 进过 session，不会留下半成品。
                    workflow_id=0,
                    key=step.key,
                    group_key=step.group_key,
                    sort_order=index * 10,
                    agent_slug=step.agent_slug,
                    prompt_tpl=step.prompt_tpl,
                    output_kind=step.output_kind,
                    output_keys=list(step.output_keys) if step.output_keys else None,
                    # 空串按「没有产物变量」处理：否则界面上清空一格就在库里留下
                    # 一个既不是 NULL 也不是名字的值。
                    output_var=step.output_var or None,
                    model_config_id=step.model_config_id,
                    optional=step.optional,
                )
            )

        # 分组必须连续：引擎按 group_key 首次出现归组，同名组被别的组隔开时，
        # 编辑器里看到的顺序和实际执行顺序就不是一回事了。
        placed: List[str] = []
        for step in steps:
            if placed and placed[-1] != step.group_key and step.group_key in placed:
                raise ValueError(
                    f"并行分组 {step.group_key} 的步骤不连续：同组的步骤请放在一起"
                )
            placed.append(step.group_key)

        self._check_template_vars(steps, declared)
        return steps

    @staticmethod
    def _check_template_vars(steps: List[WorkflowStep], declared: List[str]) -> None:
        """``{{name}}`` 的根必须是声明过的输入或**前面并行组**的产物；``{{name.字段}}`` 只允许挂在 JSON 步骤的产物上。"""
        known = set(declared)
        # 只有 JSON 步骤的产物进上下文时带着解析结果，才有点号可取（见 execute_run）。
        json_vars = {
            step.output_var
            for step in steps
            if step.output_var and step.output_kind == OUTPUT_JSON
        }
        for _, members in _group_order(steps):
            in_group = {step.output_var: step.key for step in members if step.output_var}
            for step in members:
                for name in _PLACEHOLDER.findall(step.prompt_tpl):
                    root, _, path = name.partition(".")
                    if root not in known:
                        if root in in_group:
                            raise ValueError(
                                f"步骤 {step.key} 引用了同组步骤 {in_group[root]} 的产物 {root}："
                                "同一组是并行执行的，那一步这时还没有输出"
                            )
                        raise ValueError(
                            f"步骤 {step.key} 的模板引用了未定义的变量 {root}："
                            "它既不是这条流程声明的输入，也不是前面步骤的产物"
                        )
                    if "." not in name:
                        continue
                    if root not in json_vars:
                        raise ValueError(
                            f"步骤 {step.key} 的模板引用了 {name}：{root} 是整段文本，"
                            f"没有字段可取，请引用整段 {{{{{root}}}}}"
                        )
                    broken = [part for part in path.split(".") if not _FIELD_SEGMENT.match(part)]
                    if broken:
                        raise ValueError(
                            f"步骤 {step.key} 的模板引用了 {name}：字段名 {broken[0]!r} 不合法，"
                            "只能写字母开头的键或数组下标"
                        )
            known |= set(in_group)

    # -- 发起运行 -------------------------------------------------------------

    def create_run(
        self,
        workflow_id: int,
        inputs: Dict[str, str],
        *,
        subject_kind: Optional[str] = None,
        subject_ref: Optional[object] = None,
        subject_label: Optional[str] = None,
    ) -> WorkflowRun:
        """校验输入、落一条运行与每步的 ``pending`` 行。

        步骤行在这里一次性建齐：这样「六个方向跑到第三页崩了」在运行历史里
        看得见全貌（前两页成功、第三页失败、后面三页 pending/skipped），
        而不是只有已执行的那几步。

        ``subject_kind`` / ``subject_ref`` 把这次运行认领给一个业务对象（见
        :func:`subject_state`）：功能侧发起的流水线都该带上，否则业务行的状态
        没有出处。``subject_label`` 是那个对象当时叫什么——运行记录要能自己说清
        「这次是给谁跑的」，业务行删了也还得看得懂。
        """
        workflow = self.get(workflow_id)
        # 与内置智能体同一条规则：内置流程是功能自己的流水线，用户把它从界面上
        # 藏起来之后，那个功能照常要能跑。
        if not workflow.is_builtin and not workflow.enabled:
            raise ValueError("该工作流已停用")

        steps = self.steps_of(workflow_id)
        if not steps:
            raise ValueError("该工作流没有可执行的步骤")

        declared = {field["name"] for field in (workflow.input_schema or [])}
        # 声明过但没传的（可选）变量补成空串：上下文按声明闭合，这样模板里
        # 「未定义的变量」永远意味着写错了，而不是调用方少传了一个可选字段。
        given: Dict[str, str] = {name: "" for name in declared}
        given.update({key: (value or "") for key, value in inputs.items()})
        missing = [
            field["name"]
            for field in (workflow.input_schema or [])
            if field.get("required", True) and not given.get(field["name"], "").strip()
        ]
        if missing:
            labels = {
                field["name"]: field.get("label") or field["name"]
                for field in (workflow.input_schema or [])
            }
            raise ValueError(f"缺少必填输入：{'、'.join(labels[name] for name in missing)}")
        unknown = sorted(set(given) - declared)
        if unknown:
            # 拒绝而不是忽略：拼错的变量名如果静默消失，喂给模型的就是一段缺料的
            # prompt，而报告看起来一切正常。
            raise ValueError(f"未知的输入变量：{'、'.join(unknown)}")

        run = WorkflowRun(
            owner_id=self._owner_id,
            workflow_id=workflow.id,
            workflow_slug=workflow.slug,
            subject_kind=subject_kind,
            # 业务主键统一按字符串存：整数 id 与面试题的 qid 走同一个索引。
            subject_ref=None if subject_ref is None else str(subject_ref),
            # 空串归一成 NULL（「没有归属」），超长截到列宽：各功能的标题字段
            # 长度谁也没保证，集中在这儿截比每个调用方各写一份可靠。
            subject_label=(subject_label or "").strip()[:SUBJECT_LABEL_MAX] or None,
            inputs=given,
            status=RUN_PENDING,
        )
        self._db.add(run)
        self._db.flush()
        for step in steps:
            self._db.add(
                WorkflowRunStep(
                    owner_id=self._owner_id,
                    run_id=run.id,
                    key=step.key,
                    group_key=step.group_key,
                    agent_slug=step.agent_slug,
                    sort_order=step.sort_order,
                    status=STEP_PENDING,
                )
            )
        self._db.commit()
        self._db.refresh(run)
        logger.info(
            "owner %s started workflow run %s (%s, %d step(s))",
            self._owner_id,
            run.id,
            workflow.slug,
            len(steps),
        )
        return run

    # -- 运行记录 -------------------------------------------------------------

    def list_runs(
        self,
        workflow_id: Optional[int] = None,
        *,
        subject: Optional[Tuple[str, object]] = None,
        run_status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[WorkflowRun], int]:
        """一页运行记录与**匹配条件**的总条数，新的在前。

        总数按同一套条件算：界面上的「共 N 次」要说的是筛出来的那批，而不是
        当页有几条——按 ``limit`` 报回去的话，翻到第二页数字就变了。
        """
        stmt = select(WorkflowRun).where(WorkflowRun.owner_id == self._owner_id)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
        # 按业务对象收敛：功能页面的「运行记录」入口要的是「这份简历跑过哪几次」，
        # 不是全站流水。kind 与 ref 必须成对——整数 id 在各张表上是各自编号的，
        # 只按 ref 筛会把「简历 3」和「deck 3」的运行混到一起。
        if subject is not None:
            stmt = stmt.where(
                WorkflowRun.subject_kind == subject[0],
                WorkflowRun.subject_ref == str(subject[1]),
            )
        if run_status is not None:
            stmt = stmt.where(WorkflowRun.status == run_status)
        total = int(self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        items = list(
            self._db.scalars(
                stmt.order_by(WorkflowRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        return items, total

    def _reopen_for_read(self) -> None:
        """结束当前读事务，让下一次 SELECT 拿新快照。

        运行与步骤的行是**执行它的那条连接**提交的：同进程里发起运行的调用方
        （简历分析就是）往往还持有自己更早的快照，而身份映射里的旧副本不会自动
        作废——不重开事务的话，它会一直读到 ``pending``，把一次成功的执行当成失败。
        """
        self._db.commit()

    def get_run(self, run_id: int) -> WorkflowRun:
        self._reopen_for_read()
        run = self._db.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.owner_id == self._owner_id, WorkflowRun.id == run_id)
        )
        if run is None:
            raise LookupError("运行记录不存在")
        return run

    def run_steps(self, run_id: int) -> List[WorkflowRunStep]:
        self._reopen_for_read()
        return list(
            self._db.scalars(
                select(WorkflowRunStep)
                .where(
                    WorkflowRunStep.owner_id == self._owner_id,
                    WorkflowRunStep.run_id == run_id,
                )
                .order_by(WorkflowRunStep.sort_order, WorkflowRunStep.id)
            ).all()
        )

    def step_models(self, run_ids: Sequence[int]) -> List[Tuple[int, int, str]]:
        """这些运行的每一步实际用到的模型，形状是 ``(run_id, step.id, 模型名)``。

        一次问完整页：列表一页 20 次运行，按行逐条查就是 20 次往返，而这就是
        「运行记录打开慢」的全部原因。没跑到的步骤没有模型可报，配置在跑完后被
        删掉的也报不出名字，两种都不出现在结果里。
        """
        ids = list(run_ids)
        if not ids:
            return []
        rows = self._db.execute(
            select(WorkflowRunStep.run_id, WorkflowRunStep.id, ModelConfig.model_name)
            .outerjoin(ModelConfig, ModelConfig.id == WorkflowRunStep.model_config_id)
            .where(
                WorkflowRunStep.owner_id == self._owner_id,
                WorkflowRunStep.run_id.in_(ids),
            )
            .order_by(WorkflowRunStep.run_id, WorkflowRunStep.sort_order, WorkflowRunStep.id)
        ).all()
        return [(run_id, step_id, name) for run_id, step_id, name in rows if name]

    def run_model_names(self, run_ids: Sequence[int]) -> Dict[int, List[str]]:
        """按运行聚合 :meth:`step_models`：各步用到的模型，按步骤顺序去重。"""
        grouped: Dict[int, List[str]] = {}
        for run_id, _, model_name in self.step_models(run_ids):
            grouped.setdefault(run_id, []).append(model_name)
        return {
            run_id: list(dict.fromkeys(names)) for run_id, names in grouped.items()
        }

    @staticmethod
    def to_run_item(run: WorkflowRun, model_names: Sequence[str] = ()) -> WorkflowRunItem:
        return WorkflowRunItem(
            id=run.id,
            workflow_id=run.workflow_id,
            workflow_slug=run.workflow_slug,
            subject_kind=run.subject_kind,
            subject_ref=run.subject_ref,
            subject_label=run.subject_label,
            status=run.status,
            error_msg=run.error_msg,
            elapsed_ms=run.elapsed_ms,
            model_names=list(model_names),
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )

    def to_run_detail(self, run: WorkflowRun) -> WorkflowRunDetail:
        """运行详情：带输入快照与每一步的现场（含渲染后的 prompt）。

        步骤行存的是当时的现场，「这一步可选吗、要什么格式、是谁的人设」长在定义上，
        所以这里按 key 对一次：对得上就带上，对不上（定义后来改过、步骤已删）就按
        行本身显示——现场的主人是那次运行，不是当前的定义。
        """
        rows = self.run_steps(run.id)
        definitions = {step.key: step for step in self.steps_of(run.workflow_id)}
        agent_names = self._agent_names(row.agent_slug for row in rows)
        # 模型一次查出两种用法：行上给的是去重后的聚合，现场里按步给各自用的那个。
        used = self.step_models([run.id])
        model_names = {step_id: name for _, step_id, name in used}
        return WorkflowRunDetail(
            **self.to_run_item(run, list(dict.fromkeys(name for _, _, name in used))).model_dump(),
            inputs=dict(run.inputs or {}),
            steps=[
                WorkflowRunStepItem(
                    id=row.id,
                    key=row.key,
                    group_key=row.group_key,
                    agent_slug=row.agent_slug,
                    agent_name=agent_names.get(row.agent_slug),
                    output_kind=definitions[row.key].output_kind if row.key in definitions else None,
                    optional=definitions[row.key].optional if row.key in definitions else False,
                    status=row.status,
                    prompt=row.prompt,
                    output=row.output,
                    parsed=row.parsed,
                    error_msg=row.error_msg,
                    model_config_id=row.model_config_id,
                    # 这一步当时用的是哪个模型：现场里每一步各用自己的（按步骤覆盖
                    # 与默认模型混着跑的流程，只有行级聚合说不清）。
                    model_name=model_names.get(row.id),
                    elapsed_ms=row.elapsed_ms,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ],
        )


# ---- 运行 ↔ 业务对象 --------------------------------------------------------

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


# ---- 执行 ------------------------------------------------------------------

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


# 队列里标记「这一组跑完了」的哨兵，与事件名区分开。
_GROUP_DONE = "__group_done__"


async def _execute_step(
    *,
    owner_id: int,
    run_id: int,
    step: WorkflowStep,
    context: Dict[str, object],
    queue: asyncio.Queue,
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

        try:
            # 落这一步**实际**用的模型：定义里的 model_config_id 只写按步骤的覆盖，
            # 空着的是运行时的默认对话模型。历史行只存前者的话，绝大多数运行在
            # 界面上就是「没用什么模型」。
            config = require_chat_model(db, owner_id, step.model_config_id)
            row.model_config_id = config.id
            db.commit()
            text = await run_agent(
                db,
                owner_id,
                step.agent_slug,
                prompt,
                config_id=config.id,
            )
            plain, parsed = parse_output(text, step.output_kind, step.output_keys)
        except Exception as exc:  # noqa: BLE001 - 步骤的失败就是这一步的状态
            logger.warning(
                "workflow step %s of run %s failed: %s", step.key, run_id, exc, exc_info=True
            )
            return await _finish_step(db, row, step, queue, model_error_text(exc), started)

        return await _finish_step(
            db, row, step, queue, None, started, output=plain, parsed=parsed
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
    await queue.put(("step_end", _step_event(outcome, elapsed_ms=elapsed_ms, parsed=parsed)))
    return outcome


def _step_event(
    outcome: _StepOutcome, *, elapsed_ms: Optional[int] = None, parsed: Optional[dict] = None
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
    }


async def execute_run(
    *,
    owner_id: int,
    run_id: int,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
) -> AsyncIterator[Tuple[str, object]]:
    """执行一次已经建好的运行，产出 ``(event, payload)`` 供 SSE 转发。

    事件顺序：``run_start`` →（每组）若干 ``step_start`` / ``step_end`` →
    ``run_end``。并行组内的事件按各步骤实际完成的先后到达，不保证成对相邻。

    每步的写入都是即时提交的，所以进度既不依赖这条流（换设备打开历史详情也
    能看到跑到哪了），也不会因为最后一起提交而整段丢失。

    ``is_disconnected`` 与对话的流式回答同一用法：用户关掉页面就停在这里，
    已经完成的步骤保留它们的终态，没跑的标成 ``skipped``。
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

        # 模型门槛提前测：没配好对话模型时，第一条事件就该是明确的「去配置」，
        # 而不是让每个步骤各自失败一遍。按步骤指定的配置在步骤里各自校验。
        require_chat_model(db, owner_id)

        run.status = RUN_RUNNING
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

        context: Dict[str, object] = dict(run.inputs or {})
        queue: asyncio.Queue = asyncio.Queue()
        started = time.monotonic()
        failed: Optional[str] = None
        aborted = False

        for group_key, group in _group_order(steps):
            if is_disconnected is not None and await is_disconnected():
                aborted = True
                break

            async def _drive(members: List[WorkflowStep]) -> None:
                results = await asyncio.gather(
                    *[
                        _execute_step(
                            owner_id=owner_id, run_id=run_id, step=member,
                            context=context, queue=queue,
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
        run.status = RUN_CANCELLED if aborted else (RUN_FAILED if failed else RUN_SUCCEEDED)
        if aborted:
            # 取消也写个原因：业务行的状态现在是从这条运行翻译出来的，一个没有任何
            # 说明的失败只会让用户看到「失败」两个字。
            run.error_msg = "页面已关闭，本次运行已中止"
        else:
            run.error_msg = failed
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        run.finished_at = db_now()
        db.commit()

        if aborted:
            # 不发 run_end：客户端已经走了，这里的意义就在于把状态收干净。
            return

        yield "run_end", {
            "run_id": run_id,
            "status": run.status,
            "error": run.error_msg,
            "elapsed_ms": run.elapsed_ms,
        }


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
    steps = {
        row.key: {
            "status": row.status,
            "group_key": row.group_key,
            "agent_slug": row.agent_slug,
            "error": row.error_msg,
            "output_chars": len(row.output or ""),
            "elapsed_ms": row.elapsed_ms,
        }
        for row in db.scalars(
            select(WorkflowRunStep)
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

    运行进终态就收流；``is_disconnected`` 与 :func:`execute_run` 同一用法。
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


def prepare_step_rerun(db: Session, owner_id: int, run_id: int, key: str) -> WorkflowRun:
    """把一次终态运行里的那一步打回待运行，现场交给调用方的后台任务去跑。

    闸门都在这儿：还在跑的运行不能重发（两个执行体会抢同一行），定义里已经没有的
    步骤不能重跑——prompt 模板只存在于定义里，步骤行只存渲染出来的那一份。
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
    definition = db.scalar(
        select(WorkflowStep).where(
            WorkflowStep.workflow_id == run.workflow_id, WorkflowStep.key == key
        )
    )
    if definition is None:
        raise ValueError("这条流程的定义里已经没有这一步，没有模板可以重跑")

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


def _rerun_context(db: Session, run: WorkflowRun, target_key: str) -> Dict[str, object]:
    """按当时落库的上游产物重建这一步的上下文。

    只带目标步骤**之前**那些组的输出：同组并行步骤第一次跑的时候也看不见彼此的
    产物，重跑要复刻的就是当时那张 prompt，不能因为这次跑得晚就多喂一段。
    """
    definitions = list(
        db.scalars(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == run.workflow_id)
            .order_by(WorkflowStep.sort_order, WorkflowStep.id)
        ).all()
    )
    by_key = {step.key: step for step in definitions}
    target = by_key.get(target_key)
    earlier = []
    for group_key, _members in _group_order(definitions):
        if target is None or group_key == target.group_key:
            break
        earlier.append(group_key)

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


async def rerun_step(*, owner_id: int, run_id: int, key: str) -> None:
    """重跑一次运行里的单个步骤，跑完按全部步骤行重算运行状态。

    由路由的后台任务调用（``prepare_step_rerun`` 已经把它打回 pending 了）。
    进度不在这儿发：步骤状态本来就是一步一提交，客户端用 :func:`follow_run` 接上。
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
        definition = db.scalar(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == run.workflow_id, WorkflowStep.key == key
            )
        )
        if definition is None:
            settle_run_status(db, owner_id, run_id, error="这条流程的定义里已经没有这一步")
            return
        context = _rerun_context(db, run, key)
        step = _detach(db, definition)

    queue: asyncio.Queue = asyncio.Queue()
    try:
        await _execute_step(
            owner_id=owner_id, run_id=run_id, step=step, context=context, queue=queue
        )
    except Exception as exc:  # noqa: BLE001 - 不能让运行卡在 running
        logger.warning("rerun step %s of run %s crashed: %s", key, run_id, exc, exc_info=True)
        with SessionLocal() as db:
            settle_run_status(db, owner_id, run_id, error=model_error_text(exc))
        return

    with SessionLocal() as db:
        settle_run_status(db, owner_id, run_id)


def start_builtin(
    db: Session,
    owner_id: int,
    slug: str,
    inputs: Dict[str, str],
    *,
    subject: Tuple[str, object],
    subject_label: Optional[str] = None,
) -> WorkflowRun:
    """功能侧发起一条内置流程：在请求里把运行建出来，交给后台任务去跑。

    与 :func:`finish_builtin_step` 拆成两步，是为了让「这次任务的状态」在请求返回时就
    已经落库：以前 ``start_analysis`` 往业务行写一个 ``analyzing``、后台任务再往
    运行表写一份，两边各是各的事实源。现在认领了这个业务对象（``subject``）的运行
    就是它的状态本身，重启后的收尾也只有 ``reset_stale_runs`` 一处。

    ``subject_label`` 带上那个对象当时叫什么，运行记录才在「工作流 → 运行」的
    流水里也说得出自己是给谁跑的。
    """
    workflows = WorkflowService(db, owner_id)
    workflow = workflows.require_by_slug(slug)
    return workflows.create_run(
        workflow.id,
        inputs,
        subject_kind=subject[0],
        subject_ref=subject[1],
        subject_label=subject_label,
    )


async def finish_builtin_step(
    db: Session,
    owner_id: int,
    run_id: int,
    *,
    step_key: Optional[str] = None,
) -> WorkflowRunStep:
    """跑完一条内置工作流，返回它产出结果的那一步。

    行上两样都给：``output`` 是模型的原始文本，``parsed`` 是按这一步声明的输出
    契约解析出来的对象（markdown 步没有对象，是 ``None``）。后处理直接读 ``parsed``
    就行——引擎已经剥过代码栅栏、核对过期望字段，这里再解析一遍只是把同一件事做两次。

    功能侧（简历、幻灯片、面试、求职助手）调一次模型的路径统一到这里：同样的调用
    先落一条运行，步骤的渲染结果、原始输出、耗时都进 ``workflow_run_step``——
    「当时模型到底收到了什么」从此问得出来，prompt 与模型也就能在「工作流」页面上
    改了。

    失败抛 :class:`RuntimeError`，原因已经在运行记录上；调用方的**后处理**挂了
    （清洗页面结构那类）时用 :func:`fail_run` 把运行降级，别指望这里替它兜。

    ``step_key`` 省略时取最后一个成功步骤：内置流程基本都是单步，调用方不必
    跟着定义里的 key 改一遍。
    """
    workflows = WorkflowService(db, owner_id)
    run = workflows.get_run(run_id)

    async for event, payload in execute_run(owner_id=owner_id, run_id=run_id):
        if event == "step_end" and payload.get("error"):
            logger.info(
                "workflow %s step %s failed: %s",
                run.workflow_slug,
                payload.get("key"),
                payload["error"],
            )

    finished = workflows.get_run(run_id)
    if finished.status != RUN_SUCCEEDED:
        raise RuntimeError(finished.error_msg or "工作流执行失败")

    rows = [row for row in workflows.run_steps(run_id) if row.status == STEP_SUCCEEDED]
    step = next(
        (row for row in reversed(rows) if step_key is None or row.key == step_key), None
    )
    if step is None:
        raise RuntimeError(f"工作流 {finished.workflow_slug} 没有产出可用的结果")
    return step


async def builtin_output(
    db: Session,
    owner_id: int,
    run_id: int,
    *,
    step_key: Optional[str] = None,
) -> str:
    """只要产物文本的调用方用这个；要按输出契约拿对象就选 :func:`finish_builtin_step`。"""
    return (await finish_builtin_step(db, owner_id, run_id, step_key=step_key)).output or ""

