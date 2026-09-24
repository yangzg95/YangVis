"""智能办公 · 面试记录的存取与 AI 参考答案生成。

数据访问按单个用户隔离（与 :class:`ResumeService` 同一约定）：所有查询都经过
:meth:`InterviewService._scope`，handler 不直接接触 session。

参考答案生成是后台任务，与简历分析同一理由：一次模型调用远超 SPA 的 30s axios
超时。接口只负责把对应题目的 ``ref_status`` 置为 analyzing，真正的模型调用在
``BackgroundTasks`` 里跑，前端轮询详情感知进度。

questions 是记录上的 JSON 子列表，写它的一共有三条路径，靠「各自只管各自的字
段」避开并发互相覆盖，不需要锁：

- ``PUT /interviews/{id}`` 只改元数据，根本不接收 questions；
- ``PUT .../questions/{qid}`` 只改 question/my_answer/note，ref_* 原样保留；
- 后台任务 :meth:`InterviewService.run_answer` 只改对应 qid 的 ref_* 三键，
  且提交前 refresh 一次记录，把别人的改动先合并进来。
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import List, Optional

from sqlalchemy import Select, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from app.errors import BusinessError, CODE_CHAT_NOT_READY
from app.models.entities import InterviewRecord, ModelConfig
from app.models.schemas import (
    InterviewCreate,
    InterviewDetail,
    InterviewItem,
    InterviewQuestion,
    InterviewQuestionIn,
    InterviewUpdate,
    ModelPurpose,
)
from app.services.agents import AgentService
from app.services.providers import build_chat_model
from app.services.resume import MODEL_TASK_TIMEOUT, _labeled_section, _task_error_text

logger = logging.getLogger("yangvis.interview")

# 参考答案生成的人设在内置智能体上，随 seed 管线就地刷新；这里只留 slug。
AGENT_SLUG_ANSWER = "interview-answerer"

# 每题 AI 参考答案的状态值。不用 Enum：questions 里存的是裸 JSON。
REF_NONE = "none"
REF_ANALYZING = "analyzing"
REF_READY = "ready"
REF_ERROR = "error"

STALE_ANSWER_ERROR = "服务重启导致生成中断，请重新生成"


# ---- 纯函数（questions JSON 的读写都过这里，方便单测）--------------------------

def _new_qid() -> str:
    """一题一个服务端 id：题目级端点与后台回写都按它寻址，不依赖数组下标。"""
    return secrets.token_hex(6)


def _new_question(payload: InterviewQuestionIn) -> dict:
    return {
        "qid": _new_qid(),
        "question": payload.question.strip(),
        "my_answer": (payload.my_answer or "").strip() or None,
        "note": (payload.note or "").strip() or None,
        "ref_answer": None,
        "ref_status": REF_NONE,
        "ref_error": None,
    }


def _find_question(questions: list, qid: str) -> Optional[dict]:
    for item in questions:
        if item.get("qid") == qid:
            return item
    return None


def _merge_ref_fields(
    questions: list,
    qid: str,
    *,
    status: str,
    answer: Optional[str],
    error: Optional[str],
) -> list:
    """按 qid 只改一题的 ref_* 三键，返回新列表（调用方负责整体重赋值）。

    qid 已不存在（生成期间被用户删掉）时原样返回——结果无处安放，丢弃。
    """
    merged = []
    for item in questions:
        if item.get("qid") == qid:
            item = {**item, "ref_status": status, "ref_answer": answer, "ref_error": error}
        merged.append(item)
    return merged


def _reset_stale_questions(questions: list) -> tuple[list, int]:
    """把残留的 analyzing 题目标记为失败，返回（新列表, 修复数）。

    后台任务活在 web worker 里，进程一重启它们就没了；不收拾的话这些题目会
    永远停在 analyzing，前端按钮一直转圈，用户也删不掉这条记录。
    """
    fixed, count = [], 0
    for item in questions:
        if item.get("ref_status") == REF_ANALYZING:
            item = {**item, "ref_status": REF_ERROR, "ref_error": STALE_ANSWER_ERROR}
            count += 1
        fixed.append(item)
    return fixed, count


def _answer_prompt(record: InterviewRecord, question: dict) -> str:
    """把面试背景与题目组装成喂给模型的 human 消息。人设在智能体上。"""
    head = f"应聘公司：{record.company}\n面试岗位：{record.position}"
    if record.round:
        head += f"\n面试轮次：{record.round}"
    parts = [head, _labeled_section("面试问题", question.get("question") or "")]
    my_answer = (question.get("my_answer") or "").strip()
    if my_answer:
        parts.append(_labeled_section("求职者当时的回答", my_answer))
    return "\n\n".join(parts)


class InterviewService:
    """针对单个用户的面试记录做增删改查与参考答案生成。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._agents = AgentService(db, owner_id)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(InterviewRecord.owner_id == self._owner_id)

    # -- 模型配置（与 ResumeService 同一道门槛）----------------------------------

    def require_chat_config(self) -> ModelConfig:
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

    async def _ask(self, slug: str, user_prompt: str) -> str:
        """按 slug 取内置智能体的人设发起一轮问答，返回纯文本输出。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        agent = self._agents.require_by_slug(slug)
        config = self.require_chat_config()
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

    # -- 面试场次 CRUD ----------------------------------------------------------

    def list_records(self) -> List[InterviewRecord]:
        stmt = self._scope(select(InterviewRecord)).order_by(InterviewRecord.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_record(self, record_id: int) -> InterviewRecord:
        record = self._db.scalar(
            self._scope(select(InterviewRecord).where(InterviewRecord.id == record_id))
        )
        if record is None:
            raise LookupError("面试记录不存在")
        return record

    def create_record(self, payload: InterviewCreate) -> InterviewRecord:
        record = InterviewRecord(
            owner_id=self._owner_id,
            company=payload.company.strip(),
            position=payload.position.strip(),
            interview_date=payload.interview_date,
            round=(payload.round or "").strip() or None,
            result=payload.result,
            notes=(payload.notes or "").strip() or None,
            questions=[_new_question(item) for item in payload.questions],
        )
        self._db.add(record)
        self._db.commit()
        self._db.refresh(record)
        logger.info(
            "owner %s created interview record %s (%s / %s, %d question(s))",
            self._owner_id,
            record.id,
            record.company,
            record.position,
            len(record.questions),
        )
        return record

    def update_record(self, record_id: int, payload: InterviewUpdate) -> InterviewRecord:
        record = self.get_record(record_id)
        # exclude_unset：显式传 null 的字段要真的清空（如把面试日期抹掉），
        # 没传的字段保持不动。
        for field, value in payload.model_dump(exclude_unset=True).items():
            if isinstance(value, str):
                value = value.strip() or None
            # 公司 / 岗位是 NOT NULL：min_length=1 挡不住纯空格，清出 None 就跳过。
            if value is None and field in ("company", "position"):
                continue
            setattr(record, field, value)
        self._db.commit()
        self._db.refresh(record)
        return record

    def delete_record(self, record_id: int) -> None:
        record = self.get_record(record_id)
        if any(q.get("ref_status") == REF_ANALYZING for q in record.questions or []):
            raise ValueError("有题目正在生成参考答案，请稍后再删")
        self._db.delete(record)
        self._db.commit()
        logger.info(
            "owner %s deleted interview record %s (%s / %s)",
            self._owner_id,
            record_id,
            record.company,
            record.position,
        )

    # -- 题目 CRUD（都返回整条记录，handler 序列化成详情直接回传）--------------------

    def add_question(self, record_id: int, payload: InterviewQuestionIn) -> InterviewRecord:
        record = self.get_record(record_id)
        record.questions = [*(record.questions or []), _new_question(payload)]
        self._db.commit()
        self._db.refresh(record)
        return record

    def update_question(
        self, record_id: int, qid: str, payload: InterviewQuestionIn
    ) -> InterviewRecord:
        record = self.get_record(record_id)
        questions = record.questions or []
        target = _find_question(questions, qid)
        if target is None:
            raise LookupError("问题不存在")
        # ref_* 原样保留：这条写路径只负责用户手填的三个字段。
        updated = {
            **target,
            "question": payload.question.strip(),
            "my_answer": (payload.my_answer or "").strip() or None,
            "note": (payload.note or "").strip() or None,
        }
        record.questions = [updated if q.get("qid") == qid else q for q in questions]
        self._db.commit()
        self._db.refresh(record)
        return record

    def delete_question(self, record_id: int, qid: str) -> InterviewRecord:
        record = self.get_record(record_id)
        target = _find_question(record.questions or [], qid)
        if target is None:
            raise LookupError("问题不存在")
        if target.get("ref_status") == REF_ANALYZING:
            raise ValueError("该题正在生成参考答案，请稍后再删")
        record.questions = [q for q in record.questions or [] if q.get("qid") != qid]
        self._db.commit()
        self._db.refresh(record)
        return record

    # -- AI 参考答案 -------------------------------------------------------------

    def start_answer(self, record_id: int, qid: str) -> InterviewRecord:
        """把指定题目标记为生成中。真正的模型调用由后台任务完成。"""
        record = self.get_record(record_id)
        target = _find_question(record.questions or [], qid)
        if target is None:
            raise LookupError("问题不存在")
        if target.get("ref_status") == REF_ANALYZING:
            raise ValueError("该题正在生成中，请勿重复触发")
        record.questions = _merge_ref_fields(
            record.questions or [], qid, status=REF_ANALYZING, answer=None, error=None
        )
        self._db.commit()
        self._db.refresh(record)
        return record

    async def run_answer(self, record_id: int, qid: str) -> None:
        """后台任务体：调模型、回写 ref_*。任何异常都落到题目状态上而不是抛出去。"""
        started = time.monotonic()
        record = self.get_record(record_id)
        logger.info(
            "interview answer started: record=%s qid=%s owner=%s company=%r",
            record_id,
            qid,
            self._owner_id,
            record.company,
        )
        try:
            target = _find_question(record.questions or [], qid)
            if target is None:
                logger.info("interview answer skipped: record=%s qid=%s deleted", record_id, qid)
                return
            answer = (await self._ask(AGENT_SLUG_ANSWER, _answer_prompt(record, target))).strip()
            if not answer:
                raise RuntimeError("模型返回了空回答")

            # 生成期间用户可能改过别的题或元数据：先 refresh 合并，再只动 ref_*。
            # 记录也可能已被整条删除——删了就到此为止，状态没有地方可写。
            if not self._refresh_alive(record, record_id, qid):
                return
            record.questions = _merge_ref_fields(
                record.questions or [], qid, status=REF_READY, answer=answer, error=None
            )
            logger.info(
                "interview answer finished: record=%s qid=%s answer_chars=%s elapsed=%.1fs",
                record_id,
                qid,
                len(answer),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 状态字段就是这类错误的归宿
            logger.warning(
                "interview answer failed: record=%s qid=%s elapsed=%.1fs error=%s",
                record_id,
                qid,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            if not self._refresh_alive(record, record_id, qid):
                return
            record.questions = _merge_ref_fields(
                record.questions or [],
                qid,
                status=REF_ERROR,
                answer=None,
                error=_task_error_text(exc),
            )
        self._db.commit()

    def _refresh_alive(self, record: InterviewRecord, record_id: int, qid: str) -> bool:
        """refresh 合并并发改动；记录在生成期间被删除时返回 False，调用方直接收工。"""
        try:
            self._db.refresh(record)
        except (InvalidRequestError, ObjectDeletedError):
            logger.info(
                "interview answer dropped: record=%s qid=%s record deleted during generation",
                record_id,
                qid,
            )
            return False
        return True

    # -- 序列化 ---------------------------------------------------------------

    @staticmethod
    def to_item(record: InterviewRecord) -> InterviewItem:
        return InterviewItem(
            id=record.id,
            company=record.company,
            position=record.position,
            interview_date=record.interview_date,
            round=record.round,
            result=record.result,
            notes=record.notes,
            question_count=len(record.questions or []),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def to_detail(self, record: InterviewRecord) -> InterviewDetail:
        questions = [
            InterviewQuestion(
                qid=str(q.get("qid") or ""),
                question=str(q.get("question") or ""),
                my_answer=q.get("my_answer"),
                note=q.get("note"),
                ref_answer=q.get("ref_answer"),
                ref_status=str(q.get("ref_status") or REF_NONE),
                ref_error=q.get("ref_error"),
            )
            for q in record.questions or []
        ]
        return InterviewDetail(**self.to_item(record).model_dump(), questions=questions)


def reset_stale_answers(db: Session) -> None:
    """把上一个进程遗留的「生成中」题目标记为失败。

    不在 SQL 层筛：JSON 在不同方言里的文本化写法不一致（MySQL 的 CAST AS
    CHAR、SQLite 的原生 TEXT），为启动时跑一次的任务维护方言分支不值得——
    面试记录是人均几十条的小表，全量扫一遍就行。
    """
    total = 0
    for record in db.scalars(select(InterviewRecord)).all():
        questions, count = _reset_stale_questions(record.questions or [])
        if count:
            record.questions = questions
            total += count
    if total:
        db.commit()
        logger.warning("reset stale interview answer jobs after restart: questions=%s", total)
