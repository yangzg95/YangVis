"""运行对比评测：评分细则（rubric）的存取与 A/B 双运行评测。

玩法借自 llm-space：选同一条工作流的两次运行当证据，挑一把含 2–6 条标准的
尺子，对每条标准分别给 A、B 打 1–5 分，保存非加权均分与 ``B − A`` 差值，
总体结论由评测人自己写，不替人下判断。

两条不可变性：

* **标准快照冻结**——评测保存时把当时的标准列表原样写进 ``rubric_snapshot``，
  以后细则改名、增删标准甚至删除，历史评测对着的尺子不变。
* **运行被引用即豁免清理**——``runs.prune_runs`` 删老运行前会排除评测引用到的
  运行（含其步骤现场），否则留下的分数没有证据可复核。

同一对运行、同一把尺子重复提交走更新而不是新增：同一份证据改了主意，不该
长出两行。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.entities import (
    WorkflowEvalRubric,
    WorkflowEvaluation,
    WorkflowRun,
)
from app.models.schemas import (
    EvalCriterion,
    EvalRunBrief,
    EvalScore,
    EvaluationCreate,
    EvaluationItem,
    RubricItem,
    RubricUpsert,
)

logger = logging.getLogger("yangvis.workflows")


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    """去首尾空白；空串与纯空白归一为 None。"""
    return value.strip() or None if value else None


def _criteria_values(criteria: Sequence[EvalCriterion]) -> List[dict]:
    """落库的标准 JSON：去空格、空描述归一为 None。"""
    values: List[dict] = []
    seen: set[str] = set()
    for item in criteria:
        name = item.name.strip()
        if name in seen:
            raise ValueError(f"评分标准重名：{name}")
        seen.add(name)
        values.append({"name": name, "description": _clean_optional_text(item.description)})
    return values


class EvalService:
    """一个用户名下的评分细则与评测记录。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 评分细则 -------------------------------------------------------------

    def list_rubrics(self) -> List[WorkflowEvalRubric]:
        return list(
            self._db.scalars(
                select(WorkflowEvalRubric)
                .where(WorkflowEvalRubric.owner_id == self._owner_id)
                .order_by(WorkflowEvalRubric.id.desc())
            ).all()
        )

    def _owned_rubric(self, rubric_id: int) -> WorkflowEvalRubric:
        row = self._db.scalar(
            select(WorkflowEvalRubric).where(
                WorkflowEvalRubric.owner_id == self._owner_id,
                WorkflowEvalRubric.id == rubric_id,
            )
        )
        if row is None:
            raise LookupError("评分细则不存在")
        return row

    def create_rubric(self, payload: RubricUpsert) -> WorkflowEvalRubric:
        name = payload.name.strip()
        if not name:
            raise ValueError("评分细则名称不能为空")
        exists = self._db.scalar(
            select(WorkflowEvalRubric.id).where(
                WorkflowEvalRubric.owner_id == self._owner_id,
                WorkflowEvalRubric.name == name,
            )
        )
        if exists is not None:
            raise ValueError("已存在同名评分细则")

        row = WorkflowEvalRubric(
            owner_id=self._owner_id,
            name=name,
            description=_clean_optional_text(payload.description),
            criteria=_criteria_values(payload.criteria),
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info("owner %s created eval rubric %s (%s)", self._owner_id, row.id, name)
        return row

    def update_rubric(self, rubric_id: int, payload: RubricUpsert) -> WorkflowEvalRubric:
        """改细则只影响以后的评测：已保存评测拿着冻结快照，不会被改写。"""
        row = self._owned_rubric(rubric_id)
        name = payload.name.strip()
        if not name:
            raise ValueError("评分细则名称不能为空")
        clash = self._db.scalar(
            select(WorkflowEvalRubric.id).where(
                WorkflowEvalRubric.owner_id == self._owner_id,
                WorkflowEvalRubric.name == name,
                WorkflowEvalRubric.id != rubric_id,
            )
        )
        if clash is not None:
            raise ValueError("已存在同名评分细则")

        row.name = name
        row.description = _clean_optional_text(payload.description)
        row.criteria = _criteria_values(payload.criteria)
        self._db.commit()
        self._db.refresh(row)
        logger.info("owner %s updated eval rubric %s", self._owner_id, rubric_id)
        return row

    def delete_rubric(self, rubric_id: int) -> None:
        """删细则不删评测：评测行的 rubric_snapshot 自足，名字读侧取不到给 None。"""
        row = self._owned_rubric(rubric_id)
        self._db.delete(row)
        self._db.commit()
        logger.info("owner %s deleted eval rubric %s", self._owner_id, rubric_id)

    @staticmethod
    def to_rubric_item(row: WorkflowEvalRubric) -> RubricItem:
        return RubricItem(
            id=row.id,
            name=row.name,
            description=row.description,
            criteria=[EvalCriterion(**item) for item in (row.criteria or [])],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # -- 评测 -----------------------------------------------------------------

    def _owned_run(self, run_id: int, *, which: str) -> WorkflowRun:
        run = self._db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.owner_id == self._owner_id,
                WorkflowRun.id == run_id,
            )
        )
        if run is None:
            raise LookupError(f"{which}运行不存在")
        return run

    def list_for_run(
        self, run_id: int, *, with_run_id: Optional[int] = None
    ) -> List[WorkflowEvaluation]:
        """涉及某次运行的评测（它当 A 或当 B 都算），新的在前。

        ``with_run_id`` 给定时收敛成「这两次运行之间」的评测：对比页要的是
        这一对的历史分数，不关心它各自跟别人的对比。
        """
        run = self._owned_run(run_id, which="")
        stmt = select(WorkflowEvaluation).where(
            WorkflowEvaluation.owner_id == self._owner_id,
            or_(
                WorkflowEvaluation.run_a_id == run.id,
                WorkflowEvaluation.run_b_id == run.id,
            ),
        )
        if with_run_id is not None:
            other = self._owned_run(with_run_id, which="另一次")
            stmt = stmt.where(
                or_(
                    (WorkflowEvaluation.run_a_id == run.id)
                    & (WorkflowEvaluation.run_b_id == other.id),
                    (WorkflowEvaluation.run_a_id == other.id)
                    & (WorkflowEvaluation.run_b_id == run.id),
                )
            )
        return list(
            self._db.scalars(
                stmt.order_by(WorkflowEvaluation.id.desc())
            ).all()
        )

    def upsert_evaluation(self, payload: EvaluationCreate) -> Tuple[WorkflowEvaluation, bool]:
        """提交（或更新）一次对比评测，返回 ``(行, 是否新建)``。"""
        if payload.run_a_id == payload.run_b_id:
            raise ValueError("两次运行不能是同一次，请选择另一次运行作为对比")

        run_a = self._owned_run(payload.run_a_id, which="基准")
        run_b = self._owned_run(payload.run_b_id, which="对比")
        if run_a.workflow_id != run_b.workflow_id:
            raise ValueError("只能对比同一条工作流的两次运行：步骤不同，没法逐条对齐")

        rubric = self._owned_rubric(payload.rubric_id)
        snapshot = [dict(item) for item in (rubric.criteria or [])]

        scores = self._validate_scores(payload.scores, len(snapshot))

        avg_a = round(sum(item.score_a for item in scores) / len(scores), 2)
        avg_b = round(sum(item.score_b for item in scores) / len(scores), 2)
        note = payload.note.strip() if payload.note else None

        existing = self._db.scalar(
            select(WorkflowEvaluation).where(
                WorkflowEvaluation.owner_id == self._owner_id,
                WorkflowEvaluation.run_a_id == payload.run_a_id,
                WorkflowEvaluation.run_b_id == payload.run_b_id,
                WorkflowEvaluation.rubric_id == payload.rubric_id,
            )
        )
        if existing is not None:
            existing.rubric_snapshot = snapshot
            existing.scores = [item.model_dump() for item in scores]
            existing.avg_a = avg_a
            existing.avg_b = avg_b
            existing.note = note
            self._db.commit()
            self._db.refresh(existing)
            logger.info(
                "owner %s updated evaluation %s (run %s vs %s)",
                self._owner_id,
                existing.id,
                payload.run_a_id,
                payload.run_b_id,
            )
            return existing, False

        row = WorkflowEvaluation(
            owner_id=self._owner_id,
            workflow_id=run_a.workflow_id,
            rubric_id=payload.rubric_id,
            rubric_snapshot=snapshot,
            run_a_id=payload.run_a_id,
            run_b_id=payload.run_b_id,
            scores=[item.model_dump() for item in scores],
            avg_a=avg_a,
            avg_b=avg_b,
            note=note,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "owner %s created evaluation %s (run %s vs %s, rubric %s)",
            self._owner_id,
            row.id,
            payload.run_a_id,
            payload.run_b_id,
            payload.rubric_id,
        )
        return row, True

    @staticmethod
    def _validate_scores(raw: Sequence[EvalScore], criteria_count: int) -> List[EvalScore]:
        """打分必须与细则快照逐条对齐：下标不重不漏，每条两个 1–5 分。"""
        by_index: dict[int, EvalScore] = {}
        for item in raw:
            if item.index in by_index:
                raise ValueError(f"第 {item.index + 1} 条标准打了两次分")
            by_index[item.index] = item
        wanted = set(range(criteria_count))
        given = set(by_index)
        if given != wanted:
            missing = sorted(i + 1 for i in wanted - given)
            extra = sorted(i + 1 for i in given - wanted)
            details = []
            if missing:
                details.append(f"漏了第 {'、'.join(map(str, missing))} 条")
            if extra:
                details.append(f"多了第 {'、'.join(map(str, extra))} 条")
            raise ValueError("打分与评分标准对不上：" + "，".join(details))
        return [by_index[i] for i in range(criteria_count)]

    def delete_evaluation(self, evaluation_id: int) -> None:
        row = self._db.scalar(
            select(WorkflowEvaluation).where(
                WorkflowEvaluation.owner_id == self._owner_id,
                WorkflowEvaluation.id == evaluation_id,
            )
        )
        if row is None:
            raise LookupError("评测记录不存在")
        self._db.delete(row)
        self._db.commit()
        logger.info("owner %s deleted evaluation %s", self._owner_id, evaluation_id)

    def to_item(self, row: WorkflowEvaluation) -> EvaluationItem:
        runs = {
            run.id: run
            for run in self._db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.id.in_((row.run_a_id, row.run_b_id))
                )
            ).all()
        }
        run_a = runs.get(row.run_a_id)
        run_b = runs.get(row.run_b_id)
        rubric_name = self._db.scalar(
            select(WorkflowEvalRubric.name).where(
                WorkflowEvalRubric.id == row.rubric_id
            )
        )

        def brief(run: Optional[WorkflowRun]) -> EvalRunBrief:
            # run 被豁免清理，正常不会缺；万一点子背（手动删库），给个占位而不是 500。
            if run is None:
                return EvalRunBrief(
                    id=-1, status="unknown", subject_label=None, elapsed_ms=None,
                    created_at=row.created_at,
                )
            return EvalRunBrief(
                id=run.id,
                status=run.status,
                subject_label=run.subject_label,
                elapsed_ms=run.elapsed_ms,
                created_at=run.created_at,
            )

        return EvaluationItem(
            id=row.id,
            workflow_id=row.workflow_id,
            rubric_id=row.rubric_id,
            rubric_name=rubric_name,
            rubric_snapshot=[EvalCriterion(**item) for item in (row.rubric_snapshot or [])],
            run_a_id=row.run_a_id,
            run_b_id=row.run_b_id,
            run_a=brief(run_a),
            run_b=brief(run_b),
            scores=[EvalScore(**item) for item in (row.scores or [])],
            avg_a=row.avg_a,
            avg_b=row.avg_b,
            diff=round(row.avg_b - row.avg_a, 2),
            note=row.note,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

def referenced_run_ids(db: Session, run_ids: Sequence[int]) -> set[int]:
    """这批运行里被**任何**评测引用到的：留存清理的豁免名单（全站口径）。

    评测与运行同属一个用户，但启动清理走全站口径，这里不按 owner 收敛——
    被引用就豁免，查宽不查窄。
    """
    ids = list(run_ids)
    if not ids:
        return set()
    referenced: set[int] = set()
    for column in (WorkflowEvaluation.run_a_id, WorkflowEvaluation.run_b_id):
        referenced.update(
            db.scalars(select(column).where(column.in_(ids)).distinct()).all()
        )
    return referenced
