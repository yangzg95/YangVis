"""工作流：多步骤智能体编排、执行与运行现场。

一条工作流 = 若干**步骤组**，组之间顺序执行、组内并行；每个步骤就是一次
:func:`app.services.agent_run.run_agent` 调用。这是把简历、幻灯片、面试那些
「功能里硬编码的一次模型调用」搬进配置的地基。

三件事说清楚：

* **变量替换不做条件判断**。模板只有 ``{{name}}`` 这一种语法；「有补充描述才
  加这一段」这类条件拼装由调用方的输入构造代码完成（见 ``services/resume.py``
  的 ``_analysis_inputs``）。想让模板表达条件，就得为它发明一门小语言，而那段
  逻辑本来就是 Python 里更好读、更好测的地方。
* **每步现场都落库**。渲染后的 prompt、原始输出、解析结果、耗时、用的哪份模型
  配置，全进 ``workflow_run_step``。以前一个后台任务只在业务行上留个状态，
  「当时模型到底收到了什么」根本问不出来。
* **步骤并发写各自的行**。每个步骤用自己的 session：SQLAlchemy 的 Session
  不是并发安全的，一组并行步骤共用请求 session 会写出互相覆盖的脏数据。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

from sqlalchemy import Select, delete, select, update
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

# 并行组内同时执行几个步骤。步骤是「发一次非流式请求然后等」，瓶颈在上游模型
# 和它的并发额度，不是本地 CPU；上限存在是为了别让一次 6 个方向的批量生成
# 把账号的 QPS 打满，连累同一时间别人正在打的对话。
STEP_CONCURRENCY = 4

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

# 变量名、步骤 key 与分组名的合法字符。必须与上面 ``_PLACEHOLDER`` 的字符集对齐：
# 一个声明成 ``my-var`` 的变量模板里永远引用不到，静默少喂一段材料比保存时报错
# 难查得多。不含点号——``a.b`` 这种字段访问目前是未实现能力，不该当成合法名字。
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        .values(status=RUN_FAILED, error_msg=STALE_RUN_ERROR, finished_at=datetime.now(timezone.utc))
    )
    db.execute(
        update(WorkflowRunStep)
        .where(WorkflowRunStep.run_id.in_(run_ids))
        .where(WorkflowRunStep.status.in_((STEP_PENDING, STEP_RUNNING)))
        .values(status=STEP_FAILED, error_msg=STALE_RUN_ERROR, finished_at=datetime.now(timezone.utc))
    )
    db.commit()
    logger.info("reset %s stale workflow run(s) after a restart", len(stale))


# ---- 模板 ------------------------------------------------------------------

def render_prompt(tpl: str, context: Dict[str, object]) -> str:
    """把 ``{{name}}`` 替换成上下文里的值。

    引用了不存在的变量直接报错，而不是替换成空串：静悄悄少喂一段简历全文，
    模型照样能编出一份看着挺完整的报告，而那份报告是废的，代价是一整趟调用的
    时间和 token。
    """
    missing: List[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in context:
            missing.append(name)
            return match.group(0)
        value = context[name]
        return "" if value is None else str(value)

    rendered = _PLACEHOLDER.sub(replace, tpl)
    if missing:
        raise ValueError(f"模板引用了未定义的变量：{'、'.join(sorted(set(missing)))}")
    return rendered


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
        """模板里的 ``{{name}}`` 必须是声明过的输入，或**前面并行组**的产物。"""
        known = set(declared)
        for _, members in _group_order(steps):
            in_group = {step.output_var: step.key for step in members if step.output_var}
            for step in members:
                for name in _PLACEHOLDER.findall(step.prompt_tpl):
                    if name in known:
                        continue
                    if name in in_group:
                        raise ValueError(
                            f"步骤 {step.key} 引用了同组步骤 {in_group[name]} 的产物 {name}："
                            "同一组是并行执行的，那一步这时还没有输出"
                        )
                    if "." in name:
                        raise ValueError(
                            f"步骤 {step.key} 的模板引用了 {name}：暂不支持按字段取值，"
                            f"只能用整段产物 {name.split('.')[0]}"
                        )
                    raise ValueError(
                        f"步骤 {step.key} 的模板引用了未定义的变量 {name}："
                        "它既不是这条流程声明的输入，也不是前面步骤的产物"
                    )
            known |= set(in_group)

    # -- 发起运行 -------------------------------------------------------------

    def create_run(self, workflow_id: int, inputs: Dict[str, str]) -> WorkflowRun:
        """校验输入、落一条运行与每步的 ``pending`` 行。

        步骤行在这里一次性建齐：这样「六个方向跑到第三页崩了」在运行历史里
        看得见全貌（前两页成功、第三页失败、后面三页 pending/skipped），
        而不是只有已执行的那几步。
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

    def list_runs(self, workflow_id: Optional[int] = None, limit: int = 20) -> List[WorkflowRun]:
        stmt = select(WorkflowRun).where(WorkflowRun.owner_id == self._owner_id)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
        return list(
            self._db.scalars(stmt.order_by(WorkflowRun.id.desc()).limit(limit)).all()
        )

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

    @staticmethod
    def to_run_item(run: WorkflowRun) -> WorkflowRunItem:
        return WorkflowRunItem(
            id=run.id,
            workflow_id=run.workflow_id,
            workflow_slug=run.workflow_slug,
            status=run.status,
            error_msg=run.error_msg,
            elapsed_ms=run.elapsed_ms,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )

    def to_run_detail(self, run: WorkflowRun) -> WorkflowRunDetail:
        """运行详情：带输入快照与每一步的现场（含渲染后的 prompt）。"""
        return WorkflowRunDetail(
            **self.to_run_item(run).model_dump(),
            inputs=dict(run.inputs or {}),
            steps=[
                WorkflowRunStepItem(
                    id=row.id,
                    key=row.key,
                    group_key=row.group_key,
                    agent_slug=row.agent_slug,
                    status=row.status,
                    prompt=row.prompt,
                    output=row.output,
                    parsed=row.parsed,
                    error_msg=row.error_msg,
                    model_config_id=row.model_config_id,
                    elapsed_ms=row.elapsed_ms,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in self.run_steps(run.id)
            ],
        )


# ---- 执行 ------------------------------------------------------------------

@dataclass
class _StepOutcome:
    """一个步骤的执行结果，供编排协程判断要不要继续往下走。"""

    key: str
    status: str
    optional: bool
    output_var: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None


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
        row.started_at = datetime.now(timezone.utc)
        db.commit()

        try:
            prompt = render_prompt(step.prompt_tpl, context)
            row.prompt = prompt
            db.commit()
        except ValueError as exc:
            return await _finish_step(db, row, step, queue, str(exc), started)

        try:
            text = await run_agent(
                db,
                owner_id,
                step.agent_slug,
                prompt,
                config_id=step.model_config_id,
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
    row.model_config_id = step.model_config_id
    row.finished_at = datetime.now(timezone.utc)
    db.commit()

    outcome = _StepOutcome(
        key=step.key,
        status=row.status,
        optional=step.optional,
        output_var=step.output_var,
        output=output,
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
        run.started_at = datetime.now(timezone.utc)
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
                        context[outcome.output_var] = outcome.output
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
            .values(status=STEP_SKIPPED, finished_at=datetime.now(timezone.utc))
        )

        run = db.get(WorkflowRun, run_id)
        run.status = RUN_CANCELLED if aborted else (RUN_FAILED if failed else RUN_SUCCEEDED)
        run.error_msg = failed
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        run.finished_at = datetime.now(timezone.utc)
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


async def run_builtin(
    db: Session,
    owner_id: int,
    slug: str,
    inputs: Dict[str, str],
    *,
    step_key: Optional[str] = None,
) -> str:
    """跑完一条内置工作流，返回它那一步的产物文本。

    功能侧（简历、幻灯片、面试、求职助手）调一次模型的路径统一到这里：以前是
    ``_ask(slug, prompt)`` 直接发一次请求，现在同样的调用先落一条运行，步骤的
    渲染结果、原始输出、耗时都进 ``workflow_run_step``——「当时模型到底收到了
    什么」从此问得出来，prompt 与模型也就能在「工作流」页面上改了。

    ``step_key`` 省略时取最后一个成功步骤：内置流程基本都是单步，调用方不必
    跟着定义里的 key 改一遍。
    """
    workflows = WorkflowService(db, owner_id)
    workflow = workflows.require_by_slug(slug)
    run = workflows.create_run(workflow.id, inputs)

    async for event, payload in execute_run(owner_id=owner_id, run_id=run.id):
        if event == "step_end" and payload.get("error"):
            logger.info(
                "workflow %s step %s failed: %s", slug, payload.get("key"), payload["error"]
            )

    finished = workflows.get_run(run.id)
    if finished.status != RUN_SUCCEEDED:
        raise RuntimeError(finished.error_msg or "工作流执行失败")

    rows = [row for row in workflows.run_steps(run.id) if row.status == STEP_SUCCEEDED]
    step = next(
        (row for row in reversed(rows) if step_key is None or row.key == step_key), None
    )
    if step is None:
        raise RuntimeError(f"工作流 {slug} 没有产出可用的结果")
    return step.output or ""

