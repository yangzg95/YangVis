"""智能办公 · 简历的存取与 AI 分析。

数据访问按单个用户隔离（与 :class:`KnowledgeService` 同一约定）：所有查询都
经过 :meth:`ResumeService._scope`，handler 不直接接触 session。

分析与对比都是后台任务：一次调用要等模型读完整份（甚至几份）简历全文，远超
SPA 的 30s axios 超时，所以接口只负责把这次运行建出来，真正的模型调用在
``BackgroundTasks`` 里跑，前端靠轮询状态感知进度。

进度与失败原因不在简历行上：发起分析就是 ``start_builtin`` 建一条认领了这份简历
的运行，界面上的 ``status`` / ``error_msg`` 由 :func:`subject_state` 从最近一次
运行翻译出来。这一行只留产物（报告、改进意见、分析时间）。
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.entities import (
    ModelConfig,
    Resume,
    ResumeComparison,
    ResumeToolkitTask,
    WorkflowRun,
)
from app.models.schemas import (
    ResumeComparisonDetail,
    ResumeComparisonItem,
    ResumeDetail,
    ResumeItem,
    ResumeToolkitCreate,
    ResumeToolkitDetail,
    ResumeToolkitItem,
)
from app.services.agent_run import (
    extract_json_object,
    labeled_section,
    model_error_text,
    require_chat_model,
)
from app.services.chunking import UnsupportedFileType, extract_text, normalise
from app.services.workflows import (
    SUBJECT_ERROR,
    SUBJECT_RESUME,
    SUBJECT_RESUME_COMPARISON,
    SUBJECT_RESUME_TOOLKIT,
    fail_run,
    finish_builtin,
    latest_run,
    latest_runs,
    running_subjects,
    start_builtin,
    subject_state,
)

logger = logging.getLogger("yangvis.resume")

# 简历的额外支持格式；纯文本格式直接复用知识库的抽取逻辑。
RESUME_SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown")

# 喂给模型的单份简历全文的截断上限。多数聊天模型窗口在 32k-128k token，
# 对比时最多 10 份简历，每份留 12000 字符，加上 prompt 也不会把窗口撑爆。
MAX_RESUME_CHARS = 12000

# 分析与对比的人设（含输出格式要求）都在内置智能体上，Prompt 骨架在内置工作流上，
# 两边都随 seed 管线就地刷新；这里只留「哪条流程」。
# 变量内容（简历全文）走运行输入，不进模板 seed。
WORKFLOW_ANALYZE = "resume-analysis"
WORKFLOW_COMPARE = "resume-comparison"


# ---- 求职助手（toolkit）-------------------------------------------------------

@dataclass(frozen=True)
class ToolkitKindSpec:
    """一类求职助手生成任务的规则。

    ``needs_resume`` / ``allows_resume`` 约束「简历来源」（已上传简历或粘贴文本）；
    ``background_as_source`` 为真时，background 字段也能充当来源（职业匹配分析）。
    ``required_fields`` 是简历来源之外的必填项：(字段名, 中文名)。
    ``workflow_slug`` 指向内置工作流里的那条流水线——人设、Prompt 骨架与模型
    都在工作流和它的步骤上，这里只声明「这类任务走哪条」。
    """

    label: str
    workflow_slug: str
    needs_resume: bool = False
    allows_resume: bool = False
    background_as_source: bool = False
    required_fields: tuple = ()


TOOLKIT_KINDS: dict = {
    "optimize": ToolkitKindSpec(
        label="简历优化",
        workflow_slug="toolkit-optimize",
        needs_resume=True,
    ),
    "career-match": ToolkitKindSpec(
        label="职业匹配分析",
        workflow_slug="toolkit-career-match",
        needs_resume=True,
        background_as_source=True,
    ),
    "jd-match": ToolkitKindSpec(
        label="简历匹配审计",
        workflow_slug="toolkit-jd-match",
        needs_resume=True,
        required_fields=(("job_description", "职位描述"),),
    ),
    "interview-prep": ToolkitKindSpec(
        label="面试准备策略",
        workflow_slug="toolkit-interview-prep",
        allows_resume=True,
        required_fields=(("position", "岗位名称"),),
    ),
    "portfolio-plan": ToolkitKindSpec(
        label="证明构建计划",
        workflow_slug="toolkit-portfolio-plan",
        allows_resume=True,
        required_fields=(("position", "目标职位"),),
    ),
    "salary-negotiation": ToolkitKindSpec(
        label="薪资最大化框架",
        workflow_slug="toolkit-salary-negotiation",
        required_fields=(("offer_amount", "offer 年薪"),),
    ),
}


def _toolkit_title(kind: str, inputs: dict) -> str:
    """按 kind 和输入快照生成记录标题，如「面试准备 · 高级前端工程师」。"""
    source_name = inputs.get("resume_title") or (
        "粘贴的简历" if (inputs.get("resolved_resume") or "") else ""
    )
    position = (inputs.get("position") or "").strip()
    if kind == "optimize":
        subject = position or source_name
    elif kind == "career-match":
        subject = source_name or "个人背景"
    elif kind == "jd-match":
        subject = source_name
    elif kind in ("interview-prep", "portfolio-plan"):
        subject = position
    elif kind == "salary-negotiation":
        subject = (inputs.get("offer_amount") or "").strip()
    else:
        subject = ""
    label = TOOLKIT_KINDS[kind].label
    return f"{label} · {subject}"[:128] if subject else label


def _toolkit_inputs(kind: str, inputs: dict) -> dict:
    """把输入快照整理成那条工作流声明的输入变量。

    条件段落在这里拼好（「有目标岗位才加那一行」），模板里只有 ``{{变量}}`` 与
    固定骨架：模板不做判断，否则就得为它发明一门小语言。整段材料用
    :func:`labeled_section` 包成三引号边界，材料里的「请忽略上面」才会被当成
    内容读。
    """
    content = (inputs.get("resolved_resume") or "").strip()
    position = (inputs.get("position") or "").strip()
    background = (inputs.get("background") or "").strip()
    notes = (inputs.get("notes") or "").strip()

    if kind == "optimize":
        return {
            "position_note": f"目标岗位：{position}\n\n" if position else "",
            "resume_content": content,
        }

    if kind == "career-match":
        return {
            "resume_block": f"{labeled_section('简历全文', content)}\n\n" if content else "",
            "background_block": labeled_section("个人背景补充", background) if background else "",
        }

    if kind == "jd-match":
        return {
            "job_description": _truncate(inputs.get("job_description") or ""),
            "resume_content": content,
        }

    if kind == "interview-prep":
        job_description = (inputs.get("job_description") or "").strip()
        return {
            "position": position,
            "job_description_block": (
                f"{labeled_section('职位描述', _truncate(job_description))}\n\n"
                if job_description
                else ""
            ),
            "resume_block": labeled_section("求职者简历全文", content) if content else "",
        }

    if kind == "portfolio-plan":
        return {
            "position": position,
            "resume_block": f"{labeled_section('求职者简历全文', content)}\n\n" if content else "",
            "background_block": labeled_section("个人背景补充", background) if background else "",
        }

    if kind == "salary-negotiation":
        return {
            "offer_amount": (inputs.get("offer_amount") or "").strip(),
            "position_note": f"\n岗位：{position}" if position else "",
            "notes_note": f"\n补充说明：{notes}" if notes else "",
        }

    raise ValueError(f"未知的求职助手类型：{kind}")


def extract_resume_text(filename: str, raw: bytes) -> str:
    """把上传的简历文件解码成纯文本。

    PDF / DOCX 用各自的解析器；纯文本格式复用知识库的解码逻辑
    （多编码回退 + 空白规整）。不支持的格式抛 :class:`UnsupportedFileType`。
    """
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # noqa: BLE001 - pypdf 的异常类型随文件而坏法各异
            raise UnsupportedFileType(f"PDF 解析失败：{exc}") from exc
        return normalise(text)

    if lowered.endswith(".docx"):
        import docx

        try:
            document = docx.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
        except Exception as exc:  # noqa: BLE001 - 同上
            raise UnsupportedFileType(f"Word 文档解析失败：{exc}") from exc
        return normalise(text)

    if lowered.endswith((".txt", ".md", ".markdown")):
        return extract_text(filename, raw)

    raise UnsupportedFileType(
        f"暂不支持该格式，目前仅支持 {'/'.join(RESUME_SUPPORTED_EXTENSIONS)}"
    )


def _truncate(text: str, limit: int = MAX_RESUME_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n……（原文过长，已截断）"


def _parse_analysis(raw_text: str) -> tuple[str, List[str]]:
    """从模型输出里拆出报告和改进意见。

    剥 ```json 代码栅栏、截取第一个 ``{`` 到最后一个 ``}`` 这些由
    :func:`extract_json_object` 统一处理。这里只留下简历自己的一层兜底：
    解析不出来就把整段输出当报告——一份格式跑偏的报告对用户还有用，
    一条「解析失败」的空白页没有。
    """
    try:
        data = extract_json_object(raw_text)
    except ValueError:
        logger.debug("analysis output was not valid json, falling back to raw text")
        return raw_text.strip(), []

    report = str(data.get("report") or "").strip()
    # 模型把 suggestions 写成字符串或对象都可能：按列表逐项转文本，别的形态
    # 一律丢弃，否则遍历字符串会把建议拆成一个个字。
    raw_suggestions = data.get("suggestions")
    suggestions = (
        [str(item).strip() for item in raw_suggestions if str(item).strip()]
        if isinstance(raw_suggestions, list)
        else []
    )
    if not report:
        logger.debug("analysis json had no report field, falling back to raw text")
        return raw_text.strip(), []
    return report, suggestions


class ResumeService:
    """针对单个用户的简历做增删改查与 AI 分析。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(Resume.owner_id == self._owner_id)

    def _scope_comparison(self, stmt: Select) -> Select:
        return stmt.where(ResumeComparison.owner_id == self._owner_id)

    def _scope_toolkit(self, stmt: Select) -> Select:
        return stmt.where(ResumeToolkitTask.owner_id == self._owner_id)

    # -- 模型配置 -----------------------------------------------------------

    def require_chat_config(self) -> ModelConfig:
        """分析与对比共用的一份门槛：用户得先有一份验证过的默认对话模型。"""
        return require_chat_model(self._db, self._owner_id)

    # -- 简历 CRUD -----------------------------------------------------------

    def list_resumes(self) -> List[Resume]:
        stmt = self._scope(select(Resume)).order_by(Resume.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_resume(self, resume_id: int) -> Resume:
        resume = self._db.scalar(self._scope(select(Resume).where(Resume.id == resume_id)))
        if resume is None:
            raise LookupError("简历不存在")
        return resume

    def create_resume(
        self,
        *,
        title: str,
        description: Optional[str],
        filename: str,
        mime: Optional[str],
        size: int,
        content: str,
    ) -> Resume:
        resume = Resume(
            owner_id=self._owner_id,
            title=title,
            description=description,
            filename=filename,
            mime=mime,
            size=size,
            content=content,
        )
        self._db.add(resume)
        self._db.commit()
        self._db.refresh(resume)
        logger.info(
            "owner %s created resume %s (%s, %d bytes)",
            self._owner_id,
            resume.id,
            filename,
            size,
        )
        return resume

    def update_resume(
        self,
        resume_id: int,
        *,
        title: Optional[str],
        description: Optional[str],
    ) -> Resume:
        resume = self.get_resume(resume_id)
        if title is not None:
            resume.title = title
        if description is not None:
            resume.description = description or None
        self._db.commit()
        self._db.refresh(resume)
        return resume

    def delete_resume(self, resume_id: int) -> None:
        resume = self.get_resume(resume_id)
        if self._analyzing(SUBJECT_RESUME, resume.id):
            raise ValueError("简历正在分析中，请稍后再删")
        self._db.delete(resume)
        self._db.commit()
        logger.info("owner %s deleted resume %s (%s)", self._owner_id, resume_id, resume.title)

    def mark_netdisk(self, resume: Resume, fs_id: int, path: str) -> None:
        """网盘同步成功后回填原文件在网盘里的位置。"""
        resume.netdisk_fs_id = fs_id
        resume.netdisk_path = path
        self._db.commit()

    # -- 运行状态：业务行上没有 status，一切从认领它的运行读 ---------------------

    def _analyzing(self, subject_kind: str, subject_ref: object) -> bool:
        """这个业务对象还在跑吗？删除与重复触发的闸门读的是运行，不是列。"""
        return bool(
            running_subjects(self._db, self._owner_id, subject_kind, [subject_ref])
        )

    # -- AI 分析 -------------------------------------------------------------

    def _analysis_inputs(self, resume: Resume) -> dict:
        """把一份简历整理成工作流的输入变量。

        「有补充描述才加那一段」这种条件拼装留在代码里：模板只做变量替换，
        否则就得为模板发明一门小语言，而这段逻辑用 Python 读起来更便宜。
        """
        return {
            "resume_content": _truncate(resume.content),
            "description_note": (
                f"上传者对这份简历的补充描述：{resume.description}\n\n"
                if resume.description
                else ""
            ),
        }

    def start_analysis(self, resume_id: int) -> Resume:
        """为这份简历建一条认领它的运行；模型调用由后台任务完成。

        运行在这里就落库，接口一返回界面上就是「分析中」——以前这一步写的是
        ``resume.status``，状态列退役后它就是这条运行。
        """
        resume = self.get_resume(resume_id)
        if self._analyzing(SUBJECT_RESUME, resume.id):
            raise ValueError("该简历正在分析中，请勿重复触发")
        start_builtin(
            self._db,
            self._owner_id,
            WORKFLOW_ANALYZE,
            self._analysis_inputs(resume),
            subject=(SUBJECT_RESUME, resume.id),
        )
        self._db.refresh(resume)
        return resume

    async def run_analysis(self, resume_id: int) -> None:
        """后台任务体：跑认领这次分析的运行，只写产物。

        失败的去处是运行本身（``workflow_run_step`` 里的现场与 ``error_msg``），
        这里只负责把它记进日志；后处理挂了才用 :func:`fail_run` 降级运行。
        """
        started = time.monotonic()
        resume = self.get_resume(resume_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_RESUME, resume.id)
        if run is None:
            logger.error("resume analysis has no run to execute: id=%s", resume_id)
            return
        logger.info(
            "resume analysis started: id=%s run=%s owner=%s title=%r content_chars=%s",
            resume_id,
            run.id,
            self._owner_id,
            resume.title,
            len(resume.content),
        )
        try:
            text = await finish_builtin(self._db, self._owner_id, run.id)
            report, suggestions = _parse_analysis(text)

            resume.report = report
            resume.suggestions = suggestions or None
            resume.analyzed_at = datetime.now(timezone.utc)
            self._db.commit()
            logger.info(
                "resume analysis finished: id=%s report_chars=%s suggestions=%s elapsed=%.1fs",
                resume_id,
                len(report),
                len(suggestions),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 失败写在运行上，不留到这里抛出
            logger.warning(
                "resume analysis failed: id=%s elapsed=%.1fs error=%s",
                resume_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

    # -- 简历对比 -------------------------------------------------------------

    def _comparison_inputs(self, resumes: List[Resume]) -> dict:
        """把多份简历拼成对比用的那一段材料（发起时定稿，进运行的输入快照）。"""
        blocks = []
        for index, resume in enumerate(resumes, start=1):
            description = f"\n补充描述：{resume.description}" if resume.description else ""
            blocks.append(
                f"=== 简历 {index}：{resume.title} ==={description}\n"
                f"\"\"\"\n{_truncate(resume.content)}\n\"\"\""
            )
        return {
            "resumes_block": "\n\n".join(blocks),
            "resume_count": str(len(resumes)),
        }

    def create_comparison(self, resume_ids: List[int], title: Optional[str]) -> ResumeComparison:
        resumes = [self.get_resume(rid) for rid in resume_ids]
        if len({r.id for r in resumes}) != len(resume_ids):
            raise ValueError("存在重复的简历")
        comparison = ResumeComparison(
            owner_id=self._owner_id,
            title=title or None,
            resume_ids=list(resume_ids),
        )
        self._db.add(comparison)
        self._db.commit()
        self._db.refresh(comparison)
        # 材料在发起时就定稿：这一行之后即使有简历被删，进行中的对比与历史记录
        # 喂给模型的还是当时那几份全文。
        start_builtin(
            self._db,
            self._owner_id,
            WORKFLOW_COMPARE,
            self._comparison_inputs(resumes),
            subject=(SUBJECT_RESUME_COMPARISON, comparison.id),
        )
        logger.info(
            "owner %s created resume comparison %s over %d resume(s)",
            self._owner_id,
            comparison.id,
            len(resume_ids),
        )
        return comparison

    def list_comparisons(self) -> List[ResumeComparison]:
        stmt = self._scope_comparison(select(ResumeComparison)).order_by(
            ResumeComparison.id.desc()
        )
        return list(self._db.scalars(stmt).all())

    def get_comparison(self, comparison_id: int) -> ResumeComparison:
        comparison = self._db.scalar(
            self._scope_comparison(
                select(ResumeComparison).where(ResumeComparison.id == comparison_id)
            )
        )
        if comparison is None:
            raise LookupError("对比记录不存在")
        return comparison

    def delete_comparison(self, comparison_id: int) -> None:
        comparison = self.get_comparison(comparison_id)
        if self._analyzing(SUBJECT_RESUME_COMPARISON, comparison.id):
            raise ValueError("对比分析进行中，请稍后再删")
        self._db.delete(comparison)
        self._db.commit()

    async def run_comparison(self, comparison_id: int) -> None:
        """后台任务体：跑发起时建好的那条对比运行，只写报告。"""
        started = time.monotonic()
        comparison = self.get_comparison(comparison_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_RESUME_COMPARISON, comparison.id)
        if run is None:
            logger.error("resume comparison has no run to execute: id=%s", comparison_id)
            return
        logger.info(
            "resume comparison started: id=%s run=%s owner=%s resumes=%s",
            comparison_id,
            run.id,
            self._owner_id,
            len(comparison.resume_ids),
        )
        try:
            report = (await finish_builtin(self._db, self._owner_id, run.id)).strip()
            if not report:
                raise RuntimeError("模型返回了空报告")

            comparison.report = report
            self._db.commit()
            logger.info(
                "resume comparison finished: id=%s report_chars=%s elapsed=%.1fs",
                comparison_id,
                len(report),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 同 run_analysis，失败写在运行上
            logger.warning(
                "resume comparison failed: id=%s elapsed=%.1fs error=%s",
                comparison_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

    # -- 求职助手（toolkit）----------------------------------------------------

    def _build_toolkit_snapshot(self, payload: ResumeToolkitCreate) -> dict:
        """把提交参数整理成可持久化的输入快照。

        简历来源在这里解析成最终喂模型的文本存进快照（``resolved_resume``）：
        任务此后自包含，即使原简历之后被删除，进行中的生成与历史记录都不受影响。
        """
        resume = None
        if payload.resume_id is not None:
            # get_resume 已按 owner 收敛；不存在的 id 抛 LookupError，路由转 404。
            resume = self.get_resume(payload.resume_id)

        snapshot = payload.model_dump()
        snapshot["resume_title"] = resume.title if resume else None
        content = ""
        if resume is not None:
            content = _truncate(resume.content)
        elif (payload.resume_text or "").strip():
            content = _truncate(payload.resume_text.strip())
        snapshot["resolved_resume"] = content
        return snapshot

    def create_toolkit_task(self, payload: ResumeToolkitCreate) -> ResumeToolkitTask:
        """校验必填项、落记录并建一条认领它的运行；模型调用由后台任务完成。"""
        spec = TOOLKIT_KINDS.get(payload.kind)
        if spec is None:
            raise ValueError(f"未知的任务类型：{payload.kind}")

        snapshot = self._build_toolkit_snapshot(payload)

        for field, label in spec.required_fields:
            if not (snapshot.get(field) or "").strip():
                raise ValueError(f"{spec.label}需要填写{label}")

        has_source = bool(snapshot["resolved_resume"])
        if spec.background_as_source and (snapshot.get("background") or "").strip():
            has_source = True
        if spec.needs_resume and not has_source:
            raise ValueError(
                f"{spec.label}需要简历内容：选择一份已上传的简历，或直接粘贴简历文本"
            )

        task = ResumeToolkitTask(
            owner_id=self._owner_id,
            kind=payload.kind,
            title=_toolkit_title(payload.kind, snapshot),
            inputs=snapshot,
        )
        self._db.add(task)
        self._db.commit()
        self._db.refresh(task)
        start_builtin(
            self._db,
            self._owner_id,
            spec.workflow_slug,
            _toolkit_inputs(payload.kind, snapshot),
            subject=(SUBJECT_RESUME_TOOLKIT, task.id),
        )
        logger.info(
            "toolkit task created: id=%s kind=%s owner=%s title=%r",
            task.id,
            task.kind,
            self._owner_id,
            task.title,
        )
        return task

    def list_toolkit_tasks(self) -> List[ResumeToolkitTask]:
        stmt = self._scope_toolkit(select(ResumeToolkitTask)).order_by(
            ResumeToolkitTask.id.desc()
        )
        return list(self._db.scalars(stmt).all())

    def get_toolkit_task(self, task_id: int) -> ResumeToolkitTask:
        task = self._db.scalar(
            self._scope_toolkit(
                select(ResumeToolkitTask).where(ResumeToolkitTask.id == task_id)
            )
        )
        if task is None:
            raise LookupError("生成记录不存在")
        return task

    def delete_toolkit_task(self, task_id: int) -> None:
        task = self.get_toolkit_task(task_id)
        if self._analyzing(SUBJECT_RESUME_TOOLKIT, task.id):
            raise ValueError("生成进行中，请稍后再删")
        self._db.delete(task)
        self._db.commit()

    async def run_toolkit_task(self, task_id: int) -> None:
        """后台任务体：跑认领这条任务的运行，只写报告。"""
        started = time.monotonic()
        task = self.get_toolkit_task(task_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_RESUME_TOOLKIT, task.id)
        if run is None:
            logger.error("toolkit task has no run to execute: id=%s", task_id)
            return
        logger.info(
            "toolkit task started: id=%s run=%s kind=%s owner=%s title=%r",
            task_id,
            run.id,
            task.kind,
            self._owner_id,
            task.title,
        )
        try:
            report = (await finish_builtin(self._db, self._owner_id, run.id)).strip()
            if not report:
                raise RuntimeError("模型返回了空报告")

            task.report = report
            self._db.commit()
            logger.info(
                "toolkit task finished: id=%s kind=%s report_chars=%s elapsed=%.1fs",
                task_id,
                task.kind,
                len(report),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 同上，失败写在运行上
            logger.warning(
                "toolkit task failed: id=%s kind=%s elapsed=%.1fs error=%s",
                task_id,
                task.kind,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

    # -- 序列化 -------------------------------------------------------------

    def _resume_titles(self, resume_ids: List[int]) -> List[str]:
        """把对比记录里的 id 快照解析成标题；已删除的简历标注为「已删除」。"""
        if not resume_ids:
            return []
        rows = self._db.scalars(
            self._scope(select(Resume).where(Resume.id.in_(resume_ids)))
        ).all()
        by_id = {row.id: row.title for row in rows}
        return [by_id.get(rid, "（已删除）") for rid in resume_ids]

    def _item_of(self, resume: Resume, run: Optional[WorkflowRun]) -> ResumeItem:
        status, error_msg = subject_state(run, has_product=bool(resume.report))
        return ResumeItem(
            id=resume.id,
            title=resume.title,
            description=resume.description,
            filename=resume.filename,
            mime=resume.mime,
            size=resume.size,
            status=status,
            error_msg=error_msg,
            has_report=bool(resume.report),
            has_netdisk=resume.netdisk_fs_id is not None,
            analyzed_at=resume.analyzed_at,
            created_at=resume.created_at,
            updated_at=resume.updated_at,
        )

    def to_item(self, resume: Resume) -> ResumeItem:
        return self._item_of(
            resume, latest_run(self._db, self._owner_id, SUBJECT_RESUME, resume.id)
        )

    def to_items(self, resumes: List[Resume]) -> List[ResumeItem]:
        """整页一次查回各自最近一次运行——列表不该每行问一次状态。"""
        runs = latest_runs(self._db, self._owner_id, SUBJECT_RESUME, [r.id for r in resumes])
        return [self._item_of(resume, runs.get(str(resume.id))) for resume in resumes]

    def to_detail(self, resume: Resume) -> ResumeDetail:
        return ResumeDetail(
            **self.to_item(resume).model_dump(),
            report=resume.report,
            suggestions=resume.suggestions,
        )

    def _comparison_item(
        self, comparison: ResumeComparison, run: Optional[WorkflowRun]
    ) -> ResumeComparisonItem:
        # idle 用 error：这类记录一创建就带着运行，没有运行又没有报告的行只剩
        # 「改造之前的存量」这一种解释，而它当时就是失败或已被重启收尾。
        status, error_msg = subject_state(
            run, has_product=bool(comparison.report), idle=SUBJECT_ERROR
        )
        return ResumeComparisonItem(
            id=comparison.id,
            title=comparison.title,
            resume_ids=comparison.resume_ids,
            resume_titles=self._resume_titles(comparison.resume_ids),
            status=status,
            error_msg=error_msg,
            created_at=comparison.created_at,
        )

    def to_comparison_item(self, comparison: ResumeComparison) -> ResumeComparisonItem:
        return self._comparison_item(
            comparison,
            latest_run(self._db, self._owner_id, SUBJECT_RESUME_COMPARISON, comparison.id),
        )

    def to_comparison_items(
        self, comparisons: List[ResumeComparison]
    ) -> List[ResumeComparisonItem]:
        runs = latest_runs(
            self._db, self._owner_id, SUBJECT_RESUME_COMPARISON, [c.id for c in comparisons]
        )
        return [
            self._comparison_item(comparison, runs.get(str(comparison.id)))
            for comparison in comparisons
        ]

    def to_comparison_detail(self, comparison: ResumeComparison) -> ResumeComparisonDetail:
        return ResumeComparisonDetail(
            **self.to_comparison_item(comparison).model_dump(),
            report=comparison.report,
        )

    def _toolkit_item(
        self, task: ResumeToolkitTask, run: Optional[WorkflowRun]
    ) -> ResumeToolkitItem:
        spec = TOOLKIT_KINDS.get(task.kind)
        status, error_msg = subject_state(
            run, has_product=bool(task.report), idle=SUBJECT_ERROR
        )
        return ResumeToolkitItem(
            id=task.id,
            kind=task.kind,
            kind_label=spec.label if spec else task.kind,
            title=task.title,
            status=status,
            error_msg=error_msg,
            created_at=task.created_at,
        )

    def to_toolkit_item(self, task: ResumeToolkitTask) -> ResumeToolkitItem:
        return self._toolkit_item(
            task, latest_run(self._db, self._owner_id, SUBJECT_RESUME_TOOLKIT, task.id)
        )

    def to_toolkit_items(self, tasks: List[ResumeToolkitTask]) -> List[ResumeToolkitItem]:
        runs = latest_runs(
            self._db, self._owner_id, SUBJECT_RESUME_TOOLKIT, [t.id for t in tasks]
        )
        return [self._toolkit_item(task, runs.get(str(task.id))) for task in tasks]

    def to_toolkit_detail(self, task: ResumeToolkitTask) -> ResumeToolkitDetail:
        inputs = dict(task.inputs or {})
        # 解析后的简历全文只是生成时的中间产物，不回传给前端。
        inputs.pop("resolved_resume", None)
        return ResumeToolkitDetail(
            **self.to_toolkit_item(task).model_dump(),
            inputs=inputs,
            report=task.report,
        )
