"""本地联网工具：web_search（博查）与 fetch_webpage（服务端代抓）。

出站 HTTP 全部走 httpx.MockTransport，不发真请求；DNS 解析直接 monkeypatch
工具自己的 _resolve_host，SSRF 用例因此既快又不依赖本机 hosts。重点钉住：
- 未配置 Key 时返回提示而不是炸掉；博查 HTTP/业务两种错误的落点；
- 抓取的 SSRF 红线：IP 字面量内网地址、重定向跳到内网、DNS 失败；
- 正文抽取：丢脚本、要标题、GBK 老页面能解码、PDF 能取文本、超限中止。
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json as json_lib
import socket

import httpx
import pytest

from app.config import get_settings
from app.services.tools import ToolContext, build_local_tools, specs
from app.services.tools.builtins import web_tools

wait = asyncio.run

# ctx 用不上：两个工具都不吃身份，但传一个确认它们真的不依赖。
CTX = ToolContext(1, 1)


# ---- 公共夹具 -----------------------------------------------------------------


@pytest.fixture()
def bocha(monkeypatch):
    conf = get_settings()
    monkeypatch.setattr(conf, "BOCHA_API_KEY", "test-bocha-key")
    monkeypatch.setattr(conf, "BOCHA_RESULT_COUNT", 8)
    return conf


def install_http(monkeypatch, handler) -> list[httpx.Request]:
    """把出站 HTTP 全部导向 MockTransport，并留一份请求记录。"""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", factory)
    return seen


@pytest.fixture()
def public_dns(monkeypatch):
    """让所有域名解析都落在一个公网示例地址上（IP 字面量不走这里）。"""

    async def fake_resolve(host: str, port: int):
        # 与生产同一口径：IP 字面量直接返回，只把域名指向一个公网示例地址，
        # 否则内网 IP 的短路口径被 mock 吃掉，SSRF 用例就失去意义。
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ["93.184.216.34"]
        return [host]

    monkeypatch.setattr(web_tools, "_resolve_host", fake_resolve)


# ---- 注册形态 -----------------------------------------------------------------


def test_registry_shape():
    by_key = {s.key: s for s in specs()}
    search = by_key["web_search"]
    fetch = by_key["fetch_webpage"]

    assert search.title == "联网搜索" and search.is_async and not search.takes_ctx
    params = {p.name: p for p in search.params}
    assert set(params) == {"query", "freshness"}
    assert params["query"].required and params["freshness"].required is False
    assert params["freshness"].default == "noLimit"

    assert fetch.title == "读取网页内容" and fetch.is_async and not fetch.takes_ctx
    (url_param,) = fetch.params
    assert url_param.name == "url" and url_param.required

    # 装配成 LangChain 工具后名字不被改写。
    tools = build_local_tools(["web_search", "fetch_webpage"], CTX)
    assert {t.name for t in tools} == {"web_search", "fetch_webpage"}


# ---- web_search ---------------------------------------------------------------


def test_search_without_api_key_returns_hint(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOCHA_API_KEY", "")
    out = wait(web_tools.web_search("今天新闻"))
    assert "尚未启用" in out and "BOCHA_API_KEY" in out


def test_search_rejects_blank_query(bocha):
    with pytest.raises(ValueError, match="query"):
        wait(web_tools.web_search("   "))


def test_search_success(bocha, monkeypatch):
    payload = {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "第一条",
                        "url": "https://a.test/1",
                        "summary": "长摘要一",
                        "datePublished": "2026-10-20T08:00:00Z",
                    },
                    {"name": "第二条", "url": "https://a.test/2", "snippet": "短摘要二"},
                ]
            }
        },
    }
    seen = install_http(monkeypatch, lambda req: httpx.Response(200, json=payload))

    out = wait(web_tools.web_search("yangvis 发布"))

    request = seen[0]
    assert request.url == bocha.BOCHA_SEARCH_URL
    assert request.headers["authorization"] == "Bearer test-bocha-key"
    body = json_lib.loads(request.content)
    assert body["query"] == "yangvis 发布" and body["summary"] is True
    assert body["count"] == 8 and body["freshness"] == "noLimit"

    assert "联网搜索「yangvis 发布」" in out
    assert "1. 第一条（2026-10-20）" in out
    assert "https://a.test/1" in out and "长摘要一" in out
    assert "短摘要二" in out


def test_search_invalid_freshness_falls_back_to_no_limit(bocha, monkeypatch):
    seen = install_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}}),
    )
    out = wait(web_tools.web_search("新闻", freshness="lastYear"))
    assert json_lib.loads(seen[0].content)["freshness"] == "noLimit"
    assert "没有搜索到" in out


def test_search_week_scope_is_passed_and_labelled(bocha, monkeypatch):
    seen = install_http(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"webPages": {"value": [{"name": "t", "url": "https://a", "snippet": "s"}]}},
            },
        ),
    )
    out = wait(web_tools.web_search("新闻", freshness="oneWeek"))
    assert json_lib.loads(seen[0].content)["freshness"] == "oneWeek"
    assert "一周内" in out


def test_search_http_error_becomes_message(bocha, monkeypatch):
    install_http(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    assert wait(web_tools.web_search("x")).startswith("联网搜索失败：搜索服务返回 HTTP 500")


def test_search_business_error_becomes_message(bocha, monkeypatch):
    install_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"code": 401, "msg": "API Key 无效"}),
    )
    assert "API Key 无效" in wait(web_tools.web_search("x"))


def test_search_network_error_becomes_message(bocha, monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    install_http(monkeypatch, boom)
    assert "网络异常" in wait(web_tools.web_search("x"))


# ---- fetch_webpage：入参与 SSRF -----------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "ftp://a.test/x", "javascript:alert(1)", "/relative/path"])
def test_fetch_rejects_bad_urls(bad):
    with pytest.raises(ValueError):
        wait(web_tools.fetch_webpage(bad))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://10.0.0.5/secret",
        "http://192.168.1.10/secret",
        "http://169.254.169.254/secret",
        "http://[::1]/secret",
    ],
)
def test_fetch_blocks_private_and_loopback_ips(url, monkeypatch):
    seen = install_http(monkeypatch, lambda req: pytest.fail("内网地址根本不该发请求"))
    out = wait(web_tools.fetch_webpage(url))
    assert "公网地址" in out
    assert seen == []


def test_fetch_ipv4_mapped_ipv6_is_blocked(monkeypatch):
    out = wait(web_tools.fetch_webpage("http://[::ffff:127.0.0.1]/secret"))
    assert "公网地址" in out


def test_fetch_dns_failure_is_reported(monkeypatch):
    async def boom(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(web_tools, "_resolve_host", boom)
    out = wait(web_tools.fetch_webpage("http://missing.test/"))
    assert "域名解析失败" in out


def test_fetch_userinfo_in_url_is_rejected(public_dns, monkeypatch):
    install_http(monkeypatch, lambda req: httpx.Response(200, text="x"))
    out = wait(web_tools.fetch_webpage("http://user:pass@example.test/"))
    assert "用户名密码" in out


def test_fetch_redirect_to_private_network_is_blocked_per_hop(public_dns, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "http://192.168.0.1/secret"})
        return httpx.Response(200, text="should never arrive")

    seen = install_http(monkeypatch, handler)
    out = wait(web_tools.fetch_webpage("http://example.test/go"))
    assert "公网地址" in out
    # 只发出了第一跳请求，第二跳在校验阶段被挡下。
    assert len(seen) == 1 and seen[0].url.path == "/go"


# ---- fetch_webpage：正文抽取 --------------------------------------------------


def _html_response(raw: bytes, content_type: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type}, content=raw)


def test_fetch_extracts_html_title_text_and_drops_scripts(public_dns, monkeypatch):
    page = (
        "<html><head><title>测试标题</title>"
        '<meta name="description" content="页面简介"></head>'
        "<body><script>alert('x')</script>"
        "<nav>导航栏垃圾</nav>"
        "<h1>主标题</h1><p>正文第一段</p><p>正文第二段</p></body></html>"
    ).encode("utf-8")
    install_http(monkeypatch, lambda req: _html_response(page))

    out = wait(web_tools.fetch_webpage("http://example.test/doc"))
    assert "网页标题：测试标题" in out
    assert "页面简介" in out
    assert "主标题" in out and "正文第一段" in out and "正文第二段" in out
    assert "alert" not in out and "导航栏垃圾" not in out


def test_fetch_decodes_gbk_page_without_charset_header(public_dns, monkeypatch):
    page = "<html><head><meta http-equiv=Content-Type content='text/html; charset=gb2312'></head><body><p>中文老页面内容</p></body></html>"
    install_http(monkeypatch, lambda req: _html_response(page.encode("gb18030"), "text/html"))
    out = wait(web_tools.fetch_webpage("http://example.test/gbk"))
    assert "中文老页面内容" in out


def test_fetch_plain_text_and_json(public_dns, monkeypatch):
    install_http(
        monkeypatch,
        lambda req: httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"ok": true}'
        ),
    )
    out = wait(web_tools.fetch_webpage("http://example.test/api"))
    assert "纯文本" in out and '"ok": true' in out


def test_fetch_unsupported_content_type_is_rejected(public_dns, monkeypatch):
    install_http(
        monkeypatch,
        lambda req: httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"),
    )
    out = wait(web_tools.fetch_webpage("http://example.test/a.png"))
    assert "不支持的内容类型" in out


def test_fetch_aborts_when_body_exceeds_size_limit(public_dns, monkeypatch):
    monkeypatch.setattr(get_settings(), "WEB_FETCH_MAX_BYTES", 1024)
    install_http(
        monkeypatch,
        lambda req: _html_response(b"x" * 5000),
    )
    out = wait(web_tools.fetch_webpage("http://example.test/big"))
    assert "上限" in out


def test_fetch_http_error_status_is_reported(public_dns, monkeypatch):
    install_http(monkeypatch, lambda req: httpx.Response(404, text="missing"))
    assert "404" in wait(web_tools.fetch_webpage("http://example.test/nope"))


# ---- PDF ----------------------------------------------------------------------


def _make_pdf(text: str = "Hello PDF world") -> bytes:
    """用 pypdf 现造一份带一条 Tj 文本流的单页 PDF。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode("ascii"))
    stream_ref = writer._add_object(stream)
    # 不挂字体资源时 pypdf 无法确定文本编码，抽出来是乱码，标准字体给一份。
    font_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    resources_ref = writer._add_object(
        DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    )
    page = writer.add_blank_page(width=300, height=144)
    page[NameObject("/Contents")] = stream_ref
    page[NameObject("/Resources")] = resources_ref
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_fetch_extracts_pdf_text(public_dns, monkeypatch):
    pdf = _make_pdf()
    install_http(
        monkeypatch,
        lambda req: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=pdf
        ),
    )
    out = wait(web_tools.fetch_webpage("http://example.test/a.pdf"))
    assert "Hello PDF world" in out and "来源：" in out


def test_fetch_broken_pdf_is_reported(public_dns, monkeypatch):
    install_http(
        monkeypatch,
        lambda req: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"not a pdf at all"
        ),
    )
    assert "PDF" in wait(web_tools.fetch_webpage("http://example.test/a.pdf"))
