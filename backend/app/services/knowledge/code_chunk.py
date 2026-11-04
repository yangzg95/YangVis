"""代码文件的收录判定与结构感知切片。

与散文用的 :mod:`app.services.chunking` 是两套切分器：散文按句读/段落切，
代码必须沿函数、类、方法的语法边界切，并且每个分片要回答得清「我是哪个文件
的哪个符号、第几行到第几行」。P0 不引 tree-sitter（原生构建与 slim 镜像不
友好），用语言族规则 + 行扫描识别结构边界；识别不出来的文件退化为按段落与
行预算切，保证任何文本文件都不会整体入库或被丢弃。

输出分片的 ``content`` 带一行情报头（路径 · 符号 · 行号区间），但
``line_start/line_end`` 永远指向原始代码行、不含情报头。
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("yangvis.codechunk")

# 一个代码分片的预算：代码自描述密度低（缩进/括号/签名），比散文的 700 字
# 宽到 1800；仍远小于整文件，让函数级问题能被单个分片完整回答。
CODE_CHUNK_CHARS = 1800
CODE_CHUNK_LINES = 80
# 超长块二次硬切时带入的重叠行数（只在块内，符号之间不重叠）。
CODE_BLOCK_OVERLAP_LINES = 10
# 压缩/生成文件的单行长度嗅探：源码里几乎不该出现 2000 字符一行的东西。
MAX_SINGLE_LINE = 2000
# 模块前缀（shebang/package/import/模块 docstring）最多带多少行进首片。
PREAMBLE_MAX_LINES = 25

# 扩展名 → 语言标识。切片器按语言族选边界规则，展示也用它。
_EXT_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".pyx": "python",
    ".js": "brace",
    ".jsx": "brace",
    ".mjs": "brace",
    ".cjs": "brace",
    ".ts": "brace",
    ".tsx": "brace",
    ".vue": "declarative",
    ".java": "brace",
    ".kt": "brace",
    ".go": "brace",
    ".rs": "brace",
    ".c": "brace",
    ".h": "brace",
    ".cpp": "brace",
    ".cc": "brace",
    ".cxx": "brace",
    ".hpp": "brace",
    ".hh": "brace",
    ".cs": "brace",
    ".rb": "declarative",
    ".php": "brace",
    ".swift": "brace",
    ".scala": "brace",
    ".sh": "brace",
    ".bash": "brace",
    ".zsh": "brace",
    ".sql": "declarative",
    ".html": "declarative",
    ".htm": "declarative",
    ".css": "brace",
    ".scss": "brace",
    ".less": "brace",
    ".xml": "declarative",
    ".proto": "brace",
    ".gradle": "brace",
    ".groovy": "brace",
    ".tf": "brace",
    ".properties": "declarative",
    ".conf": "declarative",
    ".ini": "declarative",
    ".toml": "declarative",
    ".cfg": "declarative",
    ".dockerfile": "declarative",
}

# 无扩展名但名字固定的文件。
_BASENAME_LANGUAGE = {
    "dockerfile": "declarative",
    "makefile": "declarative",
    "rakefile": "declarative",
    "gemfile": "declarative",
}

# 内置默认排除：路径段（目录名）匹配。
DEFAULT_EXCLUDE_DIR_SEGMENTS = {
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    ".next",
    ".nuxt",
    "coverage",
    "__pycache__",
    ".idea",
    ".vscode",
    ".gradle",
    ".git",
    ".svn",
    ".hg",
    "vendor",
    "bin",
    "obj",
}

# 内置默认排除：glob（匹配仓库相对路径或其 basename）。
DEFAULT_EXCLUDE_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*.map",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "*.pb.go",
    "*_pb2.py",
    "*.generated.*",
)


class SourceSkip(ValueError):
    """这个源文件不该被收录（扩展名、二进制、体积、超长行等）。"""


@dataclass(frozen=True)
class CodeChunk:
    """一个待入库的代码分片。content 已含情报头，行号仍指原始代码。"""

    content: str
    line_start: int
    line_end: int
    symbol: Optional[str]


# ---- 收录判定 ---------------------------------------------------------------


def detect_language(rel_path: str) -> Optional[str]:
    """按相对路径识别语言；不在白名单内返回 None。"""
    lowered = rel_path.lower().replace("\\", "/")
    base = lowered.rsplit("/", 1)[-1]
    if base in _BASENAME_LANGUAGE:
        return _BASENAME_LANGUAGE[base]
    if "." not in base:
        return None
    ext = "." + base.rsplit(".", 1)[-1]
    return _EXT_LANGUAGE.get(ext)


def _match_any(path: str, patterns: Sequence[str]) -> bool:
    basename = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern)
        for pattern in patterns
    )


def parse_user_globs(raw: Optional[str]) -> List[str]:
    """把换行/逗号分隔的用户排除规则解析成 glob 列表。"""
    if not raw:
        return []
    out: List[str] = []
    for piece in re.split(r"[\n,]", raw):
        piece = piece.strip().lstrip("/")
        if piece:
            out.append(piece.replace("\\", "/"))
    return out


def is_excluded(rel_path: str, user_globs: Sequence[str] = ()) -> bool:
    """内置默认排除与用户 glob 的并集。路径段与整串两种匹配都做。"""
    normalized = rel_path.replace("\\", "/")
    segments = normalized.split("/")
    if any(segment in DEFAULT_EXCLUDE_DIR_SEGMENTS for segment in segments[:-1]):
        return True
    if _match_any(normalized, DEFAULT_EXCLUDE_GLOBS):
        return True
    if user_globs and _match_any(normalized, user_globs):
        return True
    return False


def decode_source(raw: bytes) -> str:
    """源码解码：utf-8（含 BOM）优先，gb18030 兜底，再不行替换保内容。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def inspect_source(
    rel_path: str,
    raw: bytes,
    *,
    max_file_bytes: int,
    user_globs: Sequence[str] = (),
) -> Tuple[str, str]:
    """收录前的全部闸门，返回 ``(language, text)``；不通过抛 :class:`SourceSkip`。

    调用方已经用 ``git ls-tree`` 保证了「只看受跟踪文件」，这里的目录/锁文件
    排除是第二道（用户 glob 与内置默认规则）。
    """
    normalized = rel_path.replace("\\", "/")
    if is_excluded(normalized, user_globs):
        raise SourceSkip("命中排除规则")

    language = detect_language(normalized)
    if language is None:
        raise SourceSkip("非受支持的源码扩展名")

    if not raw:
        raise SourceSkip("空文件")
    if b"\x00" in raw[:4096]:
        # NUL 嗅探：几乎没有哪种文本源码会包含 NUL 字节。
        raise SourceSkip("疑似二进制文件")
    if len(raw) > max_file_bytes:
        raise SourceSkip(f"超过单文件上限 {max_file_bytes} 字节")

    text = decode_source(raw)
    # 归一化换行，行号统计才与编辑器一致。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        if len(line) > MAX_SINGLE_LINE:
            raise SourceSkip("存在超长行，疑似压缩或生成文件")
    if not text.strip():
        raise SourceSkip("纯空白文件")
    return language, text


