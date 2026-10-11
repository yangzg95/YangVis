"""智能办公 · 面试记录的存取与 AI 参考答案生成。

数据访问按单个用户隔离（与 :class:`ResumeService` 同一约定）：所有查询都经过
:meth:`InterviewService._scope`，handler 不直接接触 session。

参考答案生成是后台任务，与简历分析同一理由：一次模型调用远超 SPA 的 30s axios
超时。接口只建一条认领这道题（``subject_ref`` = qid）的运行，真正的模型调用在
``BackgroundTasks`` 里跑，前端轮询详情感知进度。

questions 是记录上的 JSON 子列表，写它的一共有三条路径，靠「各自只管各自的字
段」避开并发互相覆盖，不需要锁：

- ``PUT /interviews/{id}`` 只改元数据，根本不接收 questions；
- ``PUT .../questions/{qid}`` 只改 question/my_answer/note；
- 后台任务 :meth:`InterviewService.run_answer` 只按 qid 写那一题的 ``ref_answer``，
  且提交前 refresh 一次记录，把别人的改动先合并进来。

每题「生成到哪了/为什么失败」不在 JSON 里：那是认领它的那次运行的事，序列化时
翻译回 ``ref_status`` / ``ref_error`` 两个字段返回给前端。
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

from app.models.entities import InterviewRecord, ModelConfig, WorkflowRun
from app.models.schemas import (
    InterviewCreate,
    InterviewDetail,
    InterviewItem,
    InterviewQuestion,
    InterviewQuestionIn,
    InterviewUpdate,
)
from app.services.agent_run import (
    labeled_section,
    model_error_text,
    require_chat_model,
)
from app.services.workflow_builtin import builtin_output, start_builtin
from app.services.workflow_runs import (
    fail_run,
    latest_run,
    latest_runs,
    running_subjects,
    subject_state,
)
from app.services.workflows import SUBJECT_INTERVIEW_QUESTION

logger = logging.getLogger("yangvis.interview")

# 参考答案的人设（interview-answerer）与 Prompt 骨架都在内置工作流上，界面上能改；
# 这里只留「哪条流程」。
WORKFLOW_ANSWER = "interview-answer"

# 参考答案在接口上的状态词表（取值与改造前一致，前端不用跟着改）。这里只是**取值**
# 而不是存储：进度由认领该题的运行翻译出来，一题没有运行时才是 none。
REF_NONE = "none"


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
    }


def _find_question(questions: list, qid: str) -> Optional[dict]:
    for item in questions:
        if item.get("qid") == qid:
            return item
    return None


def _merge_ref_answer(questions: list, qid: str, answer: str) -> list:
    """按 qid 只改一题的 ``ref_answer``，返回新列表（调用方负责整体重赋值）。

    qid 已不存在（生成期间被用户删掉）时原样返回——结果无处安放，丢弃。
    """
    return [
        {**item, "ref_answer": answer} if item.get("qid") == qid else item
        for item in questions
    ]


def _answer_inputs(record: InterviewRecord, question: dict) -> dict:
    """把面试背景与题目整理成那条工作流声明的输入变量。

    「有轮次才加那一行、有当时的回答才附那一段」留在代码里拼：模板只做替换。
    """
    my_answer = (question.get("my_answer") or "").strip()
    return {
        "company": record.company,
        "position": record.position,
        "round_note": f"\n面试轮次：{record.round}" if record.round else "",
        "question": question.get("question") or "",
        "answer_note": labeled_section("求职者当时的回答", my_answer) if my_answer else "",
    }


class InterviewService:
    """针对单个用户的面试记录做增删改查与参考答案生成。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(InterviewRecord.owner_id == self._owner_id)

    # -- 模型门槛 ---------------------------------------------------------------

    def require_chat_config(self) -> ModelConfig:
        return require_chat_model(self._db, self._owner_id)

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
        if self._analyzing(record):
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

    def _analyzing(self, record: InterviewRecord) -> bool:
        """这场面试里还有题目在生成吗——删除的闸门读的是运行，不是 JSON 里的标记。"""
        return bool(
            running_subjects(
                self._db,
                self._owner_id,
                SUBJECT_INTERVIEW_QUESTION,
                [str(q.get("qid") or "") for q in record.questions or []],
            )
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
        # ref_answer 原样保留：这条写路径只负责用户手填的三个字段，生成结果另有
        # 一条写路径（run_answer），两边各改各的键。
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
        if running_subjects(self._db, self._owner_id, SUBJECT_INTERVIEW_QUESTION, [qid]):
            raise ValueError("该题正在生成参考答案，请稍后再删")
        record.questions = [q for q in record.questions or [] if q.get("qid") != qid]
        self._db.commit()
        self._db.refresh(record)
        return record

    # -- AI 参考答案 -------------------------------------------------------------

    def start_answer(self, record_id: int, qid: str) -> InterviewRecord:
        """为这道题建一条认领它的运行；模型调用由后台任务完成。

        以前这里写的是 JSON 里的 ``ref_status``，前端据此转圈；现在同一件事只有
        运行记录一份，``ref_status`` 是序列化时从它翻译出来的。
        """
        record = self.get_record(record_id)
        target = _find_question(record.questions or [], qid)
        if target is None:
            raise LookupError("问题不存在")
        if running_subjects(self._db, self._owner_id, SUBJECT_INTERVIEW_QUESTION, [qid]):
            raise ValueError("该题正在生成中，请勿重复触发")
        start_builtin(
            self._db,
            self._owner_id,
            WORKFLOW_ANSWER,
            _answer_inputs(record, target),
            subject=(SUBJECT_INTERVIEW_QUESTION, qid),
            # 题干存在记录行的 JSON 里，运行记录按 qid 反查不到它，发起时抄一份。
            subject_label=f"{record.company} · {record.position} · {target.get('question') or ''}",
        )
        self._db.refresh(record)
        return record

    async def run_answer(self, record_id: int, qid: str) -> None:
        """后台任务体：跑发起时建好的那条运行，只写 ``ref_answer``。"""
        started = time.monotonic()
        record = self.get_record(record_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_INTERVIEW_QUESTION, qid)
        if run is None:
            logger.error("interview answer has no run to execute: record=%s qid=%s", record_id, qid)
            return
        logger.info(
            "interview answer started: record=%s qid=%s run=%s owner=%s company=%r",
            record_id,
            qid,
            run.id,
            self._owner_id,
            record.company,
        )
        try:
            target = _find_question(record.questions or [], qid)
            if target is None:
                logger.info("interview answer skipped: record=%s qid=%s deleted", record_id, qid)
                return
            answer = (await builtin_output(self._db, self._owner_id, run.id)).strip()
            if not answer:
                raise RuntimeError("模型返回了空回答")

            # 生成期间用户可能改过别的题或元数据：先 refresh 合并，再只动 ref_answer。
            # 记录也可能已被整条删除——删了就到此为止，结果没有地方可写。
            if not self._refresh_alive(record, record_id, qid):
                return
            record.questions = _merge_ref_answer(record.questions or [], qid, answer)
            self._db.commit()
            logger.info(
                "interview answer finished: record=%s qid=%s answer_chars=%s elapsed=%.1fs",
                record_id,
                qid,
                len(answer),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 失败写在运行上，不留到这里抛出
            logger.warning(
                "interview answer failed: record=%s qid=%s elapsed=%.1fs error=%s",
                record_id,
                qid,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

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
        questions = record.questions or []
        # 一次查询取回这些题各自最近一次运行，再逐题翻译成状态。
        runs = latest_runs(
            self._db,
            self._owner_id,
            SUBJECT_INTERVIEW_QUESTION,
            [str(q.get("qid") or "") for q in questions],
        )
        return InterviewDetail(
            **self.to_item(record).model_dump(),
            questions=[
                self._question_of(question, runs.get(str(question.get("qid") or "")))
                for question in questions
            ],
        )

    @staticmethod
    def _question_of(question: dict, run: Optional[WorkflowRun]) -> InterviewQuestion:
        ref_status, ref_error = subject_state(
            run, has_product=bool(question.get("ref_answer")), idle=REF_NONE
        )
        return InterviewQuestion(
            qid=str(question.get("qid") or ""),
            question=str(question.get("question") or ""),
            my_answer=question.get("my_answer"),
            note=question.get("note"),
            ref_answer=question.get("ref_answer"),
            ref_status=ref_status,
            ref_error=ref_error,
        )
