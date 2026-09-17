"""服务器台账与 SSH 执行通道。

两件事在这里合流：一是服务器记录的增删改查（凭据加密存储、按 owner 隔离），
二是真正的 SSH 连接——交互式 PTY 给终端用，一次性执行给 AI 工具用。

主机公钥采用 TOFU：第一次连接时把指纹写进 ``ops_server.host_key``，之后每次
都比对。这不是完美的中间人防护，但比 ``known_hosts=None``（等于关掉校验）
强得多，而后者几乎是这类工具的默认写法。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import asyncssh
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import OpsServer
from app.models.schemas import (
    OpsServerCreate,
    OpsServerItem,
    OpsServerUpdate,
    ServerAuthType,
)

logger = logging.getLogger("yangvis.ops_server")


class OpsConnectError(RuntimeError):
    """无法与目标服务器建立连接。"""


class HostKeyMismatch(OpsConnectError):
    """主机公钥与首次连接时记录的指纹不一致。

    单独一个类型，是因为它的含义和「连不上」完全不同：要么服务器重装了，
    要么中间有人。两种情况都必须由人来判断，绝不能自动接受新指纹。
    """


def _fingerprint(key: asyncssh.SSHKey) -> str:
    return key.get_fingerprint()


class _PinnedHostKeyClient(asyncssh.SSHClient):
    """在握手阶段抓下服务端公钥指纹，并与已固定的值比对。"""

    def __init__(self, expected: Optional[str]) -> None:
        self._expected = expected
        self.observed: Optional[str] = None

    def validate_host_public_key(self, host: str, addr: str, port: int, key: asyncssh.SSHKey) -> bool:
        self.observed = _fingerprint(key)
        if self._expected is None:
            # 首次连接：接受并记下来（TOFU）。
            return True
        return self.observed == self._expected


class OpsServerService:
    """服务器台账。台账全员共用：``owner_id`` 只记录登记人/审计归属，
    不再作为可见性过滤；增删改由路由层限定管理员。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    # -- 内部方法 -----------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        # 台账全局化后不再按 owner 过滤；保留这个方法只是为了让各查询点的
        # 结构不变。
        return stmt

    def _require_unique_name(self, name: str, *, exclude_id: Optional[int] = None) -> None:
        """台账共用后名称全局唯一。DB 层仍只有 (owner_id, name) 复合索引，
        全局唯一靠这层预查强制（存量历史重名不强刷，只挡新增/改名）。"""
        stmt = select(OpsServer.id).where(OpsServer.name == name)
        if exclude_id is not None:
            stmt = stmt.where(OpsServer.id != exclude_id)
        if self._db.scalar(stmt) is not None:
            raise ValueError(f"服务器名称「{name}」已存在")

    # -- 查询 ---------------------------------------------------------------

    def list(self, keyword: Optional[str] = None) -> List[OpsServer]:
        stmt = self._scope(select(OpsServer))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(OpsServer.name.like(like) | OpsServer.host.like(like))
        return list(self._db.scalars(stmt.order_by(OpsServer.id.desc())).all())

    def get(self, server_id: int) -> OpsServer:
        server = self._db.scalar(self._scope(select(OpsServer).where(OpsServer.id == server_id)))
        if server is None:
            raise LookupError("服务器不存在")
        return server

    # -- 变更操作 -----------------------------------------------------------

    def create(self, payload: OpsServerCreate) -> OpsServer:
        self._require_credential(payload.auth_type, payload.password, payload.private_key)
        self._require_unique_name(payload.name.strip())

        server = OpsServer(
            owner_id=self._owner_id,
            name=payload.name.strip(),
            host=payload.host.strip(),
            port=payload.port,
            username=payload.username.strip(),
            auth_type=payload.auth_type.value,
            password_enc=encrypt(payload.password or ""),
            private_key_enc=encrypt(payload.private_key or ""),
            passphrase_enc=encrypt(payload.passphrase or ""),
            remark=payload.remark,
        )
        self._db.add(server)
        self._commit_unique(f"服务器名称「{server.name}」已存在")
        self._db.refresh(server)
        return server

    def update(self, server_id: int, payload: OpsServerUpdate) -> OpsServer:
        server = self.get(server_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ("name", "host", "username", "remark"):
            value = data.get(field)
            if value is not None:
                setattr(server, field, value.strip() if isinstance(value, str) else value)

        if data.get("name") is not None:
            self._require_unique_name(server.name, exclude_id=server.id)

        if data.get("port") is not None:
            server.port = data["port"]

        endpoint_changed = any(
            data.get(f) is not None for f in ("host", "port", "username")
        )

        if data.get("auth_type") is not None:
            server.auth_type = data["auth_type"].value
            endpoint_changed = True

        credential_changed = False
        for field, column in (
            ("password", "password_enc"),
            ("private_key", "private_key_enc"),
            ("passphrase", "passphrase_enc"),
        ):
            value = data.get(field)
            if value and not is_masked(value):
                setattr(server, column, encrypt(value))
                credential_changed = True

        if endpoint_changed or credential_changed:
            server.last_check_ok = False
            server.last_check_error = None
        if endpoint_changed:
            # 换了机器就等于换了身份，旧指纹不能继续代表它。
            server.host_key = None

        self._commit_unique(f"服务器名称「{server.name}」已存在")
        self._db.refresh(server)
        return server

    def delete(self, server_id: int) -> None:
        server = self.get(server_id)
        self._db.delete(server)
        self._db.commit()

    def record_check(self, server: OpsServer, *, ok: bool, message: str = "") -> None:
        server.last_checked_at = datetime.now(timezone.utc)
        server.last_check_ok = ok
        server.last_check_error = None if ok else message[:512]
        self._db.commit()

    def pin_host_key(self, server: OpsServer, fingerprint: str) -> None:
        if server.host_key == fingerprint:
            return
        server.host_key = fingerprint[:512]
        self._db.commit()
        logger.info("pinned host key for server %s: %s", server.id, fingerprint)

    # -- 序列化 -------------------------------------------------------------

    def to_item(self, server: OpsServer) -> OpsServerItem:
        auth_type = ServerAuthType(server.auth_type)
        blob = (
            server.password_enc
            if auth_type is ServerAuthType.PASSWORD
            else server.private_key_enc
        )
        try:
            secret = decrypt(blob)
            credential = mask(secret) if secret else ""
            error = None
        except DecryptionError as exc:
            credential = ""
            error = str(exc)

        return OpsServerItem(
            id=server.id,
            name=server.name,
            host=server.host,
            port=server.port,
            username=server.username,
            auth_type=auth_type,
            credential=credential,
            credential_error=error,
            host_key_pinned=bool(server.host_key),
            remark=server.remark,
            last_checked_at=server.last_checked_at,
            last_check_ok=server.last_check_ok,
            last_check_error=server.last_check_error,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )

    # -- 私有工具 -----------------------------------------------------------

    @staticmethod
    def _require_credential(
        auth_type: ServerAuthType, password: Optional[str], private_key: Optional[str]
    ) -> None:
        if auth_type is ServerAuthType.PASSWORD and not password:
            raise ValueError("口令认证需要填写密码")
        if auth_type is ServerAuthType.KEY and not private_key:
            raise ValueError("密钥认证需要填写私钥")

    def _commit_unique(self, message: str) -> None:
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError(message) from exc


# ---- SSH 连接 ---------------------------------------------------------------


async def connect(
    server: OpsServer, service: Optional[OpsServerService] = None
) -> asyncssh.SSHClientConnection:
    """建立一条 SSH 连接，并完成 TOFU 指纹校验。

    传入 ``service`` 时，首次连接会把指纹写回数据库。AI 工具不传，是刻意的：
    自动固定指纹这件事只应该发生在用户自己发起的连接里。
    """
    settings = get_settings()
    auth_type = ServerAuthType(server.auth_type)

    options: dict[str, Any] = {
        "host": server.host,
        "port": server.port,
        "username": server.username,
        "known_hosts": None,  # 校验交给 _PinnedHostKeyClient，不是不校验
        "connect_timeout": settings.OPS_SSH_TIMEOUT,
    }

    try:
        if auth_type is ServerAuthType.PASSWORD:
            options["password"] = decrypt(server.password_enc)
            options["client_keys"] = None
        else:
            passphrase = decrypt(server.passphrase_enc) or None
            raw_key = decrypt(server.private_key_enc)
            try:
                options["client_keys"] = [asyncssh.import_private_key(raw_key, passphrase)]
            except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError) as exc:
                raise OpsConnectError(f"私钥无法解析：{exc}") from exc
    except DecryptionError as exc:
        raise OpsConnectError(str(exc)) from exc

    client = _PinnedHostKeyClient(server.host_key)

    try:
        conn, _ = await asyncio.wait_for(
            asyncssh.create_connection(lambda: client, **options),
            timeout=settings.OPS_SSH_TIMEOUT + 5,
        )
    except asyncssh.HostKeyNotVerifiable as exc:
        raise HostKeyMismatch(
            f"主机公钥与首次连接时记录的不一致（现为 {client.observed}）。"
            "服务器可能已重装，也可能存在中间人；请确认后在编辑页面重置该服务器。"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise OpsConnectError(f"连接 {server.host}:{server.port} 超时") from exc
    except (OSError, asyncssh.Error) as exc:
        raise OpsConnectError(f"连接失败：{exc}") from exc

    if service is not None and client.observed and not server.host_key:
        service.pin_host_key(server, client.observed)

    return conn


async def run_once(
    conn: asyncssh.SSHClientConnection,
    command: str,
    *,
    timeout: Optional[float] = None,
    limit: Optional[int] = None,
) -> Tuple[int, str]:
    """执行一条命令，返回 ``(exit_status, 合并后的输出)``。

    输出被截断到 ``OPS_OUTPUT_LIMIT``：这条通道的另一端是模型的 context，
    一个 ``cat`` 大日志就能把它撑爆。
    """
    settings = get_settings()
    timeout = timeout or settings.OPS_CMD_TIMEOUT
    limit = limit or settings.OPS_OUTPUT_LIMIT

    try:
        result = await asyncio.wait_for(
            conn.run(command, check=False), timeout=timeout
        )
    except asyncio.TimeoutError:
        return 124, f"命令执行超过 {timeout:.0f} 秒，已中断"
    except asyncssh.Error as exc:
        return 1, f"执行失败：{exc}"

    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    output = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")

    if len(output) > limit:
        output = output[:limit] + f"\n…（输出超过 {limit} 字符，已截断）"

    return result.exit_status or 0, output


async def test_connection(server: OpsServer, service: OpsServerService) -> str:
    """连一次并跑一条无害命令，用于列表页的「测试」按钮。"""
    conn = await connect(server, service)
    try:
        _, output = await run_once(conn, "uname -a", timeout=10, limit=512)
        return output.strip() or "连接成功"
    finally:
        conn.close()
