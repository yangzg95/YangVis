"""华为 HMS Push 发送器：OAuth 取 token、messages:send 的请求形状与重试语义。

不出网——全程 ``httpx.MockTransport``，按 URL 区分 OAuth 与 push 两类请求，
分别断言 URL、表单 / JSON 形状、Authorization 头。token 缓存、鉴权失效刷新、
5xx / 网络错误重试、业务码终态这些分支都在这里钉死，重构时任何一处走样都会红。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.notify import huawei as huawei_module
from app.services.notify.huawei import (
    OAUTH_TOKEN_URL,
    PUSH_SEND_URL,
    SUCCESS_CODE,
    TARGET_TOKEN,
    TARGET_TOPIC,
    HuaweiTokenCache,
    build_huawei_message,
    send_huawei_message,
)
from app.services.notify.senders import NotifySendError

APP_ID = "119672293"
CLIENT_ID = "client-id-x"
CLIENT_SECRET = "client-secret-y"


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    # 模块级单例会跨用例残留 token 与锁；每个用例换一个全新缓存，
    # OAuth 调用次数才能被精确断言。
    huawei_module.token_cache = HuaweiTokenCache()
    yield
    huawei_module.token_cache = HuaweiTokenCache()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def _send(client, **overrides):
    kwargs = dict(
        app_id=APP_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        target_type=TARGET_TOPIC,
        target="yangvis",
        title="工作流运行失败：x #1",
        body="步骤超时",
        data={"event": "workflow.failed"},
    )
    kwargs.update(overrides)
    return _run(send_huawei_message(client, **kwargs))


def _recording_handler(
    seen: list,
    *,
    oauth_status: int = 200,
    oauth_payload: dict | None = None,
    push_queue: list | None = None,
):
    """按 URL 分流的桩。push_queue 里的 (status, payload) 逐次弹出。"""
    oauth_payload = oauth_payload or {"access_token": "AT", "expires_in": 3600}
    push_queue = push_queue or [(200, {"code": SUCCESS_CODE, "msg": "Success"})]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == OAUTH_TOKEN_URL:
            if oauth_status >= 400:
                return httpx.Response(oauth_status, json={"error": "invalid_client"})
            return httpx.Response(oauth_status, json=oauth_payload)
        status, payload = push_queue.pop(0)
        if payload is None:
            return httpx.Response(status, text="<html>gateway error</html>")
        return httpx.Response(status, json=payload)

    return handler


# ---- 请求体构造（纯函数）-----------------------------------------------------


def test_build_message_topic_shape():
    msg = build_huawei_message(
        target_type=TARGET_TOPIC,
        target="yangvis",
        title="t",
        body="b",
        data={"event": "workflow.failed"},
    )
    assert msg["validate_only"] is False
    message = msg["message"]
    assert message["topic"] == "yangvis"
    assert "token" not in message
    assert message["notification"] == {"title": "t", "body": "b"}
    # data 必须是字符串（HMS 不收对象），且能还原成原字典。
    assert json.loads(message["data"]) == {"event": "workflow.failed"}


def test_build_message_token_target():
    msg = build_huawei_message(
        target_type=TARGET_TOKEN, target="device-token-1", title="t", body="b"
    )
    assert msg["message"]["token"] == "device-token-1"
    assert "topic" not in msg["message"]
    # 不带 data 时不应出现该字段。
    assert "data" not in msg["message"]


def test_build_message_rejects_unknown_target():
    with pytest.raises(NotifySendError):
        build_huawei_message(
            target_type="condition", target="x", title="t", body="b"
        )


# ---- 成功路径与线上请求形状 ---------------------------------------------------


def test_send_success_wires_oauth_and_push_requests():
    seen: list[httpx.Request] = []
    result = _send(_client(_recording_handler(seen)))

    assert result.ok and result.error is None
    assert len(seen) == 2
    oauth_req, push_req = seen

    # OAuth：表单提交 client_credentials，三项参数齐全。
    assert str(oauth_req.url) == OAUTH_TOKEN_URL
    form = oauth_req.content.decode()
    assert "grant_type=client_credentials" in form
    assert f"client_id={CLIENT_ID}" in form
    assert f"client_secret={CLIENT_SECRET}" in form

    # push：URL 带 appid，Bearer 头，body 是主题通知。
    assert str(push_req.url) == PUSH_SEND_URL.format(app_id=APP_ID)
    assert push_req.headers["authorization"] == "Bearer AT"
    body = json.loads(push_req.content.decode())
    assert body["message"]["topic"] == "yangvis"
    assert body["message"]["notification"]["title"] == "工作流运行失败：x #1"


def test_token_is_cached_across_sends():
    seen: list[httpx.Request] = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (200, {"code": SUCCESS_CODE}),
            (200, {"code": SUCCESS_CODE}),
        ],
    )
    client = _client(handler)
    _send(client)
    _send(client)

    oauth_calls = [r for r in seen if str(r.url) == OAUTH_TOKEN_URL]
    push_calls = [r for r in seen if str(r.url) != OAUTH_TOKEN_URL]
    # 第二次发送命中缓存，不再换 token；两条消息都发出去了。
    assert len(oauth_calls) == 1
    assert len(push_calls) == 2


def test_numeric_success_code_is_accepted():
    # 官方返回字符串，但顺手容忍数字形态，避免上游轻微变动就判失败。
    seen: list = []
    handler = _recording_handler(
        seen, push_queue=[(200, {"code": 80000000, "msg": "Success"})]
    )
    result = _send(_client(handler))
    assert result.ok


# ---- 鉴权失效：刷新 token 后重试一次 ------------------------------------------


def test_http_401_refreshes_token_and_retries():
    seen: list = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (401, {"code": 401, "msg": "token expired"}),
            (200, {"code": SUCCESS_CODE}),
        ],
    )
    result = _send(_client(handler))

    assert result.ok
    assert len([r for r in seen if str(r.url) == OAUTH_TOKEN_URL]) == 2
    assert len([r for r in seen if str(r.url) != OAUTH_TOKEN_URL]) == 2


def test_business_token_invalid_code_refreshes_and_retries():
    seen: list = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (200, {"code": "80200001", "msg": "access token invalid"}),
            (200, {"code": SUCCESS_CODE}),
        ],
    )
    result = _send(_client(handler))

    assert result.ok
    assert len([r for r in seen if str(r.url) == OAUTH_TOKEN_URL]) == 2


def test_403_twice_is_failure():
    seen: list = []
    handler = _recording_handler(seen, push_queue=[(403, None), (403, None)])
    result = _send(_client(handler))
    assert not result.ok
    assert "403" in (result.error or "")


# ---- 终态错误：不重试 ---------------------------------------------------------


def test_4xx_is_terminal():
    seen: list = []
    handler = _recording_handler(seen, push_queue=[(400, {"code": 400})])
    result = _send(_client(handler))
    assert not result.ok
    # 一次 push 即终态，OAuth 只在开头调一次。
    assert len(seen) == 2


def test_other_business_code_is_terminal():
    seen: list = []
    handler = _recording_handler(
        seen, push_queue=[(200, {"code": "80000001", "msg": "bad argument"})]
    )
    result = _send(_client(handler))
    assert not result.ok
    assert "80000001" in (result.error or "")
    assert len([r for r in seen if str(r.url) != OAUTH_TOKEN_URL]) == 1


def test_oauth_rejection_is_terminal_and_skips_push():
    seen: list = []
    handler = _recording_handler(seen, oauth_status=401)
    result = _send(_client(handler))
    assert not result.ok
    assert "OAuth" in (result.error or "")
    # 凭据被 OAuth 拒，重试无意义，绝不应该发起 push。
    assert all(str(r.url) == OAUTH_TOKEN_URL for r in seen)


def test_non_json_2xx_is_failure():
    seen: list = []
    handler = _recording_handler(seen, push_queue=[(200, None)])
    result = _send(_client(handler))
    assert not result.ok
    assert "无法解析" in (result.error or "")


# ---- 5xx 与网络错误：重试一次 --------------------------------------------------


def test_5xx_twice_fails():
    seen: list = []
    handler = _recording_handler(seen, push_queue=[(500, None), (500, None)])
    result = _send(_client(handler))
    assert not result.ok
    assert len([r for r in seen if str(r.url) != OAUTH_TOKEN_URL]) == 2


def test_push_transport_error_retries_then_fails():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == OAUTH_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        raise httpx.ConnectError("connection reset")

    result = _send(_client(handler))
    assert not result.ok
    assert "网络错误" in (result.error or "")
    assert len([r for r in seen if str(r.url) != OAUTH_TOKEN_URL]) == 2


def test_oauth_transport_error_retries_then_succeeds():
    seen: list = []
    oauth_attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == OAUTH_TOKEN_URL:
            oauth_attempts["n"] += 1
            if oauth_attempts["n"] == 1:
                raise httpx.ConnectError("oauth unreachable")
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(200, json={"code": SUCCESS_CODE})

    result = _send(_client(handler))
    assert result.ok
    assert oauth_attempts["n"] == 2
