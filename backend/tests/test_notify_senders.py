"""通知发送器：payload 构造、加签向量与发送循环（含重试语义）。

签名算法对着**预计算的已知向量**钉——飞书与钉钉的 HmacSHA256 参数位置正好
相反（飞书以 ``"{ts}\\n{secret}"`` 为 key、空串为消息；钉钉反过来），重构时
只要有一家被写反，这里的固定值就会红。向量由独立脚本一次性生成，不与实现
共享任何代码路径。

签名**落在哪**同样两家的规矩相反：飞书要求 ``timestamp`` / ``sign`` 是请求体
字段，钉钉要求它们是 URL query。挂错位置在测试桩里一样「发出去了」，真机器人
却回 404 / 验签失败，所以这里除了向量还钉 URL 与 body 各自的形状。

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
    _http_error,
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


def test_build_payload_feishu_signed_goes_in_body():
    # 飞书的签名校验字段：timestamp 是**秒级字符串**，与 sign 一起进请求体。
    payload = build_payload(
        "feishu", event="e", title="t", body="b", text="hello", secret=SECRET, now=TS
    )
    assert payload["msg_type"] == "text"
    assert payload["content"] == {"text": "hello"}
    assert payload["timestamp"] == str(TS)
    assert payload["sign"] == FEISHU_SIGN_TS


def test_build_payload_dingtalk_never_signs_the_body():
    # 反过来：钉钉的签名只能挂 URL，body 里多出 timestamp/sign 会被验签拒。
    payload = build_payload(
        "dingtalk", event="e", title="t", body="b", text="x", secret=SECRET, now=TS
    )
    assert payload == {"msgtype": "text", "text": {"content": "x"}}


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
    # sent_at 带 +08:00 偏移：自建接收端猜不到我们容器用什么时区跑的。
    assert payload["sent_at"].endswith("+08:00")
    assert "T" in payload["sent_at"]
    # generic 不吃 text，混进去反而误导自建接收端。
    assert "text" not in payload and "content" not in payload
    # 自建接收端没有约定的加签算法，密钥不该顺着 body 漏出去。
    signed = build_payload(
        "generic", event="e", title="t", body="b", text="x", secret=SECRET, now=TS
    )
    assert "sign" not in signed and "timestamp" not in signed


# ---- 签名 URL ---------------------------------------------------------------


def test_signed_url_without_secret_unchanged():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    for kind in ("feishu", "dingtalk", "wecom", "generic"):
        assert signed_url(kind, url, None, now=TS) == url


def test_signed_url_feishu_ignores_secret():
    # 飞书签名进 body（见 build_payload）。往 hook 地址后面拼 `&timestamp=`
    # 会让路径变成 `/hook/xxx&timestamp=…`，真机器人直接回 HTTP 404。
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    assert signed_url("feishu", url, SECRET, now=TS) == url


def test_signed_url_dingtalk_appends_query():
    url = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    signed = signed_url("dingtalk", url, SECRET, now=TS)
    assert signed == f"{url}&timestamp={TS * 1000}&sign={DINGTALK_SIGN_QUOTED}"


def test_signed_url_dingtalk_without_existing_query():
    # 走自建反代的钉钉地址可能没有 query，这时分隔符必须是 `?`，
    # 用 `&` 同样拼出一个 404。
    url = "https://dingtalk.example.com/robot/send"
    signed = signed_url("dingtalk", url, SECRET, now=TS)
    assert signed == f"{url}?timestamp={TS * 1000}&sign={DINGTALK_SIGN_QUOTED}"


def test_signed_url_wecom_ignores_secret():
    # 企微机器人没有加签形态；多传了 secret 也不该把它拼进 URL。
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    assert signed_url("wecom", url, SECRET, now=TS) == url
    # generic 同理：密钥是自己的事，不该被拼进别人的地址里再发出去。
    assert signed_url("generic", "https://hook.example.com/notify", SECRET, now=TS) == (
        "https://hook.example.com/notify"
    )


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


# ---- HTTP 错误的可读性 -------------------------------------------------------


def test_http_error_keeps_json_detail():
    resp = httpx.Response(404, json={"code": 94009, "msg": "bad hook id"})
    assert _http_error(resp) == 'HTTP 404：{"code": 94009, "msg": "bad hook id"}'


def test_http_error_without_body_is_bare_code():
    assert _http_error(httpx.Response(500)) == "HTTP 500"


def test_http_error_drops_html():
    # 网关的错误页会把请求 URL 整段印出来（含 access_token / key），
    # 那种正文对排查没用、只有泄漏风险——只留状态码。
    resp = httpx.Response(
        404,
        text="<html>https://oapi.dingtalk.com/robot/send?access_token=abc123</html>",
    )
    assert _http_error(resp) == "HTTP 404"


def test_http_error_redacts_credentials_in_json():
    # 自建接收端也可能回一份带原请求 URL 的 JSON，凭证值一律抹掉。
    resp = httpx.Response(
        400,
        json={"path": "/robot/send?access_token=abc123&sign=xyz", "msg": "rejected"},
    )
    text = _http_error(resp)
    assert "abc123" not in text and "xyz" not in text
    assert "access_token=***" in text and "sign=***" in text
    assert "rejected" in text


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


def test_send_feishu_signed_body_and_clean_url_reach_wire():
    # 真机回报过 HTTP 404：签名一旦被拼到 hook 地址后面，路径就带脏了。
    # URL 原样 + 签名进 body 这两件事必须在同一条用例里钉住。
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.read().decode("utf-8"))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    result = _send(
        _client(handler),
        kind="feishu",
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        secret=SECRET,
    )
    assert result.ok
    assert seen["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
    assert seen["body"]["msg_type"] == "text"
    # timestamp 与 sign 必须同源：只有一边变了就是验签失败，最难排查。
    assert seen["body"]["sign"] == feishu_sign(int(seen["body"]["timestamp"]), SECRET)


def test_send_4xx_keeps_the_channel_detail():
    client = _client(lambda req: httpx.Response(404, json={"code": 94009, "msg": "bad hook"}))
    result = _send(client, kind="feishu")
    assert not result.ok
    # 设置页的报错弹窗与发送记录读的就是这一串：光一个状态码定位不了渠道侧原因。
    assert result.error == 'HTTP 404：{"code": 94009, "msg": "bad hook"}'
