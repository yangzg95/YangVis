"""智能办公 · 简历的存取与 AI 分析。

数据访问按单个用户隔离（与 :class:`KnowledgeService` 同一约定）：所有查询都
经过 :meth:`ResumeService._scope`，handler 不直接接触 session。

分析与对比都是后台任务：一次调用要等模型读完整份（甚至几份）简历全文，远超
SPA 的 30s axios 超时，所以接口只负责落库一行状态记录，真正的模型调用在
``BackgroundTasks`` 里跑，前端靠轮询状态感知进度。
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from app.errors import BusinessError, CODE_CHAT_NOT_READY
from app.models.entities import (
    ModelConfig,
    Resume,
    ResumeComparison,
    ResumeToolkitTask,
)
from app.models.schemas import (
    ModelPurpose,
    ResumeComparisonDetail,
    ResumeComparisonItem,
    ResumeDetail,
    ResumeItem,
    ResumeToolkitCreate,
    ResumeToolkitDetail,
    ResumeToolkitItem,
)
from app.services.agents import AgentService
from app.services.chunking import UnsupportedFileType, extract_text, normalise
from app.services.providers import build_chat_model

logger = logging.getLogger("yangvis.resume")

# 简历的额外支持格式；纯文本格式直接复用知识库的抽取逻辑。
RESUME_SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown")

# 喂给模型的单份简历全文的截断上限。多数聊天模型窗口在 32k-128k token，
# 对比时最多 10 份简历，每份留 12000 字符，加上 prompt 也不会把窗口撑爆。
MAX_RESUME_CHARS = 12000

# 后台任务（分析/对比/求职助手）的模型调用超时。这些调用是非流式的——要
# 等整份报告生成完才返回，输出动辄上千 token，全局 30s 的 MODEL_HTTP_TIMEOUT
# 几乎必然超时（慢模型、长 JD 时尤甚），所以给 5 分钟。
MODEL_TASK_TIMEOUT = 300.0

# 状态值。不用 Enum：实体里存的是裸字符串，schema 也是裸字符串。
STATUS_UPLOADED = "uploaded"
STATUS_ANALYZING = "analyzing"
STATUS_READY = "ready"
STATUS_ERROR = "error"

# 分析与对比的人设（含输出格式要求）都在内置智能体上，随 seed 管线就地刷新；
# 这里只留 slug。变量内容（简历全文）走 human 消息，不进 system_prompt。
AGENT_SLUG_ANALYZE = "resume-analyzer"
AGENT_SLUG_COMPARE = "resume-compare"


# ---- 求职助手（toolkit）-------------------------------------------------------

@dataclass(frozen=True)
class ToolkitKindSpec:
    """一类求职助手生成任务的规则。

    ``needs_resume`` / ``allows_resume`` 约束「简历来源」（已上传简历或粘贴文本）；
    ``background_as_source`` 为真时，background 字段也能充当来源（职业匹配分析）。
    ``required_fields`` 是简历来源之外的必填项：(字段名, 中文名)。
    """

    label: str
    agent_slug: str
    needs_resume: bool = False
    allows_resume: bool = False
    background_as_source: bool = False
    required_fields: tuple = ()


TOOLKIT_KINDS: dict = {
    "optimize": ToolkitKindSpec(
        label="简历优化",
        agent_slug="resume-optimizer",
        needs_resume=True,
    ),
    "career-match": ToolkitKindSpec(
        label="职业匹配分析",
        agent_slug="career-matcher",
        needs_resume=True,
        background_as_source=True,
    ),
    "jd-match": ToolkitKindSpec(
        label="简历匹配审计",
        agent_slug="jd-match-auditor",
        needs_resume=True,
        required_fields=(("job_description", "职位描述"),),
    ),
    "interview-prep": ToolkitKindSpec(
        label="面试准备策略",
        agent_slug="interview-coach",
        allows_resume=True,
        required_fields=(("position", "岗位名称"),),
    ),
    "portfolio-plan": ToolkitKindSpec(
        label="证明构建计划",
        agent_slug="portfolio-planner",
        allows_resume=True,
        required_fields=(("position", "目标职位"),),
    ),
    "salary-negotiation": ToolkitKindSpec(
        label="薪资最大化框架",
        agent_slug="salary-negotiator",
        required_fields=(("offer_amount", "offer 年薪"),),
    ),
}


def _labeled_section(label: str, text: str) -> str:
    return f'{label}：\n"""\n{text.strip()}\n"""'


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


def _toolkit_prompt(kind: str, inputs: dict) -> str:
    """把输入快照组装成喂给模型的 human 消息。人设与输出格式在智能体上。"""
    content = inputs.get("resolved_resume") or ""
    position = (inputs.get("position") or "").strip()
    background = (inputs.get("background") or "").strip()
    notes = (inputs.get("notes") or "").strip()

    if kind == "optimize":
        head = f"目标岗位：{position}\n\n" if position else ""
        return head + _labeled_section("简历全文", content)

    if kind == "career-match":
        parts = []
        if content:
            parts.append(_labeled_section("简历全文", content))
        if background:
            parts.append(_labeled_section("个人背景补充", background))
        return "\n\n".join(parts)

    if kind == "jd-match":
        return (
            _labeled_section("职位描述", _truncate(inputs.get("job_description") or ""))
            + "\n\n"
            + _labeled_section("简历全文", content)
        )

    if kind == "interview-prep":
        parts = [f"目标岗位：{position}"]
        job_description = (inputs.get("job_description") or "").strip()
        if job_description:
            parts.append(_labeled_section("职位描述", _truncate(job_description)))
        if content:
            parts.append(_labeled_section("求职者简历全文", content))
        return "\n\n".join(parts)

    if kind == "portfolio-plan":
        parts = [f"目标职位：{position}"]
        if content:
            parts.append(_labeled_section("求职者简历全文", content))
        if background:
            parts.append(_labeled_section("个人背景补充", background))
        return "\n\n".join(parts)

    if kind == "salary-negotiation":
        parts = [f"我刚刚收到一份工作邀请，年薪：{(inputs.get('offer_amount') or '').strip()}"]
        if position:
            parts.append(f"岗位：{position}")
        if notes:
            parts.append(f"补充说明：{notes}")
        return "\n".join(parts)

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


def _task_error_text(exc: Exception) -> str:
    """把后台任务里的模型异常转成用户能看懂的失败原因。"""
    text = str(exc)
    if "timed out" in text.lower() or "timeout" in text.lower():
        return (
            "模型响应超时：本次生成内容较长，可重新发起试试；"
            "若反复出现，建议到「系统设置 → 模型管理」换用响应更快的模型。"
        )
    return text[:512]


def _parse_analysis(raw_text: str) -> tuple[str, List[str]]:
    """从模型输出里拆出报告和改进意见。

    模型并不总是守规矩：JSON 外面裹一层 ```json 代码栅栏、前后带寒暄，
    都是常态。先剥栅栏再截取第一个 ``{`` 到最后一个 ``}`` 之间的部分解析；
    实在不行就把整段输出当成报告，至少分析结果不会整个丢掉。
    """
    text = raw_text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            report = str(data.get("report") or "").strip()
            suggestions = [
                str(item).strip()
                for item in (data.get("suggestions") or [])
                if str(item).strip()
            ]
            if report:
                return report, suggestions
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # 模型没按约定输出 JSON：整段当成报告，格式异常的原因留在这里可查。
    logger.debug("analysis output was not valid json, falling back to raw text")
    return raw_text.strip(), []


class ResumeService:
    """针对单个用户的简历做增删改查与 AI 分析。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._agents = AgentService(db, owner_id)

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
        config = self._db.scalar(
            select(ModelConfig).where(
                ModelConfig.owner_id == self._owner_id,
                ModelConfig.purpose == ModelPurpose.CHAT.value,
                ModelConfig.is_default.is_(True),
            )
        )
        if config is None or not config.last_test_ok:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                "尚未配置可用的对话模型。请先到「系统设置 → 模型管理」添加并通过连通性测试。",
            )
        return config

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
            status=STATUS_UPLOADED,
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
        if resume.status == STATUS_ANALYZING:
            raise ValueError("简历正在分析中，请稍后再删")
        self._db.delete(resume)
        self._db.commit()
        logger.info("owner %s deleted resume %s (%s)", self._owner_id, resume_id, resume.title)

    def mark_netdisk(self, resume: Resume, fs_id: int, path: str) -> None:
        """网盘同步成功后回填原文件在网盘里的位置。"""
        resume.netdisk_fs_id = fs_id
        resume.netdisk_path = path
        self._db.commit()

    # -- AI 分析 -------------------------------------------------------------

    async def _ask(self, slug: str, user_prompt: str) -> str:
        """按 slug 取内置智能体的人设发起一轮问答，返回纯文本输出。

        人设（含输出格式要求）在 agent.system_prompt 上，模型配置与温度也跟着
        智能体走；简历全文这类变量内容放 human 消息。
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        agent = self._agents.require_by_slug(slug)
        config = self.require_chat_config()
        # 模型名与 base_url 是定位「为什么慢/为什么失败」的第一手线索；api_key 不进日志。
        logger.info(
            "calling chat model: agent=%s model=%s base_url=%s prompt_chars=%s owner=%s",
            slug,
            config.model_name,
            config.base_url,
            len(user_prompt),
            self._owner_id,
        )
        model = build_chat_model(
            config, temperature=agent.temperature / 100, timeout=MODEL_TASK_TIMEOUT
        )
        result = await model.ainvoke(
            [SystemMessage(content=agent.system_prompt), HumanMessage(content=user_prompt)]
        )
        text = result.content if hasattr(result, "content") else str(result)
        if isinstance(text, list):  # 部分 provider 返回 content block 列表
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in text
            )
        return str(text)

    def start_analysis(self, resume_id: int) -> Resume:
        """把简历置为分析中。真正的模型调用由后台任务完成。"""
        resume = self.get_resume(resume_id)
        if resume.status == STATUS_ANALYZING:
            raise ValueError("该简历正在分析中，请勿重复触发")
        resume.status = STATUS_ANALYZING
        resume.error_msg = None
        self._db.commit()
        self._db.refresh(resume)
        return resume

    async def run_analysis(self, resume_id: int) -> None:
        """后台任务体：调模型、写结果。任何异常都落到状态上而不是抛出去。"""
        started = time.monotonic()
        resume = self.get_resume(resume_id)
        logger.info(
            "resume analysis started: id=%s owner=%s title=%r content_chars=%s",
            resume_id,
            self._owner_id,
            resume.title,
            len(resume.content),
        )
        try:
            description = (
                f"上传者对这份简历的补充描述：{resume.description}\n\n"
                if resume.description
                else ""
            )
            user_prompt = f'{description}简历全文：\n"""\n{_truncate(resume.content)}\n"""'
            text = await self._ask(AGENT_SLUG_ANALYZE, user_prompt)
            report, suggestions = _parse_analysis(text)

            resume.report = report
            resume.suggestions = suggestions or None
            resume.status = STATUS_READY
            resume.error_msg = None
            resume.analyzed_at = datetime.now(timezone.utc)
            logger.info(
                "resume analysis finished: id=%s report_chars=%s suggestions=%s elapsed=%.1fs",
                resume_id,
                len(report),
                len(suggestions),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 状态字段就是这类错误的归宿
            logger.warning(
                "resume analysis failed: id=%s elapsed=%.1fs error=%s",
                resume_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            resume.status = STATUS_ERROR
            resume.error_msg = _task_error_text(exc)
        self._db.commit()

    # -- 简历对比 -------------------------------------------------------------

    def create_comparison(self, resume_ids: List[int], title: Optional[str]) -> ResumeComparison:
        resumes = [self.get_resume(rid) for rid in resume_ids]
        if len({r.id for r in resumes}) != len(resume_ids):
            raise ValueError("存在重复的简历")
        comparison = ResumeComparison(
            owner_id=self._owner_id,
            title=title or None,
            resume_ids=list(resume_ids),
            status=STATUS_ANALYZING,
        )
        self._db.add(comparison)
        self._db.commit()
        self._db.refresh(comparison)
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
        if comparison.status == STATUS_ANALYZING:
            raise ValueError("对比分析进行中，请稍后再删")
        self._db.delete(comparison)
        self._db.commit()

    async def run_comparison(self, comparison_id: int) -> None:
        """后台任务体：把多份简历全文喂给模型，落一份对比报告。"""
        started = time.monotonic()
        comparison = self.get_comparison(comparison_id)
        logger.info(
            "resume comparison started: id=%s owner=%s resumes=%s",
            comparison_id,
            self._owner_id,
            len(comparison.resume_ids),
        )
        try:
            resumes = [self.get_resume(rid) for rid in comparison.resume_ids]
            blocks = []
            for index, resume in enumerate(resumes, start=1):
                description = f"\n补充描述：{resume.description}" if resume.description else ""
                blocks.append(
                    f"=== 简历 {index}：{resume.title} ==={description}\n"
                    f"\"\"\"\n{_truncate(resume.content)}\n\"\"\""
                )
            user_prompt = (
                f"下面共 {len(resumes)} 份候选人的简历全文，请做一次横向对比分析。\n\n"
                + "\n\n".join(blocks)
            )
            report = (await self._ask(AGENT_SLUG_COMPARE, user_prompt)).strip()
            if not report:
                raise RuntimeError("模型返回了空报告")

            comparison.report = report
            comparison.status = STATUS_READY
            comparison.error_msg = None
            logger.info(
                "resume comparison finished: id=%s report_chars=%s elapsed=%.1fs",
                comparison_id,
                len(report),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 同上，落状态
            logger.warning(
                "resume comparison failed: id=%s elapsed=%.1fs error=%s",
                comparison_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            comparison.status = STATUS_ERROR
            comparison.error_msg = _task_error_text(exc)
        self._db.commit()

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
        """校验必填项并落一条 analyzing 记录；模型调用由后台任务完成。"""
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
            status=STATUS_ANALYZING,
        )
        self._db.add(task)
        self._db.commit()
        self._db.refresh(task)
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
        if task.status == STATUS_ANALYZING:
            raise ValueError("生成进行中，请稍后再删")
        self._db.delete(task)
        self._db.commit()

    async def run_toolkit_task(self, task_id: int) -> None:
        """后台任务体：按快照组装 prompt、调模型、落报告。异常落状态而不抛出。"""
        started = time.monotonic()
        task = self.get_toolkit_task(task_id)
        logger.info(
            "toolkit task started: id=%s kind=%s owner=%s title=%r",
            task_id,
            task.kind,
            self._owner_id,
            task.title,
        )
        try:
            spec = TOOLKIT_KINDS[task.kind]
            prompt = _toolkit_prompt(task.kind, task.inputs)
            report = (await self._ask(spec.agent_slug, prompt)).strip()
            if not report:
                raise RuntimeError("模型返回了空报告")

            task.report = report
            task.status = STATUS_READY
            task.error_msg = None
            logger.info(
                "toolkit task finished: id=%s kind=%s report_chars=%s elapsed=%.1fs",
                task_id,
                task.kind,
                len(report),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 同上，落状态
            logger.warning(
                "toolkit task failed: id=%s kind=%s elapsed=%.1fs error=%s",
                task_id,
                task.kind,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            task.status = STATUS_ERROR
            task.error_msg = _task_error_text(exc)
        self._db.commit()

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

    @staticmethod
    def to_item(resume: Resume) -> ResumeItem:
        return ResumeItem(
            id=resume.id,
            title=resume.title,
            description=resume.description,
            filename=resume.filename,
            mime=resume.mime,
            size=resume.size,
            status=resume.status,
            error_msg=resume.error_msg,
            has_report=bool(resume.report),
            has_netdisk=resume.netdisk_fs_id is not None,
            analyzed_at=resume.analyzed_at,
            created_at=resume.created_at,
            updated_at=resume.updated_at,
        )

    def to_detail(self, resume: Resume) -> ResumeDetail:
        return ResumeDetail(
            **self.to_item(resume).model_dump(),
            report=resume.report,
            suggestions=resume.suggestions,
        )

    def to_comparison_item(self, comparison: ResumeComparison) -> ResumeComparisonItem:
        return ResumeComparisonItem(
            id=comparison.id,
            title=comparison.title,
            resume_ids=comparison.resume_ids,
            resume_titles=self._resume_titles(comparison.resume_ids),
            status=comparison.status,
            error_msg=comparison.error_msg,
            created_at=comparison.created_at,
        )

    def to_comparison_detail(self, comparison: ResumeComparison) -> ResumeComparisonDetail:
        return ResumeComparisonDetail(
            **self.to_comparison_item(comparison).model_dump(),
            report=comparison.report,
        )

    @staticmethod
    def to_toolkit_item(task: ResumeToolkitTask) -> ResumeToolkitItem:
        spec = TOOLKIT_KINDS.get(task.kind)
        return ResumeToolkitItem(
            id=task.id,
            kind=task.kind,
            kind_label=spec.label if spec else task.kind,
            title=task.title,
            status=task.status,
            error_msg=task.error_msg,
            created_at=task.created_at,
        )

    def to_toolkit_detail(self, task: ResumeToolkitTask) -> ResumeToolkitDetail:
        inputs = dict(task.inputs or {})
        # 解析后的简历全文只是生成时的中间产物，不回传给前端。
        inputs.pop("resolved_resume", None)
        return ResumeToolkitDetail(
            **self.to_toolkit_item(task).model_dump(),
            inputs=inputs,
            report=task.report,
        )


def reset_stale_analysis(db: Session) -> None:
    """把上一个进程遗留的「分析中」标记为失败。

    后台任务活在 Gunicorn worker 里，进程一重启它们就没了；不收拾的话，
    这些行会永远停在 analyzing，用户既删不了也等不到结果。
    """
    # 逐张表收尾并计数：重启把这些任务变成 error 是重要的运维信号，不该静默发生。
    stale_resumes = db.execute(
        update(Resume)
        .where(Resume.status == STATUS_ANALYZING)
        .values(status=STATUS_ERROR, error_msg="服务重启导致分析中断，请重新生成")
    ).rowcount
    stale_comparisons = db.execute(
        update(ResumeComparison)
        .where(ResumeComparison.status == STATUS_ANALYZING)
        .values(status=STATUS_ERROR, error_msg="服务重启导致分析中断，请重新发起对比")
    ).rowcount
    stale_toolkit = db.execute(
        update(ResumeToolkitTask)
        .where(ResumeToolkitTask.status == STATUS_ANALYZING)
        .values(status=STATUS_ERROR, error_msg="服务重启导致生成中断，请重新发起")
    ).rowcount
    db.commit()

    if stale_resumes or stale_comparisons or stale_toolkit:
        logger.warning(
            "reset stale resume jobs after restart: resumes=%s comparisons=%s toolkit=%s",
            stale_resumes,
            stale_comparisons,
            stale_toolkit,
        )
