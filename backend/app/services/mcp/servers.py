"""MCP 服务的存取，按单个用户隔离。

与 ``model_config`` 同一套口径：所有查询经 :meth:`McpServerService._scope`
收敛到当前 owner，密钥只以 Fernet 密文落库、回参逐值打码。

差异在于 MCP 的凭据不是单个字符串而是两个 JSON 对象（stdio 的 env、远端
服务的 headers），所以掩码合并按 dict 的键做：掩码回传保原值、空值删键、
新明文覆盖。
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import McpServer
from app.models.schemas import (
    McpImportReport,
    McpServerCreate,
    McpServerItem,
    McpServerUpdate,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.mcp_servers")

TRANSPORT_STDIO = "stdio"
TRANSPORT_SSE = "sse"
TRANSPORT_HTTP = "streamable_http"
_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_SSE, TRANSPORT_HTTP)


def validate_transport_fields(
    transport: str, *, command: Optional[str], url: Optional[str]
) -> None:
    """跨字段必填：stdio 必须有启动命令；sse/streamable_http 必须有服务 URL。"""
    if transport == TRANSPORT_STDIO:
        if not (command or "").strip():
            raise ValueError("stdio 类型必须填写启动命令")
    elif transport in (TRANSPORT_SSE, TRANSPORT_HTTP):
        if not (url or "").strip():
            raise ValueError(f"{transport} 类型必须填写服务 URL")
    else:
        raise ValueError(f"不支持的传输类型：{transport}")


def read_secret(blob: Optional[bytes]) -> Dict[str, str]:
    """解出一个密钥袋（JSON 对象）；没有密文就是空 dict。"""
    if not blob:
        return {}
    return json.loads(decrypt(blob))


def merge_secret(existing_blob: Optional[bytes], incoming: Dict[str, str]) -> bytes:
    """把编辑后的 dict 合并进已有密钥袋，返回新密文。

    - 值是空串 → 删除该键；
    - 值是 ``is_masked`` 的掩码（界面原样回传）→ 保持库里的原值；
    - 其他值 → 当作新明文覆盖。
    """
    merged = read_secret(existing_blob)
    for key, value in incoming.items():
        if value == "":
            merged.pop(key, None)
        elif is_masked(value):
            continue
        else:
            merged[key] = value
    return encrypt(json.dumps(merged, ensure_ascii=False))


# 远端 type 字段（2025-03-26 规约）到内部传输名的映射。
# key 统一走 lower()，所以驼峰 streamableHttp 归一成 streamablehttp——
# 千帆/Cline 导出的文档用的就是这个写法。
_REMOTE_TYPE_ALIASES = {
    "sse": TRANSPORT_SSE,
    "http": TRANSPORT_HTTP,
    "streamable-http": TRANSPORT_HTTP,
    "streamable_http": TRANSPORT_HTTP,
    "streamablehttp": TRANSPORT_HTTP,
}


def parse_mcp_config(
    doc: dict,
) -> tuple[List[McpServerCreate], List[str]]:
    """把 mcpServers 标准文档解析成一批创建参数。

    单条配置有问题只收集 ``"名称: 原因"`` 错误串、不中断整批；文档整体
    形态不对（不是对象 / mcpServers 不是对象）才抛 ValueError。
    顶层是裸的 server dict（没有 mcpServers 包裹）也接受。
    """
    if not isinstance(doc, dict):
        raise ValueError("配置必须是 JSON 对象")
    servers_doc = doc.get("mcpServers", doc)
    if not isinstance(servers_doc, dict):
        raise ValueError("mcpServers 必须是对象")

    entries: List[McpServerCreate] = []
    errors: List[str] = []

    for raw_name, entry in servers_doc.items():
        name = str(raw_name or "").strip()
        label = name or str(raw_name)
        try:
            if not name:
                raise ValueError("服务名称为空")
            if not isinstance(entry, dict):
                raise ValueError("配置项必须是对象")

            entry_type = entry.get("type")
            entry_type_key = str(entry_type).strip().lower() if entry_type is not None else None
            url = entry.get("url")
            command = entry.get("command")

            if url is not None:
                if entry_type is None:
                    # 缺 type 时按 URL 形态推断：路径含 /sse 视为 SSE，否则按新规约 HTTP。
                    transport = TRANSPORT_SSE if "/sse" in str(url) else TRANSPORT_HTTP
                elif entry_type_key in _REMOTE_TYPE_ALIASES:
                    transport = _REMOTE_TYPE_ALIASES[entry_type_key]
                else:
                    raise ValueError(f"不支持的 type：{entry_type}")
                headers = entry.get("headers", {})
                if not isinstance(headers, dict):
                    raise ValueError("headers 必须是对象")
                payload = McpServerCreate(
                    name=name,
                    transport=transport,
                    url=url,
                    headers={str(key): str(value) for key, value in headers.items()},
                )
            elif command is not None or entry_type_key in (None, "stdio"):
                if entry_type is not None and entry_type_key != "stdio":
                    raise ValueError(f"不支持的 type：{entry_type}")
                if command is None:
                    # type=stdio 却没有 command。
                    raise ValueError("缺少 command 或 url")
                args = entry.get("args", [])
                if not isinstance(args, list) or not all(
                    isinstance(arg, (str, int, float)) for arg in args
                ):
                    raise ValueError("args 必须是字符串数组")
                env = entry.get("env", {})
                if not isinstance(env, dict):
                    raise ValueError("env 必须是对象")
                payload = McpServerCreate(
                    name=name,
                    transport=TRANSPORT_STDIO,
                    command=command,
                    args=[str(arg) for arg in args],
                    env={str(key): str(value) for key, value in env.items()},
                )
            else:
                raise ValueError(f"不支持的 type：{entry_type}")

            validate_transport_fields(
                payload.transport, command=payload.command, url=payload.url
            )
            entries.append(payload)
        except (ValueError, ValidationError) as exc:
            errors.append(f"{label}: {str(exc).splitlines()[0]}")

    return entries, errors


class McpServerService:
    """针对单个用户的 MCP 服务做增删改查。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    # -- 内部 ---------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(McpServer.owner_id == self._owner_id)

    def _get_or_none(self, server_id: int) -> Optional[McpServer]:
        return self._db.scalar(
            self._scope(select(McpServer)).where(McpServer.id == server_id)
        )

    # -- 查询 ---------------------------------------------------------------

    def list(self) -> List[McpServer]:
        stmt = self._scope(select(McpServer)).order_by(McpServer.id.desc())
        return list(self._db.scalars(stmt).all())

    def get(self, server_id: int) -> Optional[McpServer]:
        return self._get_or_none(server_id)

    def require(self, server_id: int) -> McpServer:
        server = self._get_or_none(server_id)
        if server is None:
            # 404 口径：确认别人的配置存在本身就是泄露。
            raise LookupError("MCP 服务不存在")
        return server

    # -- 变更 ---------------------------------------------------------------

    def create(self, payload: McpServerCreate) -> McpServer:
        validate_transport_fields(
            payload.transport, command=payload.command, url=payload.url
        )
        server = McpServer(
            owner_id=self._owner_id,
            name=payload.name,
            transport=payload.transport,
            enabled=payload.enabled,
            remark=payload.remark,
        )
        self._set_connection_fields(
            server,
            transport=payload.transport,
            url=payload.url,
            command=payload.command,
            args=payload.args,
        )
        # 密钥袋只保留适用于本传输的那一个。
        if payload.transport == TRANSPORT_STDIO:
            server.env_enc = _dump_secret(payload.env)
        else:
            server.headers_enc = _dump_secret(payload.headers)

        self._db.add(server)
        self._db.commit()
        self._db.refresh(server)
        logger.info(
            "owner %s created mcp server %s (%s, %s)",
            self._owner_id, server.id, server.name, server.transport,
        )
        return server

    def update(self, server_id: int, payload: McpServerUpdate) -> McpServer:
        server = self.require(server_id)
        data = payload.model_dump(exclude_unset=True)

        # 校验按「改完之后」的组合做：transport 改了，就按新传输看旧字段还够不够。
        transport = data.get("transport", server.transport)
        command = data.get("command", server.command)
        url = data.get("url", server.url)
        validate_transport_fields(transport, command=command, url=url)

        before = _connection_signature(server)

        if "name" in data:
            server.name = data["name"]
        if "enabled" in data:
            server.enabled = data["enabled"]
        if "remark" in data:
            server.remark = data["remark"]

        # 明文连接字段按新传输落位；不属于本传输的清空。
        self._set_connection_fields(
            server,
            transport=transport,
            url=url,
            command=command,
            args=data.get("args", server.args),
        )
        # 换传输后，不再适用的那个密钥袋清掉（stdio 的 env 只对 stdio 有意义）。
        if transport != TRANSPORT_STDIO:
            server.env_enc = None
        if transport not in (TRANSPORT_SSE, TRANSPORT_HTTP):
            server.headers_enc = None

        # 本次提交里带着的密钥袋按掩码规则合并；没带就保持现状。
        if data.get("env") is not None and transport == TRANSPORT_STDIO:
            server.env_enc = merge_secret(server.env_enc, data["env"])
        if data.get("headers") is not None and transport in (TRANSPORT_SSE, TRANSPORT_HTTP):
            server.headers_enc = merge_secret(server.headers_enc, data["headers"])

        after = _connection_signature(server)
        if before != after:
            # 旧测试结果说明的是旧连接。
            server.last_test_ok = None
            server.last_test_msg = None

        self._db.commit()
        self._db.refresh(server)
        logger.info(
            "owner %s updated mcp server %s (connection_changed=%s)",
            self._owner_id, server.id, before != after,
        )
        return server

    def delete(self, server_id: int) -> None:
        server = self.require(server_id)
        name = server.name
        self._db.delete(server)
        self._db.commit()
        logger.info("owner %s deleted mcp server %s (%s)", self._owner_id, name, server_id)

    # -- 导入导出 -----------------------------------------------------------

    def export_rows(self, ids: Optional[List[int]]) -> List[McpServer]:
        """导出候选行：ids 为空导出全部；否则按入参顺序返回。

        停用的服务允许导出（与运行时的 :func:`resolve_servers` 口径不同）；
        任意 id 不存在或不属于本人 → LookupError。
        """
        if not ids:
            return self.list()
        rows = list(
            self._db.scalars(
                self._scope(select(McpServer)).where(McpServer.id.in_(ids))
            ).all()
        )
        found = {row.id for row in rows}
        if len(found) != len(set(ids)) or any(id_ not in found for id_ in ids):
            raise LookupError("MCP 服务不存在或无权访问")
        order = {id_: index for index, id_ in enumerate(ids)}
        rows.sort(key=lambda row: order[row.id])
        return rows

    def import_config(self, doc: dict, *, conflict: str) -> McpImportReport:
        """批量导入 mcpServers 文档，按 conflict 策略处理同名服务。

        rename（默认）：现有服务保留，新条目自动改名后创建；
        skip：同名跳过；overwrite：原地全字段覆盖（保留 id，挂载引用不断）。
        """
        entries, parse_errors = parse_mcp_config(doc)
        report = McpImportReport(errors=list(parse_errors))

        existing = {
            server.name: server
            for server in self._db.scalars(self._scope(select(McpServer))).all()
        }
        # used 同时覆盖「库里已有」和「本次已建」，保证批量内重名也会被处理。
        used = set(existing)

        for payload in entries:
            name = payload.name
            current = existing.get(name)
            try:
                if current is None:
                    created = self.create(payload)
                    existing[name] = created
                    used.add(name)
                    report.created += 1
                elif conflict == "skip":
                    report.skipped += 1
                elif conflict == "overwrite":
                    # 覆盖 = 整份替换：先清空两个密钥袋，否则 merge 语义会
                    # 留下导入文档里不存在的旧键。
                    current.env_enc = None
                    current.headers_enc = None
                    updated = self.update(
                        current.id,
                        McpServerUpdate(
                            name=payload.name,
                            transport=payload.transport,
                            url=payload.url,
                            command=payload.command,
                            args=payload.args,
                            env=payload.env,
                            headers=payload.headers,
                            enabled=payload.enabled,
                        ),
                    )
                    existing[name] = updated
                    report.overwritten += 1
                else:  # rename
                    new_name = _unique_name(name, used)
                    payload.name = new_name
                    created = self.create(payload)
                    existing[new_name] = created
                    used.add(new_name)
                    report.created += 1
                    report.renamed.append(f"{name} -> {new_name}")
            except ValueError as exc:
                report.errors.append(f"{name}: {exc}")

        logger.info(
            "owner %s imported mcp config: created=%s skipped=%s overwritten=%s errors=%s",
            self._owner_id, report.created, report.skipped,
            report.overwritten, len(report.errors),
        )
        return report

    @staticmethod
    def _set_connection_fields(
        server: McpServer,
        *,
        transport: str,
        url: Optional[str],
        command: Optional[str],
        args: Optional[List[str]],
    ) -> None:
        """按传输写明文连接字段：不属于本传输的字段清空。"""
        if transport not in _TRANSPORTS:
            raise ValueError(f"不支持的传输类型：{transport}")
        if transport == TRANSPORT_STDIO:
            server.url = None
            server.command = (command or "").strip()
            server.args = list(args or [])
        else:
            server.url = (url or "").strip()
            server.command = None
            server.args = None

    # -- 测试状态 -----------------------------------------------------------

    def record_test(self, server: McpServer, *, ok: bool, message: str) -> None:
        server.last_test_at = db_now()
        server.last_test_ok = ok
        server.last_test_msg = message[:1024]
        self._db.commit()
        logger.info("mcp server %s connectivity test: ok=%s", server.id, ok)

    # -- 序列化 -------------------------------------------------------------

    def to_item(self, server: McpServer) -> McpServerItem:
        secret_error = False
        env_item: Dict[str, str] = {}
        headers_item: Dict[str, str] = {}
        try:
            env_item = {key: mask(value) for key, value in read_secret(server.env_enc).items()}
        except DecryptionError:
            logger.warning("failed to decrypt env of mcp server %s", server.id)
            secret_error = True
        try:
            headers_item = {
                key: mask(value) for key, value in read_secret(server.headers_enc).items()
            }
        except DecryptionError:
            logger.warning("failed to decrypt headers of mcp server %s", server.id)
            secret_error = True

        return McpServerItem(
            id=server.id,
            owner_id=server.owner_id,
            name=server.name,
            transport=server.transport,
            url=server.url,
            command=server.command,
            args=list(server.args or []),
            env=env_item,
            headers=headers_item,
            enabled=server.enabled,
            remark=server.remark,
            last_test_at=server.last_test_at,
            last_test_ok=server.last_test_ok,
            last_test_msg=server.last_test_msg,
            created_at=server.created_at,
            updated_at=server.updated_at,
            secret_error=secret_error,
        )


