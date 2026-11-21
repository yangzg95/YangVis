"""创作模块的字数统计。

口径：中日韩统一表意文字一个字算一个，连续的拉丁字母/数字串算一个词，
markdown 标记与标点不计。服务端统计、前端只做同口径展示，避免两边各数各的。
"""
from __future__ import annotations

import re

# 中日韩统一表意文字主区（含常用扩展 A 的口子先不开，生僻字按标点忽略，与主流
# 写作软件的「字数」口径接近）。
_CJK_RE = re.compile(r"[一-鿿]")
# 连续拉丁字母/数字当作一个词。
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")


def count_words(text: str | None) -> int:
    """统计 markdown 正文的字数/词数之和。空文本为 0。"""
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) + len(_LATIN_RE.findall(text))
