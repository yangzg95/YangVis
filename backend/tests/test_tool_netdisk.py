"""本地工具 save_text_to_netdisk：把对话产出的文本存进本人绑定的网盘。

网盘那一层（xpan 三步上传、token）已由 test_netdisk.py 覆盖，这里钉模型面：
注册形态、参数校验、未配置/未绑定的提示文案、成功时走 chat/ 子目录且只吃
文件名段，以及网盘业务错误要落成工具结果而不是炸掉整轮。
SQLite 文件库；monkeypatch 工具模块自己 import 进来的 SessionLocal。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.crypto import encrypt
from app.database import Base
from app.errors import CODE_NETDISK_API_ERROR, BusinessError
from app.models.entities import NetdiskAccount
from app.services.netdisk import _now
from app.services.tools import ToolContext, specs
from app.services.tools.builtins import netdisk_tools

wait = asyncio.run

OWNER = 1
APP_NAME = "yangvis"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool_netdisk.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    # 异步工具按 registry 约定自开 session：patch 它模块内的绑定。
    monkeypatch.setattr(netdisk_tools, "SessionLocal", maker)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def configured(monkeypatch):
    """网盘功能已启用但用户尚未绑定的配置。"""
    conf = get_settings()
    for name, value in {
        "BAIDU_NETDISK_APP_KEY": "app-key",
        "BAIDU_NETDISK_SECRET_KEY": "app-secret",
        "BAIDU_NETDISK_APP_NAME": APP_NAME,
    }.items():
        monkeypatch.setattr(conf, name, value)
    return conf


def bind_account(db, owner_id: int = OWNER) -> None:
    account = NetdiskAccount(owner_id=owner_id)
    account.access_token_enc = encrypt("access-token")
    account.refresh_token_enc = encrypt("refresh-token")
    account.expires_at = _now() + timedelta(days=1)
    db.add(account)
    db.commit()


def test_registry_shape():
    spec = {s.key: s for s in specs()}["save_text_to_netdisk"]
    assert spec.title == "保存文件到百度网盘"
    assert spec.is_async is True
    assert spec.takes_ctx is True
    params = {p.name: p for p in spec.params}
    assert set(params) == {"filename", "content"}
    assert all(p.json_type == "string" and p.required for p in params.values())
    assert spec.source.startswith("services/tools/builtins/netdisk_tools.py")


@pytest.mark.parametrize(
    "filename",
    ["", "   ", "chat/", "a/b/", "/", "\\"],
)
def test_rejects_empty_or_path_only_filename(filename):
    with pytest.raises(ValueError, match="filename"):
        wait(netdisk_tools.save_text_to_netdisk(ToolContext(OWNER, 0), filename, "x"))


def test_rejects_empty_and_oversized_content(db, configured):
    with pytest.raises(ValueError, match="content 不能为空"):
        wait(netdisk_tools.save_text_to_netdisk(ToolContext(OWNER, 0), "a.md", ""))
    with pytest.raises(ValueError, match="1MB"):
        wait(
            netdisk_tools.save_text_to_netdisk(
                ToolContext(OWNER, 0), "a.md", "x" * (1024 * 1024 + 1)
            )
        )


def test_not_configured_returns_hint(db, monkeypatch):
    conf = get_settings()
    for name in (
        "BAIDU_NETDISK_APP_KEY",
        "BAIDU_NETDISK_SECRET_KEY",
        "BAIDU_NETDISK_APP_NAME",
    ):
        monkeypatch.setattr(conf, name, "")
    out = wait(netdisk_tools.save_text_to_netdisk(ToolContext(OWNER, 0), "a.md", "正文"))
    assert "尚未启用" in out


def test_not_bound_returns_bind_hint(db, configured):
    out = wait(netdisk_tools.save_text_to_netdisk(ToolContext(OWNER, 0), "a.md", "正文"))
    assert "绑定" in out
    # 没绑定就不该有任何账号行。
    assert db.scalar(select(NetdiskAccount)) is None


def test_success_uploads_under_chat_prefix_with_bare_filename(db, configured, monkeypatch):
    bind_account(db)
    captured: dict = {}

    async def fake_upload(self, rel_path: str, raw: bytes):
        captured["rel_path"] = rel_path
        captured["raw"] = raw
        captured["owner"] = self._owner_id
        return 9527, f"/apps/{APP_NAME}/{rel_path}"

    monkeypatch.setattr(netdisk_tools.NetdiskService, "upload_file", fake_upload)

    out = wait(
        netdisk_tools.save_text_to_netdisk(
            ToolContext(OWNER, 0), "reports/月报.md", "# 月报\n你好"
        )
    )
    assert captured["rel_path"] == "chat/月报.md"
    assert captured["raw"] == "# 月报\n你好".encode("utf-8")
    assert captured["owner"] == OWNER
    assert f"/apps/{APP_NAME}/chat/月报.md" in out
    assert "9527" in out


def test_netdisk_business_error_becomes_tool_message(db, configured, monkeypatch):
    bind_account(db)

    async def boom(self, rel_path: str, raw: bytes):
        raise BusinessError(CODE_NETDISK_API_ERROR, "网盘授权已失效，请重新绑定")

    monkeypatch.setattr(netdisk_tools.NetdiskService, "upload_file", boom)

    out = wait(netdisk_tools.save_text_to_netdisk(ToolContext(OWNER, 0), "a.md", "正文"))
    assert out.startswith("上传到百度网盘失败：")
    assert "重新绑定" in out
