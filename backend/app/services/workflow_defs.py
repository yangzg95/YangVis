"""定义存取：工作流与步骤的读写，以及一条运行的建与查。

可见性只有一条规则——共享的内置项（``owner_id = 0``）加上用户自己的；内置项不可
直接改，只能 :meth:`WorkflowService.duplicate` 成自己的版本再改。

保存时把「跑起来必然失败」的写法先挡下来：``_normalize`` 归一化步骤顺序与分组，
``_check_template_vars`` 要求 ``{{name}}`` 的根要么是声明过的输入、要么是**前面
并行组**的产物，``.字段`` 只允许挂在 JSON 步骤上，另外还查人设可见性与模型配置的
归属。这里报错只花一次请求；放过去就是一次运行的时间和 token，外加运行历史里一行
谁也看不懂原因的失败。

:meth:`WorkflowService.create_run` 一次性把每步的 ``pending`` 行建齐，所以「六步
跑到第三步崩」在历史里看得见全貌。运行记录的读侧也在这个类里：列表页要的最近一次
运行、详情页要的步骤行、一页运行各自用过哪些模型配置，都按整页一次查完。
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.errors import BusinessError, CODE_CHAT_NOT_READY
from app.models.entities import (
    McpServer,
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
    WorkflowLastRun,
    WorkflowRunDetail,
    WorkflowRunItem,
    WorkflowRunStepItem,
    WorkflowStepItem,
    WorkflowUpdate,
)
from app.services.agent_run import OUTPUT_JSON, OUTPUT_MARKDOWN, require_chat_model
from app.services.agents import AgentService, BUILTIN_OWNER_ID
from app.services.workflow_template import _PLACEHOLDER, _group_order
from app.services.workflows import RUN_PENDING, STEP_PENDING

logger = logging.getLogger("yangvis.workflows")

# 变量名、步骤 key 与分组名的合法字符。必须与模板引用的字符集对齐（见
# :func:`app.services.workflow_template.render_prompt`）：一个声明成 ``my-var``
# 的变量模板里永远引用不到，静默少喂一段材料比保存时报错难查得多。不含点号——
# ``a.b`` 里的 ``a`` 是变量名，点号后面是它的字段。
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``{{a.b.c}}`` 里每一段的形状：对象的键，或数组的下标。形状不对（``a..b``、
# ``a.b-c``）在保存时就说清楚，而不是等运行时报「取不到字段」——那时错误挂在
# 运行记录里，比编辑器里一行红字难定位得多。
_FIELD_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|\d+)$")


# ---- 定义存取 --------------------------------------------------------------

def _input_schema(fields: Sequence[WorkflowInputField]) -> List[dict]:
    """把声明的输入变量落成 ``workflow.input_schema`` 的 JSON 值。

    标签留空时用变量名兜底：界面上少填一格不该让运行历史里出现一个没有名字的
    变量。形状与内置工作流 seed 出来的完全一致（:mod:`app.services.workflow_builtin`
    的 ``BuiltinInput`` 同三个键），读的一侧不用分两种情况。
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

# subject_label 的列宽，与实体定义一致：业务标题再长也只存到这儿，超长由
# create_run 统一截，调用方不必各自防着 MySQL 的长度错误。
SUBJECT_LABEL_MAX = 128


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

    def last_runs(self, workflow_ids: Sequence[int]) -> Dict[int, WorkflowRun]:
        """每条流程的**最近一次**运行，按 ``workflow_id`` 取；口径同 :func:`latest_runs`。

        清单页要在一屏里说出每条流程跑没跑、什么状态，按行逐条查就是每行一次往返。
        """
        ids = list(workflow_ids)
        if not ids:
            return {}
        newest = (
            select(WorkflowRun.workflow_id, func.max(WorkflowRun.id).label("run_id"))
            .where(
                WorkflowRun.owner_id == self._owner_id,
                WorkflowRun.workflow_id.in_(ids),
            )
            .group_by(WorkflowRun.workflow_id)
            .subquery()
        )
        rows = self._db.scalars(
            select(WorkflowRun).join(newest, WorkflowRun.id == newest.c.run_id)
        ).all()
        return {row.workflow_id: row for row in rows}

    @staticmethod
    def _to_last_run(run: WorkflowRun) -> WorkflowLastRun:
        """卡片上那一行的摘要。

        时间只报一个：结束 / 开始 / 创建依次取，pending 的运行前两个都还没写下。
        """
        return WorkflowLastRun(
            id=run.id,
            status=run.status,
            elapsed_ms=run.elapsed_ms,
            at=run.finished_at or run.started_at or run.created_at,
        )

    def to_item(
        self, workflow: Workflow, last_run: Optional[WorkflowRun] = None
    ) -> WorkflowItem:
        steps = self.steps_of(workflow.id)
        agent_names = self._agent_names(step.agent_slug for step in steps)
        if last_run is None:
            # 单对象端点（取一条、创建、更新、复制）各自只有一行，就地查一次；
            # 列表端点批量查完传进来，别让它退化成每行一次。
            last_run = self.last_runs([workflow.id]).get(workflow.id)
        return WorkflowItem(
            id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            description=workflow.description,
            inputs=[WorkflowInputField(**field) for field in (workflow.input_schema or [])],
            mcp_server_ids=list(workflow.mcp_server_ids or []),
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
                    mcp_server_ids=list(step.mcp_server_ids or []),
                    optional=step.optional,
                )
                for step in steps
            ],
            is_builtin=workflow.is_builtin,
            enabled=workflow.enabled,
            sort_order=workflow.sort_order,
            last_run=self._to_last_run(last_run) if last_run is not None else None,
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
            mcp_server_ids=list(payload.mcp_server_ids or []),
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
        workflow.mcp_server_ids = list(payload.mcp_server_ids or [])
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
            # 副本在本人名下，原挂载仍然有效；内置源该列表恒为空。
            mcp_server_ids=list(source.mcp_server_ids or []),
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
                    # 副本在本人名下，节点级挂载引用同样有效。
                    mcp_server_ids=list(step.mcp_server_ids or []),
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

        # MCP 挂载：保存时只校验「存在且属于本人」（去重）；停用状态放行，
        # 允许先挂载后启用，运行时 resolve_servers 再挡停用项。
        mcp_ids = list(dict.fromkeys(payload.mcp_server_ids or []))
        if mcp_ids:
            owned = self._db.scalars(
                select(McpServer.id).where(
                    McpServer.owner_id == self._owner_id,
                    McpServer.id.in_(mcp_ids),
                )
            ).all()
            if len(set(owned)) != len(mcp_ids):
                raise ValueError("挂载了不存在或不属于本人的 MCP 服务，请重新选择")

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

            # 节点级 MCP 挂载：与工作流级同一口径（存在且属于本人，去重；停用
            # 放行，运行时再挡），报错要点名是第几步挂的。
            step_mcp_ids = list(dict.fromkeys(step.mcp_server_ids or []))
            if step_mcp_ids:
                owned_step_servers = self._db.scalars(
                    select(McpServer.id).where(
                        McpServer.owner_id == self._owner_id,
                        McpServer.id.in_(step_mcp_ids),
                    )
                ).all()
                if len(set(owned_step_servers)) != len(step_mcp_ids):
                    raise ValueError(
                        f"{where}挂载了不存在或不属于本人的 MCP 服务，请重新选择"
                    )

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
                    mcp_server_ids=step_mcp_ids,
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
                    # 产物以什么名字进的上下文：前端据此还原每一步收到的输入变量。
                    output_var=definitions[row.key].output_var if row.key in definitions else None,
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
