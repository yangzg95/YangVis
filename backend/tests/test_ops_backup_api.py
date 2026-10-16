"""``/ops/databases/*/backup*`` 这一层的契约：类型闸门、网盘前置判定、审计落库。

整库读一遍很贵，所以这一层只管「在花钱之前能不能拒掉」与「拒的时候给什么码」：
Redis 连接走 HTTPException(400)，网盘没配/没绑走恒 200 的业务码信封（前端靠
code 决定是提示去绑定还是提示下载），这些都必须发生在真正打开备份会话之前。
备份文件本身的写法在 ``tests/test_ops_backup.py`` 里钉。

SQLite 跑文件库，备份会话与网盘上传全部打桩。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import OpsAuditLog, OpsDatabase
from app.models.schemas import CurrentUser
from app.services.netdisk import NetdiskService
from app.services.ops import db_backup

OWNER = 1
# 一条「已经备好料」的备份会话：路由层的验收点在闸门与审计，不在文件本身。
SESSION = SimpleNamespace(
    schema="shop",
    entries=[1, 2],
    total_rows=42,
    structure_only=False,
    created_at=datetime(2026, 10, 16, 15, 4, 5),
)


class FakePan:
    """只装网盘这一侧要用的三个方法：绑定状态、上传，其余一概不碰。"""

    def __init__(self, *, configured=True, bound=True) -> None:
        self.configured = configured
        self.bound = bound
        self.uploads: List[tuple] = []

    def status_view(self):
        return SimpleNamespace(configured=self.configured, bound=self.bound)

    async def upload_file(self, rel_path, raw):
        self.uploads.append((rel_path, raw))
        return 9001, f"/apps/test/{rel_path}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'backup.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    mysql = OpsDatabase(
        owner_id=OWNER, name="订单库", db_type="mysql", host="10.0.0.1", port=3306, username="ro"
    )
    redis = OpsDatabase(owner_id=OWNER, name="缓存", db_type="redis", host="10.0.0.2", port=6379)
    db.add_all([mysql, redis])
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="t", is_admin=True
    )

    pan = FakePan()
    monkeypatch.setattr(NetdiskService, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(NetdiskService, "status_view", lambda self: pan.status_view())
    monkeypatch.setattr(NetdiskService, "upload_file", lambda self, rel, raw: pan.upload_file(rel, raw))
    yield SimpleNamespace(db=db, pan=pan, mysql=mysql.id, redis=redis.id, client=TestClient(app))
    app.dependency_overrides.clear()


async def _fake_open(*args, **kwargs):
    return SESSION


def recorder(sink: List[tuple]):
    """记下 open_backup 是否被调到、以及选表名单原样传到了没有——闸门必须排在它之前。"""

    async def fake(item, schema, structure_only=False, tables=None):
        sink.append((item.id, schema, tables))
        return SESSION

    return fake


# ---- 类型闸门与网盘前置判定 ------------------------------------------------------


def test_redis_connection_is_rejected_before_any_backup_work(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    res = env.client.get(f"/api/ops/databases/{env.redis}/schemas/0/backup")
    assert res.status_code == 400
    # 4xx 同样套信封（SPA 的拦截器只看 code），这里没有 detail 可读。
    assert "MySQL" in res.json()["message"]
    # 400 必须排在开连接之前：Redis 台账上连一次 MySQL 握手都不该发起。
    assert opened == []


def test_netdisk_gate_rejects_while_the_dump_is_still_cheap(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    env.pan.bound = False
    res = env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk", json={}
    )
    body = res.json()
    assert res.status_code == 200 and body["code"] == 4202
    assert opened == []


def test_unconfigured_netdisk_points_at_the_download_instead(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    env.pan.configured = False
    res = env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk",
        json={"structure_only": True},
    )
    body = res.json()
    assert body["code"] == 4201
    assert "下载到本地" in body["message"]
    assert opened == []


# ---- 网盘分支 -------------------------------------------------------------------


def test_netdisk_backup_uploads_the_dump_and_audits_it(env, monkeypatch):
    async def fake_collect(session):
        return b"CREATE TABLE x;\n"

    monkeypatch.setattr(db_backup, "open_backup", _fake_open)
    monkeypatch.setattr(db_backup, "collect_backup", fake_collect)
    monkeypatch.setattr(db_backup, "backup_filename", lambda session: "shop.20261016_150405.sql")

    res = env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk",
        json={"structure_only": False},
    )
    body = res.json()
    assert body["code"] == 0, body
    data = body["data"]
    assert data["path"].endswith("backups/1_shop.20261016_150405.sql")
    assert data["tables"] == 2 and data["rows"] == 42 and data["bytes"] == 16

    # 落进 backups/ 之下、以连接 id 打头：不同连接的同名库不能互相盖掉。
    assert env.pan.uploads == [("backups/1_shop.20261016_150405.sql", b"CREATE TABLE x;\n")]
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.command.startswith("[备份 shop 到网盘] 结构+数据")
    assert log.success is True and "backups/1_shop" in log.command


def test_failed_netdisk_upload_is_audited_as_a_failure(env, monkeypatch):
    from app.errors import CODE_NETDISK_API_ERROR, BusinessError

    async def boom(self, rel, raw):  # noqa: ANN001 - 顶掉 NetdiskService.upload_file
        raise BusinessError(CODE_NETDISK_API_ERROR, "网盘 errno=31034 路径非法")

    monkeypatch.setattr(db_backup, "open_backup", _fake_open)
    monkeypatch.setattr(db_backup, "collect_backup", _fake_collect)
    monkeypatch.setattr(db_backup, "backup_filename", lambda session: "shop.sql")
    monkeypatch.setattr(NetdiskService, "upload_file", boom)

    res = env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk", json={}
    )
    assert res.json()["code"] == CODE_NETDISK_API_ERROR
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.success is False and "31034" in (log.error or "")


async def _fake_collect(session):
    return b"x"


def test_dump_too_big_for_netdisk_fails_before_uploading(env, monkeypatch):
    from app.services.ops.database import OpsDbError

    async def too_big(session):
        raise OpsDbError("备份已超过 64MB，网盘存不下这么大的文件，请改为仅备份结构")

    monkeypatch.setattr(db_backup, "open_backup", _fake_open)
    monkeypatch.setattr(db_backup, "collect_backup", too_big)
    res = env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk", json={}
    )
    assert res.json()["code"] == 4103
    assert env.pan.uploads == []


# ---- 下载分支 -------------------------------------------------------------------


def test_download_streams_the_file_with_a_utf8_name(env, monkeypatch):
    async def stream(session):
        yield "-- YangVis 数据库备份\n".encode("utf-8")
        yield "CREATE TABLE x;\n".encode("utf-8")

    monkeypatch.setattr(db_backup, "open_backup", _fake_open)
    monkeypatch.setattr(db_backup, "stream_backup", stream)
    monkeypatch.setattr(db_backup, "backup_filename", lambda session: "订单.shop.sql")

    res = env.client.get(f"/api/ops/databases/{env.mysql}/schemas/shop/backup")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/sql")
    # 中文库名走 RFC 5987：ASCII 那份只是兜底，正主在 filename* 里。
    assert "filename*=UTF-8''" in res.headers["content-disposition"]
    assert res.text == "-- YangVis 数据库备份\nCREATE TABLE x;\n"


def test_download_failure_still_comes_back_as_an_envelope(env, monkeypatch):
    from app.services.ops.database import OpsDbError

    async def no_tables(item, schema, structure_only=False, tables=None):
        raise OpsDbError(f"库 {schema} 里没有表")

    monkeypatch.setattr(db_backup, "open_backup", no_tables)
    res = env.client.get(f"/api/ops/databases/{env.mysql}/schemas/empty/backup")
    # 响应头还没发出去，所以这次仍然可以是正常的业务错误信封而不是半截文件。
    body = res.json()
    assert res.status_code == 200 and body["code"] == 4103
    assert "没有表" in body["message"]


# ---- 选表名单的透传 ---------------------------------------------------------------


def test_netdisk_backup_passes_the_selected_tables(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    monkeypatch.setattr(db_backup, "collect_backup", _fake_collect)
    monkeypatch.setattr(db_backup, "backup_filename", lambda session: "shop.sql")
    env.client.post(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk",
        json={"structure_only": False, "tables": ["orders", "items"]},
    )
    # 名单原样交给服务层：这里不裁不猜，去空白去重是它的事。
    assert opened == [(env.mysql, "shop", ["orders", "items"])]


def test_netdisk_backup_without_tables_is_the_whole_schema(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    monkeypatch.setattr(db_backup, "collect_backup", _fake_collect)
    monkeypatch.setattr(db_backup, "backup_filename", lambda session: "shop.sql")
    env.client.post(f"/api/ops/databases/{env.mysql}/schemas/shop/backup/netdisk", json={})
    assert opened == [(env.mysql, "shop", None)]


async def _fake_stream(session):
    yield "-- YangVis 数据库备份\n".encode("utf-8")


def test_download_passes_tables_as_a_json_array(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    monkeypatch.setattr(db_backup, "stream_backup", _fake_stream)
    res = env.client.get(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup",
        params={"tables": json.dumps(["orders"])},
    )
    assert res.status_code == 200
    assert opened == [(env.mysql, "shop", ["orders"])]


def test_download_without_tables_is_the_whole_schema(env, monkeypatch):
    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    monkeypatch.setattr(db_backup, "stream_backup", _fake_stream)
    res = env.client.get(f"/api/ops/databases/{env.mysql}/schemas/shop/backup")
    assert res.status_code == 200
    assert opened == [(env.mysql, "shop", [])]


def test_download_rejects_a_malformed_tables_param(env, monkeypatch):
    from app.errors import CODE_OPS_EXEC_FAILED

    opened: List[tuple] = []
    monkeypatch.setattr(db_backup, "open_backup", recorder(opened))
    res = env.client.get(
        f"/api/ops/databases/{env.mysql}/schemas/shop/backup", params={"tables": "orders,items"}
    )
    body = res.json()
    assert body["code"] == CODE_OPS_EXEC_FAILED
    assert "JSON 数组" in body["message"]
    # 参数不合法就不该去开备份会话——一次整库扫描不是校验失败该付的代价。
    assert opened == []
