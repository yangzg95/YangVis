"""智能体配置的版本快照。

人设（``agent`` 行本身）是「当前值」，被保存就原地改写；这里追加的
``agent_version`` 行是不可变的历史。两者的关系与 git 的工作区 / 提交相同：

- 新建智能体 → v1；
- 每次保存**影响模型输出的字段** → 追加一个版本（只翻开关 ``enabled``、
  调 ``sort_order`` 不产生新版本）；
- 内置智能体随代码升级而改变 → 启动 seed 时对账追加；
- 版本永不改写、不删除——智能体删了，版本行也留给历史运行对现场。

工作流执行时在 :func:`current_version_no` 取当时的最新版本号绑定到步骤行。
"""
from __future__ import annotations

import json
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Agent, AgentVersion

# 快照存全字段（name/description 是版本列表的辨认标签）；但**只有 TRACKED_FIELDS
# 变了才追加新版本**——改名、藏进选择器、调排序都不会改变模型的输出，为此制造
# 一个新版本纯属噪音。
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "system_prompt",
    "temperature",
    "use_knowledge",
    "use_ops",
    "use_memory",
    "chat_visible",
)

# 真正影响模型回答的字段：prompt、温度、知识库/运维工具/长期记忆三个上下文开关。
TRACKED_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "temperature",
    "use_knowledge",
    "use_ops",
    "use_memory",
)


def snapshot_of(agent: Agent) -> dict:
    """把一个智能体当前的人设字段收成可落库的普通字典。"""
    return {field: getattr(agent, field) for field in SNAPSHOT_FIELDS}


def _fingerprint(snapshot: dict) -> str:
    """影响输出的那部分人设：版本是否追加只看它。

    键序固定、非 ASCII 不转义；``default=str`` 兜底 None 以外的非常规值。
    """
    tracked = {field: snapshot.get(field) for field in TRACKED_FIELDS}
    return json.dumps(tracked, sort_keys=True, ensure_ascii=False, default=str)


def latest(db: Session, agent_id: int) -> Optional[AgentVersion]:
    return db.scalar(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())
        .limit(1)
    )


def current_version_no(db: Session, agent_id: int) -> Optional[int]:
    """步骤开跑时绑定的版本号。

    正常路径下版本必然存在（新建与 seed 都会补 v1）；返回 None 只可能发生在
    升级后的第一次执行之前，由调用方按「老运行，无版本」处理。
    """
    row = latest(db, agent_id)
    return row.version if row is not None else None


def append(db: Session, agent: Agent) -> AgentVersion:
    """按当前人设追加下一个版本号，调用方负责 commit。"""
    current = latest(db, agent.id)
    row = AgentVersion(
        owner_id=agent.owner_id,
        agent_id=agent.id,
        version=(current.version + 1) if current is not None else 1,
        snapshot=snapshot_of(agent),
    )
    db.add(row)
    db.flush()
    return row


def append_if_changed(db: Session, agent: Agent, before: dict) -> Optional[AgentVersion]:
    """保存前后人设字段有变化才追加版本；无变化（只动了开关）返回 None。"""
    if _fingerprint(before) == _fingerprint(snapshot_of(agent)):
        return None
    return append(db, agent)


def list_versions(db: Session, agent_id: int) -> List[AgentVersion]:
    return list(
        db.scalars(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        ).all()
    )


def get_version(db: Session, agent_id: int, version: int) -> Optional[AgentVersion]:
    return db.scalar(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id, AgentVersion.version == version
        )
    )


def ensure_versions(db: Session, agents: Sequence[Agent]) -> int:
    """给还没有任何版本的智能体补 v1（启动升级用），返回补了几个。

    已有版本的不动：存量智能体的「当前值」就是升级这一刻的值，补的这个 v1
    语义上是「功能上线时的人设」。
    """
    added = 0
    for agent in agents:
        if latest(db, agent.id) is None:
            append(db, agent)
            added += 1
    if added:
        db.commit()
    return added


def reconcile_builtins(db: Session, agents: Iterable[Agent]) -> int:
    """内置智能体 seed 刷新后对账：当前值与最新快照不同就追加新版本。

    全新安装时每个内置智能体只有 v1；代码里改过 prompt 的升级后启动会追加 v2、
    v3……内置人设的演变因此和用户自建人设走同一套历史。
    """
    added = 0
    for agent in agents:
        current = latest(db, agent.id)
        if current is None or _fingerprint(current.snapshot) != _fingerprint(snapshot_of(agent)):
            append(db, agent)
            added += 1
    if added:
        db.commit()
    return added