def _dump_secret(values: Dict[str, str]) -> Optional[bytes]:
    """非空密钥袋 → Fernet 密文；空 dict → NULL。"""
    if not values:
        return None
    return encrypt(json.dumps(dict(values), ensure_ascii=False))


def _unique_name(base: str, used: set) -> str:
    """在 used 之外找一个 ``base-2``、``base-3``… 的空位。"""
    index = 2
    candidate = f"{base}-{index}"
    while candidate in used:
        index += 1
        candidate = f"{base}-{index}"
    return candidate


def _safe_read_secret(
    blob: Optional[bytes], name: str, what: str, warnings: List[str]
) -> Dict[str, str]:
    """读密钥袋；解密失败时不抛出，登记 warning 并按空袋处理。"""
    try:
        return read_secret(blob)
    except DecryptionError:
        warnings.append(f"{name}: 密钥解密失败，{what}未导出")
        return {}


def export_document(
    servers: List[McpServer],
) -> tuple[Dict[str, Dict], List[str]]:
    """把服务行导出为 mcpServers 标准文档（密钥为明文）。

    - stdio → ``command`` + 可选 ``args`` / ``env``；
    - sse → ``type: "sse"`` + ``url`` + 可选 ``headers``；
    - streamable_http → ``type: "streamable-http"`` + ``url`` + 可选 ``headers``。
    """
    warnings: List[str] = []
    doc: Dict[str, Dict] = {}

    for server in servers:
        if server.transport == TRANSPORT_STDIO:
            item: Dict[str, object] = {"command": server.command or ""}
            if server.args:
                item["args"] = list(server.args)
            env = _safe_read_secret(server.env_enc, server.name, "环境变量", warnings)
            if env:
                item["env"] = env
        else:
            type_name = (
                "sse" if server.transport == TRANSPORT_SSE else "streamable-http"
            )
            item = {"type": type_name, "url": server.url or ""}
            headers = _safe_read_secret(
                server.headers_enc, server.name, "请求头", warnings
            )
            if headers:
                item["headers"] = headers
        doc[server.name] = item

    return {"mcpServers": doc}, warnings


def _connection_signature(server: McpServer) -> tuple:
    """连接相关字段的快照：据此判断本次更新要不要作废旧测试结果。"""
    return (
        server.transport,
        server.url,
        server.command,
        tuple(server.args or []),
        server.env_enc,
        server.headers_enc,
    )
