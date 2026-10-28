"""AI 网关对外端点的端到端测试。

上游用 httpx.MockTransport 伪造，数据库用 SQLite 内存库，路由解析、故障转移、
SSE 透传与调用留痕走的都是真实代码路径。
"""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.entities import AiApiKey, AiCallLog, AiChannel, AiModelRoute
from app.models.schemas import AiApiKeyUpdate, AiChannelCreate, AiModelRouteCreate
from app.routers import ai_gateway_openai
from app.services.ai_gateway import proxy
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


def _ok_json(model: str, content: str = "上游回答") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
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
    # 出站请求盖了网关戳，万一绕回本站才认得出来（见 test_looped_request_is_refused）。
    assert seen[0].headers[proxy.GATEWAY_HOP_HEADER] == response.headers["X-Yangvis-Request-Id"]

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is True
        assert log.status_code == 200
        assert log.channel_name == "primary"
        assert (log.prompt_tokens, log.completion_tokens, log.total_tokens) == (11, 7, 18)
        # 上游 usage.prompt_tokens_details.cached_tokens 要落到日志里，前端据此算命中率。
        assert log.cached_tokens == 4
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
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            "total_tokens": 7,
                            "prompt_tokens_details": {"cached_tokens": 5},
                        },
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
        assert log.cached_tokens == 5
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


def _limit_gpt4o(Session, *, channel_name: str | None = None, **values) -> None:
    """直接改 gpt-4o 的路由配置；不指定通道时两条候选一起改。"""
    with Session() as db:
        stmt = select(AiModelRoute).where(AiModelRoute.model_name == "gpt-4o")
        if channel_name:
            stmt = stmt.join(AiChannel, AiChannel.id == AiModelRoute.channel_id).where(
                AiChannel.name == channel_name
            )
        for route in db.scalars(stmt):
            for key, value in values.items():
                setattr(route, key, value)
        db.commit()


def _chat(client, plain, model="gpt-4o"):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": model, "messages": [{"role": "user", "content": "你好"}]},
    )


def test_expired_primary_fails_over_to_backup(env):
    client, Session, plain, install = env
    _limit_gpt4o(
        Session, channel_name="primary", expires_at=datetime.now() - timedelta(hours=1)
    )
    seen = install(lambda request: _ok_json(json.loads(request.content)["model"]))

    response = _chat(client, plain)

    assert response.status_code == 200
    # 到期的 primary 一次都不能打，直接走 backup。
    assert len(seen) == 1
    assert str(seen[0].url).startswith(BACKUP)


def test_all_routes_expired_returns_403(env):
    client, Session, plain, install = env
    _limit_gpt4o(Session, expires_at=datetime.now() - timedelta(minutes=1))

    def must_not_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"过期路由不该触达上游：{request.url}")

    seen = install(must_not_call)
    response = _chat(client, plain)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "model_expired"
    assert error["type"] == "permission_error"
    assert seen == []

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is False
        assert log.status_code == 403
        assert log.channel_id is None
        assert response.headers["X-Yangvis-Request-Id"] == log.request_id


def test_quota_exhausted_returns_429_and_is_not_retried(env):
    client, Session, plain, install = env
    # 两条候选都只剩 0 可用额度：换通道也没用，一次上游都不该打。
    _limit_gpt4o(Session, token_quota=1, token_used=1)
    seen = install(lambda request: _ok_json("gpt-4o"))

    response = _chat(client, plain)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "model_quota_exceeded"
    assert error["type"] == "insufficient_quota"
    assert seen == []


def test_consumed_tokens_accrue_then_block_the_next_call(env):
    client, Session, plain, install = env
    seen = install(lambda request: _ok_json(json.loads(request.content)["model"]))

    assert _chat(client, plain).status_code == 200
    with Session() as db:
        route = db.scalar(
            select(AiModelRoute)
            .join(AiChannel, AiChannel.id == AiModelRoute.channel_id)
            .where(AiModelRoute.model_name == "gpt-4o", AiChannel.name == "primary")
        )
        # _ok_json 里上游回报 total_tokens=18，必须累加进应答那条路由的额度账。
        assert route.token_used == 18

    # primary 已用满，本次故障转移到 backup。
    _limit_gpt4o(Session, token_quota=18)
    response = _chat(client, plain)
    assert response.status_code == 200
    assert str(seen[-1].url).startswith(BACKUP)

    # 两条都到顶：再调就是 429。
    response = _chat(client, plain)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "model_quota_exceeded"


