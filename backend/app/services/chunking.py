"""文本抽取与切分。

这里自己做切分而不用 ``langchain-text-splitters``，是因为对中文文档真正
要紧的规则就那么几条、而且很具体：除 ASCII 之外还要按中日韩句读切分，
以及按字符数而不是 token 数衡量大小（对在用的这些模型来说，一个 CJK
字符大致就是一个 token，为了切个片就把每篇文档都 tokenise 一遍，不值得
为此多引入一个依赖）。
"""
from __future__ import annotations

import logging
import re
from typing import List

logger = logging.getLogger("yangvis.chunking")

# 目标大小按字符计。700 个 CJK 字符大约落在 700-800 token，既能舒服地
# 待在每个 embedding 模型的窗口之内，又还带着足够的上下文，让单个 chunk
# 自己就能回答一个问题。
CHUNK_SIZE = 700
CHUNK_OVERLAP = 100

# 只收那些不需要额外解析依赖就能读的格式。在这里放行一个 PDF、却在后台
# 任务里才失败，等于用户早就离开页面之后才把错误报出来。
SUPPORTED_EXTENSIONS = (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".yaml", ".yml")

# 先按段落断开，再按句末（中日韩与 ASCII），最后才是任意空白。越靠前的
# 分隔符切出来的 chunk 越完整连贯。
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", " ")


class UnsupportedFileType(ValueError):
    """上传的文件不是当前这个版本能读的类型。"""


def extract_text(filename: str, raw: bytes) -> str:
    """把上传的文件解码成纯文本。

    对任何需要解析器的格式抛出 :class:`UnsupportedFileType`。
    """
    lowered = filename.lower()
    if not lowered.endswith(SUPPORTED_EXTENSIONS):
        raise UnsupportedFileType(
            f"暂不支持该格式，目前仅支持 {'/'.join(SUPPORTED_EXTENSIONS)}"
        )

    for encoding in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        # 最后的兜底：与其把这次上传直接打回，不如尽量保住能读出来的部分。
        text = raw.decode("utf-8", errors="replace")

    return normalise(text)


def normalise(text: str) -> str:
    """把那些白白占用 chunk 额度的噪声压掉。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_once(text: str, separator: str) -> List[str]:
    """切分时把分隔符留在前一段的末尾。"""
    if separator == " ":
        return text.split(" ")
    parts = text.split(separator)
    out = [part + separator for part in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _explode(text: str, limit: int, separators: tuple[str, ...]) -> List[str]:
    """把文本拆成长度都不超过 ``limit`` 个字符的片段。"""
    if len(text) <= limit:
        return [text] if text else []

    for index, separator in enumerate(separators):
        if separator not in text:
            continue
        pieces: List[str] = []
        for piece in _split_once(text, separator):
            if len(piece) <= limit:
                if piece:
                    pieces.append(piece)
            else:
                pieces.extend(_explode(piece, limit, separators[index + 1 :]))
        if pieces:
            return pieces

    # 已经没有分隔符可用了：这是一堵文字墙（压缩过的 JSON、一长串中文）。
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def split_text(
    text: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> List[str]:
    """在自然边界上把文本切成相互重叠的若干 chunk。

    之所以要重叠，是为了让横跨边界的句子仍然检索得到：没有重叠的话，
    某个问题的答案可能正好被切在两个 chunk 里，结果两边的得分都不够高，
    谁都没被召回。
    """
    text = normalise(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    overlap = max(0, min(overlap, chunk_size // 2))
    pieces = _explode(text, chunk_size, _SEPARATORS)

    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if len(current) + len(piece) <= chunk_size:
            current += piece
            continue

        if current:
            chunks.append(current.strip())
            # 把尾巴带到下一段去，免得边界变成一刀切死。
            current = current[-overlap:] if overlap else ""
        current += piece

    if current.strip():
        chunks.append(current.strip())

    return [chunk for chunk in chunks if chunk]
