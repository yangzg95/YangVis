"""SPA 兜底路由：没命中的 ``/api/*`` 必须回统一信封，而不是 index.html。

起因是线上事故：反代把 ``/api/ops/servers`` 301 成带斜杠的形态，后端兜底回了
HTTP 200 的 HTML，前端按响应体里的 ``code`` 判成功与否，认不出这段 HTML，就把
整页 HTML 当成列表数据返回，取到的字段全是 undefined，运维台账页直接崩在渲染上。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_unmatched_api_path_returns_json_envelope_not_html():
    res = TestClient(app).get("/api/definitely/not/a/route")

    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")
    assert res.json()["code"] == 404


def test_registered_api_path_still_reaches_its_route():
    # 兜底不能把真接口吃掉：未登录该拿到鉴权的 401 信封，而不是 404。
    res = TestClient(app).get("/api/ops/servers")

    assert res.status_code == 401
    assert res.json()["code"] == 401


def test_spa_page_paths_still_fall_back_to_html():
    res = TestClient(app).get("/ops/servers")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
