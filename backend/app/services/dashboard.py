"""首页工作台的概览聚合。

一次请求出齐各模块的数量：首页不必挨个调列表接口再数长度，也不会因为某个
模块拉取失败而整页空白。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    Agent,
    AiCallLog,
    AiChannel,
    ChatConversation,
    InterviewRecord,
    KbDocument,
    KbProject,
    Note,
    OpsDatabase,
    OpsServer,
    Resume,
    SlideDeck,
    Workflow,
    WorkflowRun,
)
from app.models.schemas import DashboardSummary


class DashboardService:
    """按调用方归属聚合各模块数量。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def summary(self, *, is_admin: bool) -> DashboardSummary:
        def owned_count(model) -> int:
            return int(
                self._db.scalar(
                    select(func.count(model.id)).where(model.owner_id == self._owner_id)
                )
                or 0
            )

        # 内置智能体挂在 owner_id=0 上、对所有人可用，计数把它算进来，否则
        # 「智能体 0」会和一个装着好几个可用助手的选择器对不上。
        agent_count = int(
            self._db.scalar(
                select(func.count(Agent.id)).where(
                    Agent.owner_id.in_([0, self._owner_id]),
                    Agent.enabled.is_(True),
                )
            )
            or 0
        )

        # 与主对话侧栏同一口径：绑定运维目标的问答会话属于运维页面，不算这里的「会话」。
        conversation_count = int(
            self._db.scalar(
                select(func.count(ChatConversation.id)).where(
                    ChatConversation.owner_id == self._owner_id,
                    ChatConversation.ops_target_type.is_(None),
                )
            )
            or 0
        )

        # 工作流与智能体同一口径：内置的挂在 owner_id=0 上、对所有人可用，
        # 停用的不算——界面上看不到它，数出来只对不上号。
        workflow_count = int(
            self._db.scalar(
                select(func.count(Workflow.id)).where(
                    Workflow.owner_id.in_([0, self._owner_id]),
                    Workflow.enabled.is_(True),
                )
            )
            or 0
        )

        # 今天发起过的运行次数：功能侧每次分析/生成都在这一张表上留一次，
        # 它才是「这套流水线真的在跑」的证据。按 created_at（发起时刻）算，
        # 跨天跑完的不重复计入完成那天。
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        run_count_today = int(
            self._db.scalar(
                select(func.count(WorkflowRun.id)).where(
                    WorkflowRun.owner_id == self._owner_id,
                    WorkflowRun.created_at >= today_start,
                )
            )
            or 0
        )

        summary = DashboardSummary(
            agent_count=agent_count,
            knowledge_project_count=owned_count(KbProject),
            knowledge_document_count=owned_count(KbDocument),
            conversation_count=conversation_count,
            resume_count=owned_count(Resume),
            slide_count=owned_count(SlideDeck),
            interview_count=owned_count(InterviewRecord),
            note_count=owned_count(Note),
            workflow_count=workflow_count,
            run_count_today=run_count_today,
            server_count=owned_count(OpsServer),
            database_count=owned_count(OpsDatabase),
        )

        if is_admin:
            # 网关的通道与调用日志都没有 owner_id —— 它们是平台级资产，口径本身
            # 就是全局的，所以只对管理员出数。
            summary.ai_channel_count = int(
                self._db.scalar(select(func.count(AiChannel.id))) or 0
            )
            window = AiCallLog.created_at >= today_start
            summary.ai_call_count_today = int(
                self._db.scalar(select(func.count(AiCallLog.id)).where(window)) or 0
            )
            summary.ai_token_count_today = int(
                self._db.scalar(select(func.sum(AiCallLog.total_tokens)).where(window)) or 0
            )

        return summary
