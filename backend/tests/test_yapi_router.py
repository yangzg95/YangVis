"""/devtools/yapi 路由：鉴权注入、统一信封与参数透传。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import require_user
from app.models.schemas import CurrentUser, YapiSyncResult
from app.routers import devtools
from app.services.directory_dialog import DirectoryDialogError
from app.services.yapi.client import YapiError

FIXTURE = Path(__file__).parent / "fixtures" / "yapi_demo"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(devtools.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=1, username="tester"
    )
    return TestClient(app)


def test_scan_returns_endpoint_inventory(client):
    response = client.post(
        "/api/devtools/yapi/scan", json={"root_path": str(FIXTURE)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["endpoint_count"] == 9
    assert body["data"]["categories"][0]["category_name"] == "AdminOrder"
    assert body["data"]["categories"][0]["package"] == "com.example.shop.admin"


def test_scan_reports_bad_path_in_envelope(client):
    response = client.post(
        "/api/devtools/yapi/scan", json={"root_path": str(FIXTURE / "missing")}
    )

    body = response.json()
    assert body["code"] == -1
    assert "路径不存在" in body["message"]


def test_pick_directory_returns_selected_path(client, monkeypatch):
    monkeypatch.setattr(devtools, "pick_directory", lambda: r"D:\code\demo")
    response = client.post("/api/devtools/pick-directory")

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == r"D:\code\demo"


def test_pick_directory_cancel_returns_empty_data(client, monkeypatch):
    monkeypatch.setattr(devtools, "pick_directory", lambda: None)
    response = client.post("/api/devtools/pick-directory")

    body = response.json()
    assert body["code"] == 0
    assert body["data"] is None


def test_pick_directory_reports_unavailable_dialog(client, monkeypatch):
    def fail():
        raise DirectoryDialogError("当前后端环境无法弹出系统目录选择框，请手动填写路径")

    monkeypatch.setattr(devtools, "pick_directory", fail)
    response = client.post("/api/devtools/pick-directory")

    body = response.json()
    assert body["code"] == -1
    assert "无法弹出" in body["message"]


def test_sync_forwards_payload_and_returns_report(client, monkeypatch):
    captured: dict = {}

    def fake_sync(payload):
        captured["payload"] = payload
        return YapiSyncResult(
            base_url="http://yapi.local",
            project_id=payload.project_id,
            created=2,
            updated=1,
            endpoint_count=3,
        )

    monkeypatch.setattr(devtools, "sync_to_yapi", fake_sync)
    response = client.post(
        "/api/devtools/yapi/sync",
        json={
            "root_path": str(FIXTURE),
            "base_url": "http://yapi.local",
            "project_id": 9,
            "token": "secret-token",
            "endpoint_keys": ["POST /api/orders"],
        },
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"]["created"] == 2
    assert body["data"]["updated"] == 1
    assert captured["payload"].project_id == 9
    assert captured["payload"].token == "secret-token"
    assert captured["payload"].endpoint_keys == ["POST /api/orders"]


def test_sync_reports_yapi_error_in_envelope(client, monkeypatch):
    def failing_sync(_payload):
        raise YapiError("YApi 返回错误（40011）：token 无效")

    monkeypatch.setattr(devtools, "sync_to_yapi", failing_sync)
    response = client.post(
        "/api/devtools/yapi/sync",
        json={
            "root_path": str(FIXTURE),
            "base_url": "http://yapi.local",
            "project_id": 9,
            "token": "bad-token",
        },
    )

    body = response.json()
    assert body["code"] == -1
    assert "40011" in body["message"]
