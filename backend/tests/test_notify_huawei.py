"""华为 HMS Push 发送器：OAuth 取 token、messages:send 的请求形状与重试语义。

不出网——全程 ``httpx.MockTransport``，按 URL 区分 OAuth 与 push 两类请求，
分别断言 URL、表单 / JSON 形状、Authorization 头。token 缓存、鉴权失效刷新、
5xx / 网络错误重试、业务码终态这些分支都在这里钉死，重构时任何一处走样都会红。
两种授权方式（client_credentials 与服务账号 JWT bearer）各有一组请求形状断言。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.notify import huawei as huawei_module
from app.services.notify.huawei import (
    GRANT_CLIENT_CREDENTIALS,
    GRANT_JWT_BEARER,
    JWT_ALGORITHM,
    OAUTH_TOKEN_URL,
    PUSH_SEND_URL,
    SUCCESS_CODE,
    TARGET_TOKEN,
    TARGET_TOPIC,
    HuaweiAuth,
    HuaweiTokenCache,
    build_huawei_message,
    parse_service_account,
    send_huawei_message,
)
from app.services.notify.senders import NotifySendError

APP_ID = "119672293"
CLIENT_ID = "client-id-x"
CLIENT_SECRET = "client-secret-y"


@pytest.fixture(scope="module")
def service_account() -> dict:
    """测试用服务账号：现签一把 RSA 私钥（非真实凭据），模拟 AGC JSON 的三要素。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {"key_id": "test-kid-1234", "sub_account": "test-sub-9", "private_key": pem}


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


def _send(client, *, auth: HuaweiAuth | None = None, **overrides):
    kwargs = dict(
        app_id=APP_ID,
        auth=auth or HuaweiAuth.client_credentials(CLIENT_ID, CLIENT_SECRET),
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
    # 线上 80100003 "No valid android notification payload" 的根因：
    # Android 投递必须带 android.notification，且 click_action 显式声明。
    assert message["android"]["notification"] == {
        "title": "t",
        "body": "b",
        "click_action": {"type": 3},
    }
    # data 必须是字符串（HMS 不收对象），且能还原成原字典。
    assert json.loads(message["data"]) == {"event": "workflow.failed"}


def test_build_message_token_target_is_array():
    # 线上 80100003 的根因：message.token 必须是字符串数组，裸字符串会被 HMS 拒。
    msg = build_huawei_message(
        target_type=TARGET_TOKEN, target="device-token-1", title="t", body="b"
    )
    assert msg["message"]["token"] == ["device-token-1"]
    assert isinstance(msg["message"]["token"], list)
    assert "topic" not in msg["message"]
    # 不带 data 时不应出现该字段。
    assert "data" not in msg["message"]


def test_build_message_rejects_unknown_target():
    with pytest.raises(NotifySendError):
        build_huawei_message(
            target_type="condition", target="x", title="t", body="b"
        )


# ---- 服务账号 JSON 校验（落库前的守门人）---------------------------------------


def test_parse_service_account_accepts_valid_shape(service_account):
    parsed = parse_service_account(json.dumps(service_account))
    assert parsed["sub_account"] == "test-sub-9"
    assert "PRIVATE KEY" in parsed["private_key"]


def test_parse_service_account_rejects_broken_json():
    with pytest.raises(ValueError, match="JSON 格式不合法"):
        parse_service_account("{not json")


def test_parse_service_account_rejects_non_object():
    with pytest.raises(ValueError, match="顶层必须是对象"):
        parse_service_account('["a", "b"]')


@pytest.mark.parametrize("missing_key", ["key_id", "sub_account", "private_key"])
def test_parse_service_account_rejects_missing_key(service_account, missing_key):
    broken = dict(service_account)
    broken.pop(missing_key)
    with pytest.raises(ValueError, match=missing_key):
        parse_service_account(json.dumps(broken))


def test_parse_service_account_rejects_bad_pem(service_account):
    broken = dict(service_account)
    broken["private_key"] = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    with pytest.raises(ValueError, match="PEM"):
        parse_service_account(json.dumps(broken))


def test_huawei_auth_cache_key_differs_by_mode_and_identity(service_account):
    cc = HuaweiAuth.client_credentials(CLIENT_ID, CLIENT_SECRET)
    jwt_auth = HuaweiAuth.service_account(service_account)
    assert cc.cache_key == f"cc:{CLIENT_ID}"
    assert jwt_auth.cache_key == "jwt:test-sub-9"
    assert cc.cache_key != jwt_auth.cache_key


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
    assert body["message"]["android"]["notification"]["click_action"] == {"type": 3}


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


# ---- 服务账号 JWT bearer 授权 --------------------------------------------------


def test_service_account_sends_jwt_bearer_assertion(service_account):
    from urllib.parse import parse_qs

    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    seen: list = []
    auth = HuaweiAuth.service_account(service_account)
    result = _send(_client(_recording_handler(seen)), auth=auth)
    assert result.ok

    oauth_req, push_req = seen
    form = parse_qs(oauth_req.content.decode())
    # grant_type 换成 jwt-bearer，断言 JWT 走 assertion 字段；绝不能带客户端密钥。
    assert form["grant_type"] == [GRANT_JWT_BEARER]
    assert "assertion" in form and len(form["assertion"][0]) > 100
    assert "client_id" not in form and "client_secret" not in form

    # 断言 JWT 的头与载荷按 AGC 服务账号规范签名，可用私钥对应的公钥验签。
    private = load_pem_private_key(service_account["private_key"].encode(), password=None)
    header = jwt.get_unverified_header(form["assertion"][0])
    assert header == {"kid": "test-kid-1234", "typ": "JWT", "alg": JWT_ALGORITHM}
    claims = jwt.decode(
        form["assertion"][0],
        private.public_key(),
        algorithms=[JWT_ALGORITHM],
        options={"verify_aud": False},
    )
    assert claims["iss"] == "test-sub-9"
    assert claims["aud"] == OAUTH_TOKEN_URL
    assert claims["exp"] - claims["iat"] == 3600

    # push 请求与 client_credentials 模式完全同形，Bearer 头照带。
    assert str(push_req.url) == PUSH_SEND_URL.format(app_id=APP_ID)
    assert push_req.headers["authorization"] == "Bearer AT"


def test_token_cache_isolated_between_credential_modes(service_account):
    from urllib.parse import parse_qs

    seen: list = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (200, {"code": SUCCESS_CODE}),
            (200, {"code": SUCCESS_CODE}),
        ],
    )
    client = _client(handler)
    # 两套不同身份的凭据各发一次：缓存键不同，必须各换一次 token。
    _send(client)
    _send(client, auth=HuaweiAuth.service_account(service_account))

    oauth_calls = [r for r in seen if str(r.url) == OAUTH_TOKEN_URL]
    grant_seen = {
        parse_qs(r.content.decode())["grant_type"][0] for r in oauth_calls
    }
    assert len(oauth_calls) == 2
    assert GRANT_CLIENT_CREDENTIALS in grant_seen
    assert GRANT_JWT_BEARER in grant_seen


