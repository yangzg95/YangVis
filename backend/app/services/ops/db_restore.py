"""还原备份 / 导入 SQL 脚本：把一个 .sql 文件逐句跑在一条可写会话上。

与「一键备份」互为逆向：那边产出的文件（头注释 → 建库切库 → 逐表 DDL → 分批多行
INSERT → 收尾）这里原样吃下，不加工、不重排——脚本作者的顺序就是执行的顺序。

口径与 Navicat 式批量执行一致：逐句独立、一句失败不中断，成功的语句各自已落盘
（可写会话是 autocommit）。差别在两处——
- 语句数量不受 ``_BATCH_MAX_STATEMENTS`` 约束：那 100 句的上限守的是手敲控制台，
  一份整库备份本来就几万句，用同一个上限等于没有还原入口。字节上限
  ``OPS_RESTORE_MAX_BYTES`` 才是这里的闸门。
- 会话时间放宽到 ``OPS_RESTORE_TIMEOUT``：一条几十万行的多行 INSERT 按交互通道的
  20 秒会被客户端先掐掉，服务端却还在写，那是最坏的一种假失败。

写闸门比控制台更硬：还原做的全是写，连接没开「允许写入」或用户没有 ops_write
通行证时直接拒，不退化成只读会话「试一下」——那只会把整份脚本变成一屏权限错误。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pymysql

from app.config import get_settings
from app.models.entities import OpsDatabase
from app.models.schemas import DatabaseType
from app.services.ops.database import OpsDbError, OpsDbForbidden
from app.services.ops.db_exec import _connect_mysql, _password_of
from app.services.ops.safety import classify_sql_writable, split_sql_script, strip_sql_comments
from app.timeutil import db_now

logger = logging.getLogger("yangvis.ops_database")

# 一批送进会话的语句数。它同时是三件事的粒度：进度回传的密度、客户端「停止」
# 能切进去的最小单位、以及一次 to_thread 阻塞线程池的时长。
CHUNK_STATEMENTS = 200

# 连续这么多句失败就停手：一份文件的开头就连环错，后面大概率是同一个原因
# （表已存在、库不对、权限不够），继续跑只是把几万条同样的错误刷进界面。
MAX_CONSECUTIVE_FAILURES = 20

# 进度事件里带走的失败样本数上限。全量错误没有意义（可能上万条），
# 界面要的是「先告诉我错在哪」，完整脚本的失败句数在摘要里。
MAX_ERROR_SAMPLES = 50

# 脚本里的查询语句不执行：还原要的是写，而 pymysql 的普通游标会把 SELECT 的
# 整个结果集读进内存——一条藏在脚本里的 ``SELECT * FROM big`` 就足以把服务端
# 顶穿。跳过比执行它更对，也如实计入「跳过」。
_QUERY_HEADS = ("select", "show", "desc", "describe", "explain", "with")

# 文件里出现这些语句时给界面一句提醒：默认库会跟着文件走，选择框只是起点。
_USE_HEAD = re.compile(r"^use\s+([`\"\w.$]+)", re.I)


@dataclass
class _RestoreItem:
    """一句待执行的脚本语句。

    ``code`` 是去掉注释之后的正文（拆分时注释会跟着下一句一起过来，而「这句是什么
    样的」只看代码部分）；``reason`` 非空表示它压根不该进数据库：安全网关拦下来的
    算失败，``skip=True`` 的查询语句只是不跑，不计进失败数。
    """

    sql: str
    code: str
    reason: Optional[str] = None
    skip: bool = False


class RestoreSession:
    """一次还原的现场：会话、语句队列、计数与收尾。

    连接从头持有到尾（脚本里的 ``USE`` 会改变会话默认库，中途换连接等于换库），
    由 :meth:`close` 统一收，流跑到头、超上限抛出、客户端断开三条路都走它。
    """

    def __init__(
        self,
        conn: Any,
        items: List[_RestoreItem],
        schema: Optional[str],
        uses: List[str],
        timeout_seconds: float,
    ) -> None:
        self.conn = conn
        self.items = items
        self.schema = schema
        # 脚本里 ``USE x`` 点到的库：执行到这里会话就跟着切走，界面要提前说。
        self.uses = uses
        self.timeout_seconds = timeout_seconds
        self.total = len(items)
        self.done = 0
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        # 网关/跳过导致压根没送进数据库的句数——审计与界面都区分得开。
        self.refused = 0
        self.errors: List[Dict[str, Any]] = []
        self.started_at = db_now()
        self.finished_at: Optional[datetime] = None
        self.elapsed_ms = 0
        self.stop_reason: Optional[str] = None
        self._consecutive = 0
        self._closed = False

    @property
    def running(self) -> bool:
        return self.done < self.total and self.stop_reason is None

    def close(self) -> None:
        """收尾。只认第一次：流跑到头它自己会关，异常与客户端断开还要再兜一次。"""
        if self._closed:
            return
        self._closed = True
        try:
            # autocommit 的会话没有半截事务要回滚，这句只是把游标状态归零；
            # 失败不盖住还原本身的结果。
            self.conn.rollback()
        except Exception:
            logger.debug("restore session rollback failed", exc_info=True)
        finally:
            try:
                self.conn.close()
            except Exception:
                logger.debug("restore session close failed", exc_info=True)

    def run_chunk(self, stop_on_error: bool = False) -> None:
        """在会话里连着跑最多 ``CHUNK_STATEMENTS`` 句。由调用方丢进线程池。

        单句失败不中断（成功的已经各自落盘），但连续失败打满上限、或
        ``stop_on_error`` 撞上第一个错时会提前收尾，剩下的语句不再执行。
        """
        started = time.perf_counter()
        end = min(self.done + CHUNK_STATEMENTS, self.total)
        try:
            with self.conn.cursor() as cur:
                while self.done < end:
                    item = self.items[self.done]
                    self.done += 1
                    if item.skip:
                        self.skipped += 1
                        continue
                    if item.reason is not None:
                        self._fail(item, item.reason, 0, executed=False)
                        if self._halted(stop_on_error):
                            break
                        continue
                    each = time.perf_counter()
                    try:
                        cur.execute(item.sql)
                    except pymysql.Error as exc:
                        self._fail(item, str(exc), int((time.perf_counter() - each) * 1000))
                        if self._halted(stop_on_error):
                            break
                        continue
                    self.succeeded += 1
                    self._consecutive = 0
        except Exception as exc:  # 连接本身断了：整批到此为止，别拿半截结果骗人
            self.stop_reason = f"会话中断：{exc}"
            # 只把正在执行的这句计入失败：后面没跑到的那些不算「错」，
            # 它们在摘要里露在 ``done < total`` 这个缺口中，界面说是「未执行」。
            self.failed += 1
        finally:
            self.elapsed_ms = int((time.perf_counter() - started) * 1000)

    def _fail(self, item: _RestoreItem, message: str, elapsed_ms: int, executed: bool = True) -> None:
        self.failed += 1
        self._consecutive += 1
        if not executed:
            self.refused += 1
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append(
                {
                    "index": self.done,
                    "sql": _preview(item.sql),
                    "message": message[:512],
                    "elapsed_ms": elapsed_ms,
                }
            )

    def _halted(self, stop_on_error: bool) -> bool:
        """这一句失败之后还要不要接着跑。"""
        if stop_on_error and self.failed:
            self.stop_reason = "已按要求在第一个错误处停止"
            return True
        if self._consecutive >= MAX_CONSECUTIVE_FAILURES:
            self.stop_reason = f"连续 {MAX_CONSECUTIVE_FAILURES} 句失败，已停止后续语句"
            return True
        return False

    def summary(self) -> Dict[str, Any]:
        """还原结果的一页摘要（进度流的最后一行与审计都读它）。"""
        self.finished_at = self.finished_at or db_now()
        return {
            "total": self.total,
            "done": self.done,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "refused": self.refused,
            # 停手之后压根没跑的句数：它们不是失败，但也不能当没发生。
            "not_run": self.total - self.done,
            "elapsed_ms": self.elapsed_ms,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "stopped": self.stop_reason is not None,
            "stop_reason": self.stop_reason,
            "errors": self.errors,
            "chunk_size": CHUNK_STATEMENTS,
            "stmt_timeout_seconds": self.timeout_seconds,
        }


def _preview(sql: str) -> str:
    """失败列表用的单行预览：还原脚本里一条 INSERT 可以几十万字，全量回传没人看。"""
    text = " ".join(sql.split())
    return text if len(text) <= 200 else text[:200] + "…"


def _query_statement(code: str) -> bool:
    body = code.lstrip("( \t\r\n").lower()
    return any(body.startswith(f"{word} ") or body.startswith(f"{word}(") for word in _QUERY_HEADS)


def _use_target(code: str) -> Optional[str]:
    match = _USE_HEAD.match(code.lstrip("( \t\r\n"))
    return match.group(1).strip("`\"") if match else None


def prepare_script(item: OpsDatabase, script: str, allow_write: bool) -> List[_RestoreItem]:
    """拆分 + 逐句定策（网关、查询跳过），不碰数据库。

    全脚本过不了写闸门时在这里就拒——连接都不该开。字节上限在收上传的那一步
    守（:func:`app.routers.ops.restore_database_script`），拆完再量就晚了。
    """
    if DatabaseType(item.db_type) is not DatabaseType.MYSQL:
        raise OpsDbError("Redis 连接没有脚本还原，键的导入请在数据页逐条粘贴")
    if not allow_write or not item.writable:
        raise OpsDbForbidden("还原需要连接开启「允许写入」，且你持有运维写权限")

    statements = split_sql_script(script)
    if not statements:
        raise OpsDbError("这个文件里没有可执行的语句")

    items: List[_RestoreItem] = []
    for sql in statements:
        # 判定看去注释之后的正文：备份文件开头那几行标题注释会跟着第一句一起过来，
        # 照着原文判就会把一句 SELECT 当成写语句送进游标——结果集整个进内存。
        code = strip_sql_comments(sql).strip()
        if _query_statement(code):
            # 查询语句没有要落盘的东西，跳过比执行它更对（结果集只会白吃内存）。
            items.append(_RestoreItem(sql=sql, code=code, skip=True))
            continue
        verdict = classify_sql_writable(sql)
        reason = None if verdict.allowed else (verdict.reason or "该语句被安全策略拒绝")
        items.append(_RestoreItem(sql=sql, code=code, reason=reason))
    return items


async def open_restore(
    item: OpsDatabase,
    script: str,
    schema: Optional[str] = None,
    *,
    allow_write: bool = False,
) -> RestoreSession:
    """备好几要执行的语句并开好可写会话。

    拆分、网关、写权限、连不上库这些都在流开始之前抛得出——那时还是正常的
    HTTP 业务错误；一旦开始吐进度，剩下的就只有逐句失败一种了。
    """
    settings = get_settings()
    items = prepare_script(item, script, allow_write)
    uses: List[str] = []
    for entry in items:
        if entry.reason is not None:
            continue
        target = _use_target(entry.code)
        if target and target not in uses:
            uses.append(target)

    timeout = float(settings.OPS_RESTORE_TIMEOUT)
    password = _password_of(item)
    try:
        conn = _connect_mysql(
            item, password, schema, writable=True, timeout_seconds=timeout, pool=False
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"连接数据库失败：{exc}") from exc
    return RestoreSession(conn, items, schema, uses, timeout)


def start_event(session: RestoreSession) -> Dict[str, Any]:
    """第一帧：让界面先说出总量与两个需要人知道的事实。"""
    return {
        "type": "start",
        "total": session.total,
        "chunk_size": CHUNK_STATEMENTS,
        "stmt_timeout_seconds": session.timeout_seconds,
        "schema": session.schema,
        # 脚本自带 USE 时，界面上的目标库只对 USE 之前的语句生效。
        "uses": session.uses,
    }


def progress_event(session: RestoreSession) -> Dict[str, Any]:
    return {
        "type": "progress",
        "done": session.done,
        "total": session.total,
        "succeeded": session.succeeded,
        "failed": session.failed,
        # 界面在跑到一半时也要能说出「这句只是没跑」和「这句是跳过」的差别，
        # 所以这两个计数跟着每一帧走，而不是只出现在最后一帧。
        "skipped": session.skipped,
        "not_run": session.total - session.done,
        # 本批新产生的失败样本（全局上限见 MAX_ERROR_SAMPLES）。
        "errors": session.errors[-MAX_ERROR_SAMPLES:] if session.errors else [],
        "stop_reason": session.stop_reason,
    }


async def stream_restore(session: RestoreSession, stop_on_error: bool = False) -> AsyncIterator[Dict[str, Any]]:
    """逐批执行并吐进度事件；最后一帧是 ``done``。

    客户端断开时 ``GeneratorExit`` 会落进 finally：会话关掉，已经落盘的语句留在库里
    （可写会话是 autocommit），正在跑的那一批则随连接一起断掉——线程不会被取消，
    剩下的语句会以「连接已关」失败若干句后自己收手。界面不能把「停止」说成已回滚，
    审计给的也是断开那一刻的计数。
    """
    try:
        yield start_event(session)
        while session.running:
            await asyncio.to_thread(session.run_chunk, stop_on_error)
            yield progress_event(session)
        yield {"type": "done", **session.summary()}
    finally:
        session.close()


async def collect_restore(session: RestoreSession, stop_on_error: bool = False) -> Dict[str, Any]:
    """不流式：整份跑完只给一页摘要（网盘里没有 .sql 可读，先留个口子给接口层复用）。"""
    result: Dict[str, Any] = {}
    async for event in stream_restore(session, stop_on_error):
        if event["type"] == "done":
            result = event
    return result or session.summary()
