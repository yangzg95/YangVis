"""通知发送器：payload 构造、加签向量与发送循环（含重试语义）。

签名算法对着**预计算的已知向量**钉——飞书与钉钉的 HmacSHA256 参数位置正好
相反（飞书以 ``"{ts}\\n{secret}"`` 为 key、空串为消息；钉钉反过来），重构时
只要有一家被写反，这里的固定值就会红。向量由独立脚本一次性生成，不与实现
共享任何代码路径。

发送循环用 ``httpx.MockTransport`` 打桩，验证的是真实现：5xx 重试一次、4xx
与渠道业务错误是终态、generic 不强解 JSON。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.notify.senders import (
    NotifySendError,
    SendResult,
    _check_response,
    build_payload,
    dingtalk_sign,
    feishu_sign,
    send_to_channel,
    signed_url,
)

SECRET = "test-secret"
TS = 1700000000

# 独立生成的已知向量（见模块 docstring）。
FEISHU_SIGN_TS = "mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84="
DINGTALK_SIGN_TS_MS = "BYMqUCZnSqbfPf1GCfZftO7Rg2g6P+Rp3/4+bLNtSGA="
DINGTALK_SIGN_QUOTED = "BYMqUCZnSqbfPf1GCfZftO7Rg2g6P%2BRp3%2F4%2BbLNtSGA%3D"


# ---- 签名 -------------------------------------------------------------------


def test_feishu_sign_known_vector():
    assert feishu_sign(TS, SECRET) == FEISHU_SIGN_TS


def test_dingtalk_sign_known_vector():
    # 返回值已 quote_plus：base64 的 + / = 会转义，机器人 URL 要求这个形态。
    assert dingtalk_sign(TS * 1000, SECRET) == DINGTALK_SIGN_QUOTED


def test_sign_changes_with_secret():
    assert feishu_sign(TS, "other") != feishu_sign(TS, SECRET)
    assert dingtalk_sign(TS * 1000, "other") != dingtalk_sign(TS * 1000, SECRET)


# ---- payload ---------------------------------------------------------------


def test_build_payload_feishu():
    payload = build_payload("feishu", event="e", title="t", body="b", text="hello")
    assert payload == {"msg_type": "text", "content": {"text": "hello"}}


def test_build_payload_wecom_and_dingtalk():
    # 企微与钉钉的 text 字段结构相同但外层 key 不同，两家各钉一份防止串。
    assert build_payload("wecom", event="e", title="t", body="b", text="x") == {
        "msgtype": "text",
        "text": {"content": "x"},
    }
    assert build_payload("dingtalk", event="e", title="t", body="b", text="x") == {
        "msgtype": "text",
        "text": {"content": "x"},
    }


def test_build_payload_generic_is_structured():
    payload = build_payload("generic", event="workflow.failed", title="t", body="b", text="ignored")
    assert payload["event"] == "workflow.failed"
    assert payload["title"] == "t"
    assert payload["body"] == "b"
    assert "sent_at" in payload
    # generic 不吃 text，混进去反而误导自建接收端。
    assert "text" not in payload and "content" not in payload


# ---- 签名 URL ---------------------------------------------------------------


def test_signed_url_without_secret_unchanged():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    for kind in ("feishu", "dingtalk", "wecom", "generic"):
        assert signed_url(kind, url, None, now=TS) == url


def test_signed_url_feishu_appends_query():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    signed = signed_url("feishu", url, SECRET, now=TS)
    assert signed == f"{url}&timestamp={TS}&sign={FEISHU_SIGN_TS}"


def test_signed_url_dingtalk_appends_query():
    url = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    signed = signed_url("dingtalk", url, SECRET, now=TS)
    assert signed == f"{url}&timestamp={TS * 1000}&sign={DINGTALK_SIGN_QUOTED}"


def test_signed_url_wecom_ignores_secret():
    # 企微机器人没有加签形态；多传了 secret 也不该把它拼进 URL。
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    assert signed_url("wecom", url, SECRET, now=TS) == url


# ---- 响应判定 ---------------------------------------------------------------


def test_check_response_feishu_new_style_error():
    with pytest.raises(NotifySendError, match="code=9499"):
        _check_response("feishu", {"code": 9499, "msg": "sign match fail"})


def test_check_response_feishu_old_style():
    # 旧版飞书返回 StatusCode / StatusMessage，两套 key 都要认。
    with pytest.raises(NotifySendError):
        _check_response("feishu", {"StatusCode": 19021, "StatusMessage": "sign error"})
    _check_response("feishu", {"StatusCode": 0})


def test_check_response_wecom_dingtalk_error():
    with pytest.raises(NotifySendError, match="errcode=93000"):
        _check_response("wecom", {"errcode": 93000, "errmsg": "invalid webhook url"})
    with pytest.raises(NotifySendError, match="errcode=310000"):
        _check_response("dingtalk", {"errcode": 310000, "errmsg": "sign not match"})


def test_check_response_ok_cases():
    _check_response("feishu", {"code": 0, "msg": "success"})
    _check_response("wecom", {"errcode": 0, "errmsg": "ok"})
    _check_response("dingtalk", {"errcode": 0})
    # generic 不看正文。
    _check_response("generic", {"anything": True})


# ---- 发送循环 ----------------------------------------------------------------


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _send(client, **overrides):
    kwargs = dict(
        kind="wecom",
        webhook_url="https://example.com/hook",
        secret=None,
        event="workflow.failed",
        title="t",
        body="b",
        text="x",
    )
    kwargs.update(overrides)
    return asyncio.run(send_to_channel(client, **kwargs))


def test_send_success_records_elapsed():
    client = _client(lambda req: httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}))
    result = _send(client)
    assert isinstance(result, SendResult)
    assert result.ok and result.error is None
    assert result.elapsed_ms is not None and result.elapsed_ms >= 0


def test_send_business_error_is_terminal():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    result = _send(_client(handler))
    assert not result.ok
    assert "93000" in (result.error or "")
    # 渠道侧业务错误重试没有意义，必须一次即终态。
    assert len(calls) == 1


def test_send_5xx_retries_once_then_succeeds():
    states = {"n": 0}

    def handler(req):
        states["n"] += 1
        if states["n"] == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"errcode": 0})

    result = _send(_client(handler))
    assert result.ok
    assert states["n"] == 2


def test_send_5xx_twice_fails_without_raising():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(500)

    result = _send(_client(handler))
    assert not result.ok
    assert result.error == "HTTP 500"
    assert len(calls) == 2


def test_send_4xx_is_terminal():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(403)

    result = _send(_client(handler))
    assert not result.ok
    assert result.error == "HTTP 403"
    # 限流 / 地址错误重试同样会被拒，不该白等。
    assert len(calls) == 1


def test_send_connect_error_retries_then_fails():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectError("connection refused")

    result = _send(_client(handler))
    assert not result.ok
    assert "网络错误" in (result.error or "")
    assert len(calls) == 2


def test_send_generic_accepts_plain_text_2xx():
    client = _client(lambda req: httpx.Response(200, text="ok"))
    result = _send(client, kind="generic")
    assert result.ok


def test_send_non_json_2xx_body_is_terminal_failure():
    # IM 渠道返回 2xx 但正文不是 JSON：按失败落日志，不重试。
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text="<html>gateway echo</html>")

    result = _send(_client(handler))
    assert not result.ok
    assert len(calls) == 1


def test_send_dingtalk_signed_url_and_payload_reach_wire():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.read().decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0})

    client = _client(handler)
    result = _send(
        client,
        kind="dingtalk",
        webhook_url="https://oapi.dingtalk.com/robot/send?access_token=abc",
        secret=SECRET,
    )
    assert result.ok
    assert "timestamp=" in seen["url"]
    assert "sign=" in seen["url"]
    # 请求体确实是钉钉的 text 方言，而不是飞书/企微的。
    assert seen["body"] == {"msgtype": "text", "text": {"content": "x"}}