# ---- 切片 -------------------------------------------------------------------

# 各语言族的情报头注释符。
_COMMENT_PREFIX = {
    "python": "#",
    "brace": "//",
    "declarative": "#",
}

# 列 0 定义头：花括号语言族的大杂烩。匹配组 1 是符号名；匹配不到就不是边界。
_BRACE_HEADER = re.compile(
    r"^(?:(?:"
    r"export(?:\s+default)?|public|private|protected|internal|static|final|"
    r"abstract|synchronized|default|native|async|unsafe|pub(?:\([^)]*\))?|"
    r"override|open|sealed|inline|extern|mut|const"
    r")\s+)*"
    r"(?:"
    r"(?:function|func|fn|fun|def)\s*"  # function 系
    r"(?:<[^>{;]+>)?\s*"  # 泛型参数
    r"[A-Za-z_][\w.]*"  # 名字（rust 允许 Receiver::method 之外的普通名）
    r"|"
    r"(?:class|interface|struct|enum|union|namespace|object|trait|type|service|message)\s+"
    r"[A-Za-z_][\w]*"
    r"|"
    # Java/C# 风格：修饰符后「返回类型 方法名(」；用「两个标识符、第二个带括号」识别。
    r"[A-Za-z_][\w<>\[\],?.]*\s+[A-Za-z_]\w*\s*"
    r"(?:<[^{;>]+>)?\s*\([^;{]*\)"
    r"|"
    # Go func 带接收者：func (r *T) Name(
    r"func\s*\([^)]*\)\s*[A-Za-z_]\w*\s*\("
    r"|"
    # HCL/Terraform：resource "type" "name" {
    r'(?:resource|data|provider|variable|output|module|locals)\s+"?[A-Za-z_][\w-]*"?'
    r"|"
    # shell：name() { / function name {
    r"function\s+[A-Za-z_]\w*|[A-Za-z_]\w*\s*\(\s*\)"
    r")"
)
# 从定义头里抠符号名的几种形态（按顺序尝试）。
_BRACE_NAME_PATTERNS = (
    re.compile(r"\b(?:function|func|fn|fun|def)\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:class|interface|struct|enum|union|namespace|object|trait|type|service|message)\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b([A-Za-z_]\w*)\s*(?:<[^{;>]+>)?\s*\([^;{]*\)\s*(?::[^{}]*)?\s*\{?\s*$"),
    re.compile(r'(?:resource|data|provider|variable|output|module)\s+"?([A-Za-z_][\w-]*)"?'),
)
_PYTHON_DEF = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)")
_PYTHON_CLASS = re.compile(r"^class\s+([A-Za-z_]\w*)")
_PYTHON_METHOD = re.compile(r"^[ \t]+(?:async\s+)?def\s+([A-Za-z_]\w*)")


