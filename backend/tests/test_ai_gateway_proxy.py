"""AI 网关对外端点的端到端测试。

上游用 httpx.MockTransport 伪造，数据库用 SQLite 内存库，路由解析、故障转移、
SSE 透传与调用留痕走的都是真实代码路径。
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.entities import AiApiKey, AiCallLog
from app.models.schemas import AiApiKeyUpdate, AiChannelCreate, AiModelRouteCreate
from app.routers import ai_gateway_openai
from app.services import ai_gateway_proxy as proxy
from app.services.ai_gateway import AiGatewayService

PRIMARY = "https://primary.test/v1"
BACKUP = "https://backup.test/v1"


def sse(*chunks: str) -> bytes:
    return "".join(f"data: {chunk}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)

    app = FastAPI()
    app.include_router(ai_gateway_openai.router)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # 流式响应的日志在请求作用域之外落库，代理自己开 session，这里一起换掉。
    monkeypatch.setattr(proxy, "SessionLocal", Session)

    with Session() as db:
        service = AiGatewayService(db)
        primary = service.create_channel(
            AiChannelCreate(name="primary", base_url=PRIMARY, api_key="upstream-primary")
        )
        backup = service.create_channel(
            AiChannelCreate(name="backup", base_url=BACKUP, api_key="upstream-backup")
        )
        service.create_route(
            AiModelRouteCreate(model_name="gpt-4o", channel_id=primary.id, priority=100)
        )
        service.create_route(
            AiModelRouteCreate(model_name="gpt-4o", channel_id=backup.id, priority=200)
        )
        service.create_route(
            AiModelRouteCreate(
                model_name="alias-1", channel_id=backup.id, upstream_model="real-model", priority=100
            )
        )
        _, plain = service.create_key("测试密钥", None)

    def install(handler):
        """把代理出站请求接到伪造的上游，并记录它到底收到了什么。"""
        seen: list[httpx.Request] = []
        real_client = httpx.AsyncClient

        def wrapper(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(wrapper)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return seen

    return TestClient(app), Session, plain, install


def _ok_json(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "上游回答"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


def test_chat_completion_is_forwarded_and_logged(env):
    client, Session, plain, install = env
    seen = install(lambda request: _ok_json(json.loads(request.content)["model"]))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "你好"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "上游回答"
    assert response.headers["X-Yangvis-Request-Id"]

    # 打的是优先级最高的那条通道。
    assert str(seen[0].url) == f"{PRIMARY}/chat/completions"
    # 调用方的网关密钥绝不能透传给上游，换上去的必须是通道自己的 key。
    assert seen[0].headers["authorization"] == "Bearer upstream-primary"
    assert plain not in seen[0].headers["authorization"]

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is True
        assert log.status_code == 200
        assert log.channel_name == "primary"
        assert (log.prompt_tokens, log.completion_tokens, log.total_tokens) == (11, 7, 18)
        assert log.request_body and "你好" in log.request_body
        assert log.response_body == "上游回答"
        assert log.stream is False
        key = db.scalar(select(AiApiKey))
        assert key.call_count == 1
        assert key.last_used_at is not None


def test_upstream_model_mapping_is_applied(env):
    client, _, plain, install = env
    seen = install(lambda request: _ok_json(json.loads(request.content)["model"]))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "alias-1", "messages": []},
    )
    assert response.status_code == 200
    # 对外叫 alias-1，发给上游必须换成 real-model。
    assert json.loads(seen[0].content)["model"] == "real-model"


def test_failover_to_next_channel(env):
    client, Session, plain, install = env

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(PRIMARY):
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})
        return _ok_json("gpt-4o")

    seen = install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": []},
    )

    assert response.status_code == 200
    assert [str(request.url) for request in seen] == [
        f"{PRIMARY}/chat/completions",
        f"{BACKUP}/chat/completions",
    ]

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        # 最终应答的是 backup，但 primary 那次失败必须留在轨迹里。
        assert log.channel_name == "backup"
        assert log.success is True
        assert [attempt["channel_name"] for attempt in log.attempts] == ["primary"]
        assert log.attempts[0]["status_code"] == 502


def test_client_error_is_not_retried(env):
    client, Session, plain, install = env
    seen = install(lambda request: httpx.Response(400, json={"error": {"message": "参数非法"}}))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": []},
    )
    # 400 换谁都一样，只打一次；错误体原样回给调用方。
    assert len(seen) == 1
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "参数非法"

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is False
        assert log.status_code == 400


def test_streaming_is_relayed_and_audited(env):
    client, Session, plain, install = env

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # 网关默认会注入 include_usage 来拿 token 数。
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            content=sse(
                json.dumps({"choices": [{"delta": {"content": "流式"}}]}, ensure_ascii=False),
                json.dumps({"choices": [{"delta": {"content": "回答"}}]}, ensure_ascii=False),
                json.dumps(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    }
                ),
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": [], "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    body = response.text
    # 原样透传：上游那几个 chunk 一个不少地到了调用方手里。
    assert "流式" in body and "回答" in body and "[DONE]" in body

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.stream is True
        assert log.success is True
        assert log.response_body == "流式回答"
        assert log.total_tokens == 7
        assert log.first_token_ms is not None


def test_unsupported_stream_options_degrades_instead_of_failing(env):
    client, Session, plain, install = env

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "stream_options" in payload:
            return httpx.Response(400, json={"error": {"message": "unknown field stream_options"}})
        return httpx.Response(200, content=sse(json.dumps({"choices": [{"delta": {"content": "ok"}}]})),
                             headers={"Content-Type": "text/event-stream"})

    seen = install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": [], "stream": True},
    )
    # 同一条通道去掉 stream_options 再试一次，不该就此判定通道不可用。
    assert response.status_code == 200
    assert len(seen) == 2
    assert str(seen[0].url) == str(seen[1].url) == f"{PRIMARY}/chat/completions"

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is True
        assert log.response_body == "ok"


def test_embeddings_are_forwarded_and_summarised(env):
    client, Session, plain, install = env

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-v3",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    install(handler)
    response = client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "input": "你好"},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.endpoint == "embeddings"
        # 向量本体没有审计价值，记条数与维度即可。
        assert json.loads(log.response_body) == {
            "count": 1,
            "dimensions": 3,
            "model": "text-embedding-v3",
        }
        assert log.total_tokens == 4


def test_models_lists_only_enabled_routes(env):
    client, _, plain, install = env
    install(lambda request: httpx.Response(200, json={}))

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {plain}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert sorted(item["id"] for item in payload["data"]) == ["alias-1", "gpt-4o"]


def test_unknown_model_is_rejected(env):
    client, Session, plain, install = env
    seen = install(lambda request: _ok_json("x"))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "not-configured", "messages": []},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert seen == []

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is False
        assert log.channel_id is None
        # 网关自己拒绝的调用同样留了痕，响应头要能把调用方指回这一行。
        assert response.headers["X-Yangvis-Request-Id"] == log.request_id


def test_authentication_failures(env):
    client, Session, plain, install = env
    seen = install(lambda request: _ok_json("x"))

    missing = client.post("/v1/chat/completions", json={"model": "gpt-4o"})
    assert missing.status_code == 401
    assert missing.json()["error"]["type"] == "authentication_error"
    # 认证就被拒的调用不落日志，给一个查不到东西的 request id 只会误导人。
    assert "X-Yangvis-Request-Id" not in missing.headers

    wrong = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-yv-nope"},
        json={"model": "gpt-4o"},
    )
    assert wrong.status_code == 401

    # 控制台登录用的 JWT 不是网关密钥，两套凭据不能互通。
    jwt = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.fake.signature"},
        json={"model": "gpt-4o"},
    )
    assert jwt.status_code == 401
    assert seen == []


def test_disabled_key_is_rejected(env):
    client, Session, plain, install = env
    install(lambda request: _ok_json("x"))

    with Session() as db:
        AiGatewayService(db).update_key(1, AiApiKeyUpdate(enabled=False))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": []},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "key_disabled"


def test_unreachable_upstream_reports_502(env):
    client, Session, plain, install = env

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "gpt-4o", "messages": []},
    )
    assert response.status_code == 502

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is False
        # 两条通道都试过，轨迹里两条都在。
        assert len(log.attempts) == 2