def test_service_account_token_is_cached_across_sends(service_account):
    seen: list = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (200, {"code": SUCCESS_CODE}),
            (200, {"code": SUCCESS_CODE}),
        ],
    )
    client = _client(handler)
    auth = HuaweiAuth.service_account(service_account)
    _send(client, auth=auth)
    _send(client, auth=auth)

    oauth_calls = [r for r in seen if str(r.url) == OAUTH_TOKEN_URL]
    assert len(oauth_calls) == 1


def test_broken_private_key_fails_without_escaping():
    # 绕过 parse_service_account 直接构造凭据，模拟库里私钥损坏的情形：
    # 签名异常必须压成失败结果，且不能发出任何 push 请求。
    seen: list = []
    bad_auth = HuaweiAuth.service_account(
        {"key_id": "k", "sub_account": "s", "private_key": "not-a-pem"}
    )
    result = _send(_client(_recording_handler(seen)), auth=bad_auth)

    assert not result.ok
    assert "JWT" in (result.error or "")
    # 签 JWT 在任何 HTTP 请求之前，失败后不应触网。
    assert seen == []


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


def test_service_account_push_permission_denied_gets_console_hint(service_account):
    # 服务账号能换到 token，但 push 两次都 80200001：终态错误要带 AGC 角色排查引导，
    # 而不是只甩一个英文 Authentication Error 给用户。
    seen: list = []
    handler = _recording_handler(
        seen,
        push_queue=[
            (200, {"code": "80200001", "msg": "Authentication Error"}),
            (200, {"code": "80200001", "msg": "Authentication Error"}),
        ],
    )
    auth = HuaweiAuth.service_account(service_account)
    result = _send(_client(handler), auth=auth)

    assert not result.ok
    assert "80200001" in (result.error or "")
    assert "服务账号" in (result.error or "")
    assert "角色" in (result.error or "")
    # 强制刷新过一次：共两次 OAuth、两次 push。
    assert len([r for r in seen if str(r.url) == OAUTH_TOKEN_URL]) == 2
    assert len([r for r in seen if str(r.url) != OAUTH_TOKEN_URL]) == 2


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