def test_auto_pool_exhausted_reports_blocked_count(env):
    client, Session, plain, install = env
    install(lambda request: _ok_json("gpt-4o"))
    with Session() as db:
        for route in db.scalars(select(AiModelRoute).where(AiModelRoute.model_name == "gpt-4o")):
            route.auto_eligible = True
            route.auto_tags = ["chat"]
            route.token_quota = 1
            route.token_used = 1
        db.commit()

    response = _chat(client, plain, model="auto")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "auto_model_not_found"
    assert "2 条" in error["message"]

    # auto 候选池空了就不该再出现在模型列表里；点名 gpt-4o 同样被额度挡住。
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {plain}"}).json()["data"]
    assert "auto" not in {item["id"] for item in models}


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


def test_looped_request_is_refused(env):
    """通道 base_url 指回本站时，绕回来的请求必须在打出去之前就被掐断。"""
    client, Session, plain, install = env
    seen = install(lambda request: _ok_json("x"))

    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {plain}",
            proxy.GATEWAY_HOP_HEADER: "outer-request-id",
        },
        json={"model": "gpt-4o", "messages": []},
    )
    assert response.status_code == proxy.LOOP_STATUS == 508
    assert response.json()["error"]["code"] == "gateway_loop_detected"
    # 508 不在 FAILOVER_STATUS 里：换一条通道照样绕回来，重试只是拖长失败时间。
    assert seen == []

    with Session() as db:
        # 这一圈不留日志行，触发它的外层调用会把错误原样记下。
        assert db.scalar(select(AiCallLog)) is None


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


# ---- Auto 模型 --------------------------------------------------------------

EYES = "https://eyes.test/v1"
PLAIN = "https://plain.test/v1"
STRONG = "https://strong.test/v1"

IMAGE_BODY = {
    "model": "auto",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图里有什么"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
        }
    ],
}


def _seed_auto(Session, *specs):
    """铺一套参与 Auto 的通道与路由：(通道名, base_url, 对外模型名, 优先级, 标签)。"""
    with Session() as db:
        service = AiGatewayService(db)
        for channel_name, base_url, model_name, priority, tags in specs:
            channel = service.create_channel(
                AiChannelCreate(name=channel_name, base_url=base_url, api_key=f"key-{channel_name}")
            )
            service.create_route(
                AiModelRouteCreate(
                    model_name=model_name,
                    channel_id=channel.id,
                    priority=priority,
                    auto_eligible=True,
                    auto_tags=tags,
                )
            )


def test_auto_hands_an_image_to_the_route_that_sees(env):
    client, Session, plain, install = env
    _seed_auto(
        Session,
        ("plain", PLAIN, "text-model", 100, ["chat"]),
        ("eyes", EYES, "vision-model", 200, ["chat", "vision"]),
    )
    seen = install(lambda request: _ok_json(json.loads(request.content)["model"]))

    response = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {plain}"}, json=IMAGE_BODY
    )

    assert response.status_code == 200
    # 优先级更低的纯文本模型被硬性要求筛掉了，而且没人声明任务类型，
    # 所以一次分类请求都不该发——总共只有这一发出站请求。
    assert len(seen) == 1
    assert str(seen[0].url) == f"{EYES}/chat/completions"
    assert json.loads(seen[0].content)["model"] == "vision-model"
    assert seen[0].headers["authorization"] == "Bearer key-eyes"

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.model == "auto"
        assert log.upstream_model == "vision-model"
        assert log.auto_route["required"] == ["chat", "vision"]
        assert log.auto_route["task"] is None
        assert log.auto_route["source"] == "priority"
        assert log.auto_route["resolved_model"] == "vision-model"


