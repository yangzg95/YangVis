"""YApi 同步编排与客户端协议。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.models.schemas import (
    YapiScanCategory,
    YapiScanEndpoint,
    YapiScanResult,
    YapiSyncRequest,
)
from app.services.yapi import client as client_module
from app.services.yapi import sync as sync_module
from app.services.yapi.client import YapiClient, YapiError, normalize_base_url

FIXTURE = Path(__file__).parent / "fixtures" / "yapi_demo"


class FakeYapi:
    last: "FakeYapi"
    fail_paths: set[str] = set()

    def __init__(self, base_url: str, project_id: int, token: str, **_kwargs) -> None:
        self.base_url = base_url
        self.project_id = project_id
        self.token = token
        self.menu = [
            {
                "_id": 7,
                "name": "订单",
                "list": [
                    {"_id": 1, "method": "GET", "path": "/api/orders/{id}"}
                ],
            }
        ]
        self.categories: list[tuple[str, str]] = []
        self.saved: list[dict] = []
        self._next_id = 100
        FakeYapi.last = self

    def __enter__(self) -> "FakeYapi":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        pass

    def list_menu(self):
        return self.menu

    def add_category(self, name: str, description: str) -> int:
        self.categories.append((name, description))
        category_id = self._next_id
        self._next_id += 1
        self.menu.append({"_id": category_id, "name": name, "list": []})
        return category_id

    def save_interface(self, payload: dict) -> None:
        if payload["path"] in FakeYapi.fail_paths:
            raise YapiError("模拟保存失败")
        self.saved.append(payload)


@pytest.fixture()
def fake_yapi(monkeypatch):
    FakeYapi.fail_paths = set()
    monkeypatch.setattr(sync_module, "YapiClient", FakeYapi)
    return FakeYapi


def _sync(root_path: str = str(FIXTURE), endpoint_keys=None):
    return sync_module.sync_to_yapi(
        YapiSyncRequest(
            root_path=root_path,
            base_url="http://yapi.local/",
            project_id=9,
            token="secret-token",
            endpoint_keys=endpoint_keys,
        )
    )


def test_sync_maps_controllers_to_categories_and_creates_interfaces(fake_yapi):
    result = _sync()

    assert result.base_url == "http://yapi.local"
    assert result.project_id == 9
    assert result.categories_created == 1
    assert result.endpoint_count == 9
    assert result.created == 8
    assert result.updated == 1
    assert result.failed == 0
    assert fake_yapi.last.token == "secret-token"

    assert fake_yapi.last.categories == [
        (
            "AdminOrder",
            "Java 包：com.example.shop.admin · Controller：AdminOrderController",
        )
    ]
    actions = {
        (item.http_method, item.path): item.action for item in result.endpoints
    }
    assert actions[("GET", "/api/orders/{id}")] == "updated"
    assert actions[("POST", "/api/orders")] == "created"


def test_sync_only_touches_selected_interfaces(fake_yapi):
    result = _sync(
        endpoint_keys=["POST /api/orders", "POST /admin/orders/summary"]
    )

    assert result.endpoint_count == 2
    assert result.created == 2
    assert result.updated == 0
    assert {item["path"] for item in fake_yapi.last.saved} == {
        "/api/orders",
        "/admin/orders/summary",
    }
    # 只为选中接口涉及的分类建分类，未选中的分类不碰。
    assert fake_yapi.last.categories == [
        (
            "AdminOrder",
            "Java 包：com.example.shop.admin · Controller：AdminOrderController",
        )
    ]


def test_sync_with_empty_selection_opens_no_client(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("没有选中接口时不应连接 YApi")

    monkeypatch.setattr(sync_module, "YapiClient", explode)
    result = _sync(endpoint_keys=[])

    assert result.endpoint_count == 0
    assert result.created == 0
    assert result.endpoints == []


def test_interface_payload_carries_params_bodies_and_source(fake_yapi):
    _sync()
    payloads = {
        (item["method"], item["path"]): item for item in fake_yapi.last.saved
    }

    create = payloads[("POST", "/api/orders")]
    assert create["catid"] == 7
    assert create["status"] == "done"
    assert create["req_body_type"] == "json"
    assert create["req_body_is_json_schema"] is True
    assert json.loads(create["req_body_other"])["required"] == ["buyerName"]
    create_body = json.loads(create["res_body"])
    assert set(create_body["properties"]) == {"code", "message", "data"}
    assert create_body["properties"]["data"]["type"] == "integer"
    assert "Controller：OrderController.create()" in create["desc"]
    assert "源码：src/main/java/com/example/shop/order/OrderController.java:" in create["desc"]

    upload = payloads[("POST", "/api/orders/{id}/upload")]
    form = {item["name"]: item for item in upload["req_body_form"]}
    assert upload["req_body_type"] == "form"
    assert form["file"]["type"] == "file"
    assert form["remark"]["type"] == "text"
    assert {item["name"] for item in upload["req_params"]} == {"id"}

    update = payloads[("PUT", "/api/orders/{id}")]
    assert update["res_body_is_json_schema"] is True
    assert set(json.loads(update["res_body"])["properties"]) == {"code", "message"}

    batch = payloads[("POST", "/admin/orders/batch")]
    assert batch["catid"] == 100
    assert {item["name"] for item in batch["req_body_form"]} == {
        "keyword",
        "status",
        "pageNo",
        "pageSize",
    }

    page = payloads[("GET", "/api/orders/page")]
    query = {item["name"]: item for item in page["req_query"]}
    assert query["keyword"]["required"] == "0"
    headers = {item["name"]: item for item in page["req_headers"]}
    assert headers["X-Tenant"]["required"] == "1"
    assert page["res_body_is_json_schema"] is True
    records = json.loads(page["res_body"])["properties"]["data"]["properties"]["records"]
    assert records["items"]["properties"]["orderNo"]["type"] == "string"


def test_sync_continues_when_one_interface_fails(fake_yapi):
    fake_yapi.fail_paths = {"/api/orders/page"}

    result = _sync()

    assert result.failed == 1
    assert result.created + result.updated == 8
    failed = [item for item in result.endpoints if item.action == "failed"]
    assert len(failed) == 1
    assert failed[0].path == "/api/orders/page"
    assert "模拟保存失败" in failed[0].error
    assert any("同步失败" in warning for warning in result.warnings)


def test_invalid_path_is_reported_before_calling_save(fake_yapi, monkeypatch):
    endpoint = YapiScanEndpoint(
        package="demo",
        category_name="demo",
        controller="DemoController",
        method="run",
        title="查询",
        http_method="GET",
        path="/接口/查询",
        source="DemoController.java",
        line=1,
    )
    scanned = YapiScanResult(
        root_path="/demo",
        java_files=1,
        controller_count=1,
        endpoint_count=1,
        categories=[
            YapiScanCategory(
                package="demo",
                category_name="demo",
                controllers=["DemoController"],
                controller_count=1,
                endpoint_count=1,
            )
        ],
        endpoints=[endpoint],
    )
    monkeypatch.setattr(sync_module, "scan_java_path", lambda _path: scanned)

    result = _sync(root_path="/demo")

    assert result.failed == 1
    assert fake_yapi.last.saved == []
    assert "YApi 不接受的字符" in result.endpoints[0].error


# ---- 客户端 -----------------------------------------------------------------


def _client(handler, base_url: str = "http://yapi.local") -> YapiClient:
    return YapiClient(
        base_url,
        9,
        "secret-token",
        transport=httpx.MockTransport(handler),
    )


def test_list_menu_sends_credentials_and_parses_categories():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/interface/list_menu"
        assert request.url.params["project_id"] == "9"
        assert request.url.params["token"] == "secret-token"
        return httpx.Response(
            200,
            json={"errcode": 0, "data": [{"_id": 1, "name": "order", "list": []}]},
        )

    with _client(handler) as client:
        menu = client.list_menu()

    assert menu == [{"_id": 1, "name": "order", "list": []}]


def test_add_category_posts_form_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/interface/add_cat"
        form = parse_qs(request.content.decode())
        assert form["project_id"] == ["9"]
        assert form["token"] == ["secret-token"]
        assert form["name"] == ["order"]
        assert form["desc"] == ["Java 包：com.example.shop.order"]
        return httpx.Response(200, json={"errcode": 0, "data": {"_id": 12}})

    with _client(handler) as client:
        assert client.add_category("order", "Java 包：com.example.shop.order") == 12


def test_save_interface_injects_token_and_project_id():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/interface/save"
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    with _client(handler) as client:
        client.save_interface({"title": "查询", "path": "/x", "method": "GET"})

    assert captured["token"] == "secret-token"
    assert captured["project_id"] == 9
    assert captured["path"] == "/x"


def test_business_error_is_raised_even_with_http_200():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errcode": 40011, "errmsg": "token 无效"}
        )

    with _client(handler) as client:
        with pytest.raises(YapiError, match="40011"):
            client.list_menu()


def test_http_error_is_raised():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with _client(handler) as client:
        with pytest.raises(YapiError, match="HTTP 502"):
            client.list_menu()


def test_base_url_is_validated_and_normalized():
    with pytest.raises(YapiError, match="http"):
        YapiClient("yapi.local", 9, "token")
    assert (
        normalize_base_url("http://yapi.local/yapi/?from=test")
        == "http://yapi.local/yapi"
    )


def test_token_is_redacted_in_error_text():
    assert (
        client_module._redact("GET http://x/api?project_id=1&token=abc&foo=1")
        == "GET http://x/api?project_id=1&token=***&foo=1"
    )
