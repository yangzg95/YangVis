"""联网工具：博查网页搜索 + 给定 URL 的网页/PDF 正文读取。

模型自身没有网络，事实截止到训练时刻；搜索补「最新」，抓取补「这个链接里
写了什么」。两个工具都是纯出站 HTTP、无用户上下文、无数据库。

- ``web_search`` 走博查开放平台（api.bochaai.com）。key 为空时返回「未启用」
  提示而不是抛异常——把可操作的结论交回模型（与网盘工具同口径）。
- ``fetch_webpage`` 是服务端代抓，SSRF 是这里的红线：只许 http/https；主机名
  解析出的**每个** IP 都必须是公网地址（环回/私网/链路本地/保留段全拒），
  重定向逐跳重新校验——不能用 httpx 自动跟随，302 到
  ``http://169.254.169.254/`` 会绕过第一跳的检查。
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.services.tools import local_tool

logger = logging.getLogger("yangvis.web_tools")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 手动跟随重定向的最大跳数，每一跳都重过一遍 SSRF 校验。
_MAX_REDIRECTS = 5
# PDF 最多抽取的页数：大文档全文进 prompt 既烧钱又会把有用内容挤出上下文，
# 工具输出边界还有 8000 字符的统一截断兜底。
_MAX_PDF_PAGES = 30

# 博查支持的时间范围过滤；缺省/不合法都按不限时间处理（在函数体里纠正）。
_FRESHNESS_VALUES = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")


# ---- 博查搜索 -----------------------------------------------------------------


@local_tool(title="联网搜索")
async def web_search(query: str, freshness: str = "noLimit") -> str:
    """联网搜索网页，获取最新资讯、近期事件和实时信息的标题、链接与摘要。

    问题涉及新闻、近期事件、最新版本/政策/价格/天气、发布时间晚于你知识截止
    日期的内容，或你不确定的事实时调用。返回的是搜索结果列表（标题、网址、
    摘要）；要读某条结果的完整正文时，拿到链接后再用 fetch_webpage 抓取。
    常识性、与时间无关的问题不要调用。

    Args:
        query: 搜索关键词，简洁明确，不要包含"帮我搜一下"这类客套。
        freshness: 结果时间范围：noLimit（不限，默认）、oneDay（一天内）、
            oneWeek（一周内）、oneMonth（一月内）、oneYear（一年内）。
    """
    keyword = query.strip()
    if not keyword:
        raise ValueError("query 不能为空")
    scope = freshness if freshness in _FRESHNESS_VALUES else "noLimit"

    settings = get_settings()
    if not settings.BOCHA_API_KEY.strip():
        return "联网搜索尚未启用（后端未配置 BOCHA_API_KEY），请告知用户需要管理员在 .env 配置博查 API Key。"

    try:
        async with httpx.AsyncClient(timeout=settings.BOCHA_TIMEOUT) as client:
            resp = await client.post(
                settings.BOCHA_SEARCH_URL,
                headers={"Authorization": f"Bearer {settings.BOCHA_API_KEY}"},
                json={
                    "query": keyword,
                    "summary": True,
                    "count": settings.BOCHA_RESULT_COUNT,
                    "freshness": scope,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("bocha request failed: %s", type(exc).__name__)
        return f"联网搜索失败：网络异常（{type(exc).__name__}），请稍后重试。"

    if resp.status_code != 200:
        logger.warning("bocha http %s", resp.status_code)
        return f"联网搜索失败：搜索服务返回 HTTP {resp.status_code}，请稍后重试或更换关键词。"

    try:
        body = resp.json()
    except ValueError:
        return "联网搜索失败：搜索服务返回了无法解析的响应。"

    # 博查业务错误体：{"code": 401/403/..., "msg": "..."}（成功时 code=200）。
    if body.get("code") not in (None, 200):
        logger.warning("bocha business error code=%s", body.get("code"))
        detail = body.get("msg") or body.get("message") or "搜索服务拒绝了请求"
        return f"联网搜索失败：{detail}。"

    items = (((body.get("data") or {}).get("webPages") or {}).get("value")) or []
    if not items:
        return f"没有搜索到与「{keyword}」相关的网页，可以换个关键词重试。"

    scope_hint = "" if scope == "noLimit" else f"（时间范围：{_FRESHNESS_LABELS[scope]}）"
    lines = [f"联网搜索「{keyword}」{scope_hint}，共 {len(items)} 条结果："]
    for index, item in enumerate(items, 1):
        title = (item.get("name") or "(无标题)").strip()
        url = (item.get("url") or "").strip()
        # summary 是博查对该条结果的长摘要（入参 summary=true 才有），
        # snippet 是页面自带的短摘要，二选一。
        digest = (item.get("summary") or item.get("snippet") or "").strip()
        digest = re.sub(r"\s+", " ", digest)
        date = (item.get("datePublished") or "").strip()
        meta = f"（{date[:10]}）" if date else ""
        lines.append(f"\n{index}. {title}{meta}\n   {url}\n   {digest}".rstrip())
    return "\n".join(lines)


_FRESHNESS_LABELS = {
    "oneDay": "一天内",
    "oneWeek": "一周内",
    "oneMonth": "一月内",
    "oneYear": "一年内",
}


# ---- 网页读取 -----------------------------------------------------------------


@local_tool(title="读取网页内容")
async def fetch_webpage(url: str) -> str:
    """抓取并读取指定公网网址（http/https）的正文，支持网页和 PDF 文件。

    用户给出链接要求总结/阅读/分析页面内容，或 web_search 拿到结果链接后需要
    看全文时调用。只支持公网页面，内网地址、登录后才可见的内容无法读取；
    返回正文纯文本（超长会截断），读不到时返回失败原因，不要编造页面内容。

    Args:
        url: 要读取的完整网址，必须以 http:// 或 https:// 开头。
    """
    target = (url or "").strip()
    if not target:
        raise ValueError("url 不能为空")
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 网址")
    if not parts.hostname:
        raise ValueError("网址缺少主机名")
    if parts.username or parts.password:
        return "网页读取失败：网址里不允许携带用户名密码。"

    settings = get_settings()
    try:
        final_url, content_type, raw = await _download(target, settings.WEB_FETCH_TIMEOUT)
        mime = (content_type.split(";", 1)[0] or "").strip().lower()
        path_tail = urlsplit(final_url).path.lower()
        if mime == "application/pdf" or (not mime and path_tail.endswith(".pdf")):
            return _extract_pdf(final_url, raw)
        if mime in ("text/html", "application/xhtml+xml") or (
            not mime and path_tail.endswith((".html", ".htm", ".xhtml", "/"))
        ):
            return _extract_html(final_url, raw, content_type)
        if mime.startswith("text/") or mime in ("application/json", "application/xml") or (
            mime.endswith(("+json", "+xml"))
        ):
            return _wrap(final_url, "(纯文本)", _decode(raw, content_type).strip())
        return f"网页读取失败：不支持的内容类型 {mime or '未知'}，只支持网页、PDF 与文本。"
    except _WebFetchError as exc:
        return f"网页读取失败：{exc}"


class _WebFetchError(Exception):
    """把代抓过程中各类「读不了」收敛成一句给模型看的话。"""


async def _download(url: str, timeout: float) -> tuple[str, str, bytes]:
    """GET + 手动跟随重定向（逐跳做 SSRF 校验），返回最终 URL、Content-Type、正文。"""
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
    ) as client:
        current = url
        for hop in range(_MAX_REDIRECTS + 1):
            await _assert_public_url(current)
            try:
                # stream：正文按块计数，超过上限立刻停，不先把整份读进内存。
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise _WebFetchError("服务器返回了重定向却没给 Location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise _WebFetchError(f"服务器返回 HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "")
                    limit = get_settings().WEB_FETCH_MAX_BYTES
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise _WebFetchError(
                                f"内容超过 {limit // (1024 * 1024)}MB 上限，已中止抓取"
                            )
                    return str(resp.url), content_type, bytes(chunks)
            except httpx.HTTPError as exc:
                logger.warning("web fetch failed: %s", type(exc).__name__)
                raise _WebFetchError(f"网络异常（{type(exc).__name__}），请稍后重试") from exc
        raise _WebFetchError(f"重定向超过 {_MAX_REDIRECTS} 次，已中止")


async def _assert_public_url(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await _resolve_host(host, port)
    except socket.gaierror as exc:
        raise _WebFetchError(f"域名解析失败：{host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # IPv4-mapped IPv6（::ffff:127.0.0.1）按映射后的 IPv4 判定；
        # ipv4_mapped 属性只存在于 IPv6Address。
        target = ip
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            target = ipaddress.IPv4Address(ip.ipv4_mapped)
        if (
            target.is_private
            or target.is_loopback
            or target.is_link_local
            or target.is_reserved
            or target.is_multicast
            or target.is_unspecified
        ):
            logger.warning("blocked web fetch to non-public address %s (%s)", host, target)
            raise _WebFetchError("只允许抓取公网地址，内网/环回/链路本地地址已拒绝")


async def _resolve_host(host: str, port: int) -> List[str]:
    # 主机名本身就是 IP 字面量时 getaddrinfo 也能处理，但先短路省一次系统调用。
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass

    def _resolve() -> List[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        # getaddrinfo 可能为同一主机返回多条记录（IPv4/IPv6），任何一条是内网
        # 都必须挡住，所以返回全集而不是第一条。
        return list({info[4][0] for info in infos})

    return await asyncio.to_thread(_resolve)


# ---- 正文抽取 -----------------------------------------------------------------


# 抽取时直接丢弃的整枝标签：脚本、样式、导航、页头页脚等不是正文。
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "aside", "iframe"}
)
# 块级标签：在文本里折行，避免相邻元素的文字粘在一起。
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "tr", "table", "ul", "ol", "blockquote", "pre",
        "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "title",
    }
)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.parts: List[str] = []
        self.meta_description = ""
        self._skip_stack: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attributes = {k.lower(): v for k, v in attrs if k}
            if (attributes.get("name") or "").lower() == "description":
                self.meta_description = (attributes.get("content") or "").strip()
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            # 只弹匹配的层；页面标签不配对时宁可继续跳过也不要误放正文进来。
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if self._skip_stack:
            return
        if tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def _extract_html(final_url: str, raw: bytes, content_type: str) -> str:
    text = _decode(raw, content_type)
    parser = _HtmlTextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - 页面 HTML 再烂也该给模型留点东西
        logger.warning("html parse failed for %s: %s", final_url, type(exc).__name__)

    title = " ".join("".join(parser.title_parts).split())
    body_lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    # 折叠连续空行。
    body = "\n".join(line for line in body_lines if line)
    if not body:
        raise _WebFetchError("页面已打开，但没有可提取的正文（可能需要登录或全由脚本渲染）")

    header = f"网页标题：{title or '(无标题)'}\n来源：{final_url}"
    if parser.meta_description and parser.meta_description not in body:
        header += f"\n页面描述：{parser.meta_description}"
    return f"{header}\n\n{body}"


def _extract_pdf(final_url: str, raw: bytes) -> str:
    # pypdf 是已钉版本的直接依赖（简历/知识库文档处理同款）。
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            # 空口令能解开的「标记加密」PDF 不少见，试一次；真加密就如实说。
            result = reader.decrypt("")
            if result == 0:
                raise _WebFetchError("PDF 已加密，无法读取")
        title = ""
        try:
            title = str(reader.metadata.title or "") if reader.metadata else ""
        except Exception:  # noqa: BLE001 - 元数据读不出来不该连累正文
            pass
        pages = reader.pages[:_MAX_PDF_PAGES]
        texts = [(page.extract_text() or "").strip() for page in pages]
    except _WebFetchError:
        raise
    except (PdfReadError, ValueError) as exc:
        raise _WebFetchError(f"PDF 解析失败：{type(exc).__name__}") from exc

    body = "\n\n".join(t for t in texts if t)
    if not body:
        raise _WebFetchError("PDF 已打开，但提取不到文本（可能是扫描件图片）")
    note = f"（共 {len(reader.pages)} 页，仅读取前 {len(pages)} 页）" if len(reader.pages) > len(pages) else ""
    return _wrap(final_url, title or "(PDF 文档)", f"{body}{note}")


def _decode(raw: bytes, content_type: str) -> str:
    """按 Content-Type charset → meta charset → utf-8 → gb18030 的顺序解码。

    国内不少老页面没有 charset 头且是 GBK 编码；httpx 的 .text 只认响应头、
    默认回落 utf-8，会把这类页面解成乱码，所以自己解。gb18030 是 GBK/GB2312
    的超集且 Python 内置，不用引入新依赖。
    """
    declared = _charset_from_content_type(content_type)
    head = raw[:4096]
    match = re.search(
        rb'<meta[^>]+?charset=["\']?\s*([A-Za-z0-9][A-Za-z0-9._\-]*)', head, re.IGNORECASE
    )
    meta_charset = match.group(1).decode("ascii", "ignore").lower() if match else None

    tried: set[str] = set()
    for encoding in (declared, meta_charset, "utf-8", "gb18030"):
        if not encoding or encoding in tried:
            continue
        tried.add(encoding)
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _charset_from_content_type(content_type: str) -> Optional[str]:
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'").lower() or None
    return None


def _wrap(final_url: str, title: str, body: str) -> str:
    return f"标题：{title}\n来源：{final_url}\n\n{body}"
