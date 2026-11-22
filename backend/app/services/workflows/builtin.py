"""内置工作流：随代码 seed 的流水线，以及功能侧跑一条内置流程的入口。

``BUILTIN_WORKFLOWS`` 是这份目录（用 dataclass 声明输入与步骤），启动时由
:func:`seed_builtin_workflows` 就地刷进 ``workflow`` 表：步骤按 ``key`` 对齐，
改了的覆盖、消失的删掉，prompt 与步骤结构的改进随代码一起上线；``enabled``
不动——把一条流程藏起来是用户自己的决定。内置项只占 ``owner_id = 0`` 那一行，
所有人可见、不可直接修改、可以复制成自己的版本再改。

目录里只写「框住材料的骨架」（标签与三引号边界），条件段落由调用方拼成整块
变量传进来，免得把 Python 里的拼装逻辑在模板里抄第二份。

:func:`start_builtin` 与 :func:`finish_builtin_step` / :func:`builtin_output`
是简历、幻灯片、面试那些一次性模型调用的统一入口：先在请求里把运行建出来并
认领业务对象，后台再跑完它，功能侧读落库的那一步产物。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Workflow, WorkflowRun, WorkflowRunStep, WorkflowStep
from app.services.agent_run import OUTPUT_JSON, OUTPUT_MARKDOWN
from app.services.agents import BUILTIN_OWNER_ID
from app.services.workflows.defs import WorkflowService
from app.services.workflows.exec import execute_run
from app.services.workflows import RUN_SUCCEEDED, STEP_SUCCEEDED

logger = logging.getLogger("yangvis.workflows")

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
                    "{{max_pages}} 页是容量上限，不是目标，页数按人设里的材料体量规则定。"
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
    BuiltinWorkflow(
        slug="video-script-generation",
        name="短视频脚本生成",
        description="「智能应用 · 短视频」从一句话想法产出创作卡与分镜脚本（不含画面渲染）",
        inputs=(
            BuiltinInput("idea", "想法"),
            # 条件段落由调用方拼好再传进来：没填的那些就是空串。
            BuiltinInput("platform_note", "平台与时长段落", required=False),
            BuiltinInput("style_note", "风格段落", required=False),
            # 项目角色组的档案段落（人设/衣着/特色），空串 = 这条片子没选人物库角色。
            BuiltinInput("characters_note", "角色段落", required=False),
            BuiltinInput("requirement_note", "补充要求段落", required=False),
            BuiltinInput("brief_note", "已确认的创作卡段落", required=False),
            BuiltinInput("max_shots", "镜头数上限"),
        ),
        steps=(
            BuiltinStep(
                key="brief",
                agent_slug="video-briefer",
                prompt_tpl=(
                    "{{platform_note}}{{style_note}}{{characters_note}}"
                    "{{requirement_note}}{{brief_note}}"
                    "想法：\n\"\"\"\n{{idea}}\n\"\"\"\n\n"
                    "请先定这条短视频的创作卡：把「凭什么让人看完」说清楚，产出 JSON。"
                ),
                group_key="brief",
                output_kind=OUTPUT_JSON,
                output_keys=(
                    "hook_type",
                    "hook",
                    "audience",
                    "goal",
                    "structure",
                    "duration_s",
                    "risk_notes",
                ),
                output_var="brief",
                sort_order=10,
            ),
            BuiltinStep(
                key="script",
                agent_slug="video-storyboarder",
                prompt_tpl=(
                    "创作卡（钩子、结构、总时长按它执行，不要自己改）：\n"
                    "{{brief}}\n\n"
                    # 风格段（含人物设定）此前只喂给策划师：分镜师看不到它，各镜
                    # image_prompt 就会各画各的脸和衣服。空串时整段不渲染，与旧模板等价。
                    "{{style_note}}"
                    # 角色段给出人物库角色的名字与形象档案：相关镜头的 image_prompt
                    # 要写出角色名和衣着/特色，渲染时还会把形象图作为参考图注入。
                    "{{characters_note}}"
                    "把它切成能直接送去生成的镜头：最多 {{max_shots}} 镜，产出分镜 JSON。"
                ),
                group_key="script",
                output_kind=OUTPUT_JSON,
                output_keys=("title", "narration", "tags", "cover_prompt", "shots"),
                output_var="script",
                sort_order=20,
            ),
        ),
        sort_order=80,
    ),
    BuiltinWorkflow(
        slug="novel-outline",
        name="小说大纲生成",
        description="「智能应用 · 创作」按灵感与设定产出卷章大纲 JSON（写小说的生成大纲）",
        inputs=(
            BuiltinInput("title", "作品名"),
            BuiltinInput("brief_block", "灵感梗概段落", required=False),
            BuiltinInput("params_block", "创作参数段落", required=False),
            BuiltinInput("requirement_note", "用户要求段落", required=False),
            BuiltinInput("max_nodes", "节点上限"),
        ),
        steps=(
            BuiltinStep(
                key="outline",
                agent_slug="novel-architect",
                prompt_tpl=(
                    "作品名：{{title}}\n\n"
                    "{{brief_block}}{{params_block}}{{requirement_note}}"
                    "请按人设里的 JSON 契约产出这部小说的卷章大纲，"
                    "节点总数不要超过 {{max_nodes}} 个（卷与章合计）。"
                ),
                group_key="outline",
                sort_order=10,
            ),
        ),
        sort_order=90,
    ),
    BuiltinWorkflow(
        slug="article-outline",
        name="文章大纲生成",
        description="「智能应用 · 创作」按主题与素材产出段落大纲与标题候选 JSON（写文章的生成大纲）",
        inputs=(
            BuiltinInput("title", "作品名"),
            BuiltinInput("brief_block", "主题素材段落", required=False),
            BuiltinInput("params_block", "创作参数段落", required=False),
            BuiltinInput("requirement_note", "用户要求段落", required=False),
            BuiltinInput("max_nodes", "节点上限"),
        ),
        steps=(
            BuiltinStep(
                key="outline",
                agent_slug="article-architect",
                prompt_tpl=(
                    "作品名：{{title}}\n\n"
                    "{{brief_block}}{{params_block}}{{requirement_note}}"
                    "请按人设里的 JSON 契约产出标题候选与段落大纲，"
                    "段落总数不要超过 {{max_nodes}} 个。"
                ),
                group_key="outline",
                sort_order=10,
            ),
        ),
        sort_order=100,
    ),
    BuiltinWorkflow(
        slug="novel-chapter",
        name="小说章节起草",
        description="「智能应用 · 创作」按设定、前情与章节摘要起草或续写单章正文（写小说的章节生成）",
        inputs=(
            BuiltinInput("title", "作品名"),
            BuiltinInput("work_card_block", "作品卡段落", required=False),
            BuiltinInput("outline_block", "完整大纲段落", required=False),
            BuiltinInput("history_block", "前情段落", required=False),
            BuiltinInput("chapter_block", "本章要求段落"),
            BuiltinInput("existing_block", "本章已有正文段落", required=False),
            BuiltinInput("instruction_note", "动作与用户要求段落"),
        ),
        steps=(
            BuiltinStep(
                key="draft",
                agent_slug="novel-writer",
                prompt_tpl=(
                    "作品名：{{title}}\n\n"
                    "{{work_card_block}}{{outline_block}}{{history_block}}"
                    "{{chapter_block}}{{existing_block}}{{instruction_note}}"
                    "请按人设要求直接输出本章正文。"
                ),
                group_key="draft",
                sort_order=10,
            ),
        ),
        sort_order=110,
    ),
    BuiltinWorkflow(
        slug="article-draft",
        name="文章整篇起草",
        description="「智能应用 · 创作」按段落大纲一次性生成整篇文章，按二级标题切回各段落",
        inputs=(
            BuiltinInput("title", "作品名"),
            BuiltinInput("brief_block", "主题素材段落", required=False),
            BuiltinInput("params_block", "创作参数段落", required=False),
            BuiltinInput("requirement_note", "用户要求段落", required=False),
            BuiltinInput("outline_block", "段落大纲"),
        ),
        steps=(
            BuiltinStep(
                key="draft",
                agent_slug="article-writer",
                prompt_tpl=(
                    "作品名：{{title}}\n\n"
                    "{{brief_block}}{{params_block}}{{requirement_note}}{{outline_block}}"
                    "请按人设要求输出整篇文章，每个大纲段落对应一个二级标题。"
                ),
                group_key="draft",
                sort_order=10,
            ),
        ),
        sort_order=120,
    ),
    BuiltinWorkflow(
        slug="writing-polish",
        name="正文编辑加工",
        description="「智能应用 · 创作」对已有章节/段落做重写、润色、扩写、缩写、换语气（小说与文章共用）",
        inputs=(
            BuiltinInput("context_block", "章节摘要/段落观点段落", required=False),
            BuiltinInput("content_block", "待处理正文"),
            BuiltinInput("instruction_note", "动作与用户要求段落"),
        ),
        steps=(
            BuiltinStep(
                key="polish",
                agent_slug="writing-editor",
                prompt_tpl=(
                    "{{context_block}}{{content_block}}{{instruction_note}}"
                    "请按人设要求输出处理后的完整正文。"
                ),
                group_key="polish",
                sort_order=10,
            ),
        ),
        sort_order=130,
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