def _make_chunk(
    path: str,
    lines: Sequence[str],
    start: int,
    end: int,
    symbol: Optional[str],
    language: str,
) -> CodeChunk:
    """把 ``lines[start:end]``（1-based 闭区间）组装成带情报头的分片。"""
    # 必须用 "\n".join：split("\n") 得到的行不带换行符，"".join 会把整份代码
    # 压成一行，行号与内容就全毁了。
    body = "\n".join(lines[start - 1 : end]).rstrip()
    location = f"L{start}" if start == end else f"L{start}-L{end}"
    title = symbol or "文件片段"
    prefix = _COMMENT_PREFIX.get(language, "#")
    header = f"{prefix} {path} · {title} ({location})"
    return CodeChunk(
        content=header + "\n" + body,
        line_start=start,
        line_end=end,
        symbol=symbol,
    )


def _hard_split(
    path: str,
    lines: Sequence[str],
    start: int,
    end: int,
    symbol: Optional[str],
    language: str,
) -> List[CodeChunk]:
    """块仍超预算时的兜底：按行预算硬切，带入 10 行重叠。"""
    chunks: List[CodeChunk] = []
    cursor = start
    budget = max(CODE_CHUNK_LINES, 1)
    while cursor <= end:
        piece_end = min(cursor + budget - 1, end)
        # 尽量收在空行上，别在表达式中间断。
        if piece_end < end:
            for look in range(piece_end, max(cursor + budget // 2, cursor) - 1, -1):
                if not lines[look - 1].strip():
                    piece_end = look
                    break
        chunks.append(_make_chunk(path, lines, cursor, piece_end, symbol, language))
        if piece_end >= end:
            break
        cursor = piece_end + 1 - CODE_BLOCK_OVERLAP_LINES
    return chunks


def _emit_block(
    path: str,
    lines: Sequence[str],
    start: int,
    end: int,
    symbol: Optional[str],
    language: str,
) -> List[CodeChunk]:
    """一个候选块的出口：预算内整片，超了硬切。"""
    body = "\n".join(lines[start - 1 : end])
    if end - start + 1 <= CODE_CHUNK_LINES and len(body) <= CODE_CHUNK_CHARS:
        return [_make_chunk(path, lines, start, end, symbol, language)]
    return _hard_split(path, lines, start, end, symbol, language)


def _find_brace_end(lines: Sequence[str], start: int) -> int:
    """从定义头行开始，找花括号深度归 0 的行；找不到就退回定义头所在行。

    注释与字符串做了轻量剥离，字符串里出现 ``{`` 的概率远高于注释，剥不干净
    时最坏结果只是块边界难看，切片仍然完整（硬切兜底），不会丢代码。
    """
    depth = 0
    seen_open = False
    for index in range(start - 1, min(len(lines), start + CODE_CHUNK_LINES * 3)):
        line = lines[index]
        stripped = re.sub(r"//.*$", "", line)
        stripped = re.sub(r"/\*.*?\*/", "", stripped)
        stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
        stripped = re.sub(r"'(?:\\.|[^'\\])*'", "''", stripped)
        for char in stripped:
            if char == "{":
                depth += 1
                seen_open = True
            elif char == "}" and seen_open:
                depth -= 1
                if depth == 0:
                    return index + 1
    return start


def _brace_symbol(header_line: str) -> Optional[str]:
    for pattern in _BRACE_NAME_PATTERNS:
        match = pattern.search(header_line)
        if match:
            return match.group(1)
    return None


def split_brace_family(
    path: str, lines: Sequence[str], language: str
) -> List[CodeChunk]:
    """花括号族：列 0 定义头 + 深度归约找块尾。"""
    chunks: List[CodeChunk] = []
    # 前言：第一个列 0 定义头之前的内容都算（Go 的 package/import、Java 的
    # package 行等），但列 0 的非定义语句（package、import 本身）不收尾——
    # 只在撞上定义头时收尾。前言封顶 PREAMBLE_MAX_LINES，避免无结构文件整段当前言。
    preamble_end = 0
    index = 0
    while index < len(lines) and preamble_end < PREAMBLE_MAX_LINES:
        line = lines[index]
        if (
            line.strip()
            and not line.startswith((" ", "\t"))
            and _BRACE_HEADER.match(line)
        ):
            break
        preamble_end = index + 1
        index += 1
    if preamble_end and "".join(lines[:preamble_end]).strip():
        chunks.append(_make_chunk(path, lines, 1, preamble_end, "文件头", language))

    # cursor 是「尚未被任何分片消费」的第一个 0-based 行下标。结构块之间夹着的
    # 顶层语句（Go 的 import、C 的宏、各语言的类型别名等）在撞上/结束下一个块时
    # 按区间走声明式兜底，保证不静默丢内容。
    cursor = preamble_end
    row = preamble_end
    found_symbol = False
    while row < len(lines):
        line = lines[row]
        # 列 0，且长得像定义头。
        if line.strip() and not line.startswith((" ", "\t")) and _BRACE_HEADER.match(line):
            symbol = _brace_symbol(line)
            block_end = _find_brace_end(lines, row + 1)
            if block_end <= row:
                # 头里没有开花括号（老式 K&R 之外的签名换行等）：先不消费这行，
                # 让它落进后面的区间兜底。
                row += 1
                continue
            if row > cursor:
                chunks.extend(
                    split_declarative(
                        path, lines, language, lo=cursor + 1, hi=row
                    )
                )
            chunks.extend(_emit_block(path, lines, row + 1, block_end, symbol, language))
            found_symbol = True
            row = block_end
            cursor = block_end
        else:
            row += 1

    # 文件尾部还没被消费的行（通常没有，但函数后再放顶层语句是合法的）。
    if cursor < len(lines):
        chunks.extend(
            split_declarative(path, lines, language, lo=cursor + 1, hi=len(lines))
        )

    if not found_symbol:
        # 一个结构都没识别出来：别只留文件头，整文件走声明式段落切。
        return split_declarative(path, lines, language)
    return chunks


def split_python(path: str, lines: Sequence[str]) -> List[CodeChunk]:
    """Python：列 0 的 def/class 是顶层块；过大的类按方法再切。"""
    language = "python"
    # 找顶层定义的起始行（含贴在它上面的装饰器）。
    tops: List[Tuple[int, str, Optional[str]]] = []  # (1-based start, kind, symbol)
    row = 0
    while row < len(lines):
        line = lines[row]
        if line.startswith("def ") or line.startswith("async def ") or line.startswith("class "):
            start = row + 1
            decorator = row - 1
            while decorator >= 0 and lines[decorator].startswith("@"):
                start = decorator + 1
                decorator -= 1
            match = _PYTHON_CLASS.match(line) or _PYTHON_DEF.match(line)
            tops.append((start, "class" if _PYTHON_CLASS.match(line) else "def", match.group(1) if match else None))
        row += 1

    chunks: List[CodeChunk] = []
    # 文件头：第一个顶层定义之前的内容（shebang/模块 docstring/import）。
    first_start = tops[0][0] if tops else len(lines) + 1
    preamble_end = min(first_start - 1, PREAMBLE_MAX_LINES)
    if preamble_end > 0 and "".join(lines[:preamble_end]).strip():
        chunks.append(_make_chunk(path, lines, 1, preamble_end, "文件头", language))
    # 前言被 25 行封顶后，到第一个顶层定义之间可能还有模块级代码（长 import 块、
    # 模块级常量），不能静默丢掉：走声明式兜底，行号仍对得上原文件。
    if first_start - 1 > preamble_end:
        gap = split_declarative(
            path, lines, language, lo=preamble_end + 1, hi=first_start - 1
        )
        chunks.extend(gap)

    for position, (start, kind, symbol) in enumerate(tops):
        next_start = tops[position + 1][0] if position + 1 < len(tops) else len(lines) + 1
        end = next_start - 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        body = "".join(lines[start - 1 : end])
        if kind == "class" and (
            end - start + 1 > CODE_CHUNK_LINES or len(body) > CODE_CHUNK_CHARS
        ):
            chunks.extend(_split_python_class(path, lines, start, end, symbol))
        else:
            chunks.extend(_emit_block(path, lines, start, end, symbol, language))

    if not tops:
        return split_declarative(path, lines, language)
    return chunks


def _split_python_class(
    path: str,
    lines: Sequence[str],
    start: int,
    end: int,
    class_name: Optional[str],
) -> List[CodeChunk]:
    """过大的类：类头单独一片，方法各成一片；方法间的字段归入类头片。"""
    language = "python"
    out: List[CodeChunk] = []
    # 类头：class 行（含装饰器已在 start）到第一个方法之前。
    methods: List[Tuple[int, Optional[str]]] = []
    for row in range(start, end + 1):
        match = _PYTHON_METHOD.match(lines[row - 1])
        if match:
            method_start = row
            decorator = row - 2
            while decorator >= start - 1 and lines[decorator].lstrip().startswith("@"):
                method_start = decorator + 1
                decorator -= 1
            methods.append((method_start, f"{class_name}.{match.group(1)}" if class_name else match.group(1)))

    header_end = (methods[0][0] - 1) if methods else end
    if header_end >= start:
        out.append(_make_chunk(path, lines, start, header_end, class_name, language))
    for position, (method_start, symbol) in enumerate(methods):
        method_end = methods[position + 1][0] - 1 if position + 1 < len(methods) else end
        while method_end > method_start and not lines[method_end - 1].strip():
            method_end -= 1
        out.extend(_emit_block(path, lines, method_start, method_end, symbol, language))
    return out


def split_declarative(
    path: str,
    lines: Sequence[str],
    language: str,
    *,
    lo: int = 1,
    hi: Optional[int] = None,
) -> List[CodeChunk]:
    """声明式/兜底族：按空行聚段，再按行预算与字符预算切，symbol 留空。

    ``lo``/``hi`` 是相对整份文件的 1-based 绝对行区间（供别的切分器把「结构
    之间没人认领的行区间」兜底塞进来），默认覆盖整个文件。
    """
    end_limit = len(lines) if hi is None else hi
    chunks: List[CodeChunk] = []
    paragraph_start = lo
    row = lo

    def flush(end: int) -> None:
        nonlocal paragraph_start
        if end < paragraph_start:
            return
        body = "\n".join(lines[paragraph_start - 1 : end])
        if not body.strip():
            paragraph_start = end + 1
            return
        if end - paragraph_start + 1 <= CODE_CHUNK_LINES and len(body) <= CODE_CHUNK_CHARS:
            chunks.append(_make_chunk(path, lines, paragraph_start, end, None, language))
        else:
            chunks.extend(
                _hard_split(path, lines, paragraph_start, end, None, language)
            )
        paragraph_start = end + 1

    while row <= end_limit:
        if not lines[row - 1].strip():
            flush(row - 1)
            # 跳过空行本身。
            while row <= end_limit and not lines[row - 1].strip():
                row += 1
            paragraph_start = row
            continue
        if row - paragraph_start + 1 >= CODE_CHUNK_LINES:
            flush(row)
        row += 1
    flush(end_limit)
    return chunks


def split_code(text: str, language: str, rel_path: str) -> List[CodeChunk]:
    """把一份已通过收录判定的源码切成带行号与符号的分片。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # split("\n") 会给结尾换行多造一个空元素；保留它无害（行号按它算会多一空行），
    # 但尾片的 line_end 会虚高一，去掉它，结尾换行在拼回时不影响内容。
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return []

    path = rel_path.replace("\\", "/")
    if language == "python":
        chunks = split_python(path, lines)
    elif language == "brace":
        chunks = split_brace_family(path, lines, language)
    else:
        chunks = split_declarative(path, lines, language)

    # 任何分支都不允许产出空正文。
    return [chunk for chunk in chunks if chunk.content.strip()]