def test_auto_asks_the_cheapest_route_what_kind_of_job_this_is(env):
    client, Session, plain, install = env
    _seed_auto(
        Session,
        ("cheap", PLAIN, "fast-model", 100, ["chat", "general"]),
        ("strong", STRONG, "code-model", 300, ["chat", "code"]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if str(payload["messages"][0]["content"]).startswith("你在替网关挑选模型"):
            return _ok_json("classifier", content="code")
        return _ok_json(payload["model"])

    seen = install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "auto", "messages": [{"role": "user", "content": "这个报错怎么修"}]},
    )

    assert response.status_code == 200
    # 先问池子里优先级最低的那条，再按回答打给会写代码的那条。
    assert [str(request.url) for request in seen] == [
        f"{PLAIN}/chat/completions",
        f"{STRONG}/chat/completions",
    ]
    assert json.loads(seen[1].content)["model"] == "code-model"

    with Session() as db:
        rows = db.scalars(select(AiCallLog)).all()
        # 分类那一次不落日志：它是网关替调用方做的决定，不是调用方发起的调用。
        assert len(rows) == 1
        log = rows[0]
        assert log.channel_name == "strong"
        assert log.auto_route["task"] == "code"
        assert log.auto_route["source"] == "llm"
        assert log.auto_route["matched"] == 2
        assert log.auto_route["resolved_model"] == "code-model"


def test_auto_reports_404_when_nothing_declares_the_capability(env):
    client, Session, plain, install = env
    _seed_auto(Session, ("plain", PLAIN, "text-model", 100, ["chat"]))
    seen = install(lambda request: _ok_json("x"))

    response = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {plain}"}, json=IMAGE_BODY
    )

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "auto_model_not_found"
    # 报错要说清缺的是哪个标签，否则管理员只能挨条翻路由。
    assert "vision" in error["message"]
    assert seen == []

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.success is False
        assert log.auto_route["matched"] == 0


def test_auto_fails_over_within_the_pool(env):
    client, Session, plain, install = env
    _seed_auto(
        Session,
        ("a", PRIMARY, "fast-model", 100, ["chat"]),
        ("b", BACKUP, "second-model", 200, ["chat"]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(PRIMARY):
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})
        return _ok_json("second-model")

    seen = install(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]},
    )

    assert response.status_code == 200
    assert [str(request.url) for request in seen] == [
        f"{PRIMARY}/chat/completions",
        f"{BACKUP}/chat/completions",
    ]

    with Session() as db:
        log = db.scalar(select(AiCallLog))
        assert log.channel_name == "b"
        # 决策快照记的是实际应答的那条，不是最初选中的那条。
        assert log.auto_route["resolved_model"] == "second-model"
        assert [attempt["channel_name"] for attempt in log.attempts] == ["a"]


def test_auto_appears_in_the_model_list_once_someone_opts_in(env):
    client, Session, plain, install = env
    install(lambda request: httpx.Response(200, json={}))

    assert "auto" not in [
        item["id"]
        for item in client.get("/v1/models", headers={"Authorization": f"Bearer {plain}"})
        .json()["data"]
    ]

    _seed_auto(Session, ("plain", PLAIN, "text-model", 100, ["chat"]))
    ids = [
        item["id"]
        for item in client.get("/v1/models", headers={"Authorization": f"Bearer {plain}"}).json()[
            "data"
        ]
    ]
    assert "auto" in ids
    assert "text-model" in ids


def test_auto_works_for_embeddings_without_any_classification(env):
    client, Session, plain, install = env
    _seed_auto(
        Session,
        ("talk", PLAIN, "chat-model", 100, ["chat"]),
        ("vec", EYES, "embed-model", 200, ["embedding"]),
    )
    seen = install(lambda request: httpx.Response(200, json={"data": [], "model": "embed-model"}))

    response = client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {plain}"},
        json={"model": "auto", "input": "一段文本"},
    )

    assert response.status_code == 200
    assert len(seen) == 1
    assert str(seen[0].url) == f"{EYES}/embeddings"
    assert json.loads(seen[0].content)["model"] == "embed-model"
