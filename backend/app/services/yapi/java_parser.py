"""轻量 Java 源码解析：从 Spring Controller 里认出包、类、方法和注解。

只解析与接口文档有关的语法，不追求完整 Java 语法树。不引入 ``javalang``
这类第三方解析器，是为了不把版本兼容押在一个多年不更新的库上：JDK 17 的
record、sealed、注解数组参数，它都认不全。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MODIFIERS = {
    "public",
    "protected",
    "private",
    "static",
    "final",
    "abstract",
    "synchronized",
    "native",
    "strictfp",
    "default",
    "transient",
    "volatile",
    "sealed",
}

TYPE_KEYWORDS = {"class", "interface", "enum", "record"}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    offset: int


@dataclass
class Annotation:
    """注解只保留名字和参数：``@GetMapping("/x")`` → name=GetMapping, values={value: "/x"}。"""

    name: str
    values: Dict[str, object] = field(default_factory=dict)


@dataclass
class Javadoc:
    text: str = ""
    params: Dict[str, str] = field(default_factory=dict)
    returns: str = ""


@dataclass
class JavaParam:
    name: str
    type_text: str
    annotations: List[Annotation] = field(default_factory=list)


@dataclass
class JavaField:
    name: str
    type_text: str
    annotations: List[Annotation] = field(default_factory=list)
    javadoc: Javadoc = field(default_factory=Javadoc)
    modifiers: List[str] = field(default_factory=list)
    record_component: bool = False


@dataclass
class JavaMethod:
    name: str
    return_type: str
    params: List[JavaParam] = field(default_factory=list)
    annotations: List[Annotation] = field(default_factory=list)
    javadoc: Javadoc = field(default_factory=Javadoc)
    modifiers: List[str] = field(default_factory=list)
    line: int = 0


@dataclass
class JavaType:
    name: str
    fqn: str
    kind: str
    package: str
    imports: Dict[str, str]
    wildcard_imports: List[str]
    annotations: List[Annotation] = field(default_factory=list)
    javadoc: Javadoc = field(default_factory=Javadoc)
    fields: List[JavaField] = field(default_factory=list)
    methods: List[JavaMethod] = field(default_factory=list)
    type_params: List[str] = field(default_factory=list)
    extends: List[str] = field(default_factory=list)
    implements: List[str] = field(default_factory=list)
    enum_values: List[str] = field(default_factory=list)
    modifiers: List[str] = field(default_factory=list)
    line: int = 0
    source: str = ""


@dataclass
class JavaUnit:
    package: str
    imports: Dict[str, str]
    wildcard_imports: List[str]
    types: List[JavaType]


# ---- 词法 -------------------------------------------------------------------

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "'": "'",
    "\\": "\\",
}


def _unescape(raw: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\" or i + 1 >= len(raw):
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "u" and i + 5 < len(raw):
            try:
                out.append(chr(int(raw[i + 2 : i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append(_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _is_id_start(ch: str) -> bool:
    return ch == "_" or ch == "$" or ch.isalpha()


def _is_id_part(ch: str) -> bool:
    return ch in "_$" or ch.isalnum()


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    line = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            if ch == "\n":
                line += 1
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            start = i
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            tokens.append(Token("comment", text[start:i], line, start))
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            start = i
            is_javadoc = i + 2 < n and text[i + 2] == "*"
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    line += 1
                i += 1
            if i + 1 < n:
                i += 2
            body = text[start + 3 if is_javadoc else start + 2 : max(start + 2, i - 2)]
            tokens.append(Token("javadoc" if is_javadoc else "comment", body, line, start))
            continue
        if ch == '"':
            start = i
            i += 1
            chars: List[str] = []
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    chars.append(text[i : i + 2])
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                if text[i] == "\n":
                    line += 1
                chars.append(text[i])
                i += 1
            tokens.append(Token("str", _unescape("".join(chars)), line, start))
            continue
        if ch == "'":
            start = i
            i += 1
            chars = []
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    chars.append(text[i : i + 2])
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                chars.append(text[i])
                i += 1
            tokens.append(Token("char", _unescape("".join(chars)), line, start))
            continue
        if ch.isdigit():
            start = i
            while i < n and (text[i].isalnum() or text[i] in "._"):
                i += 1
            tokens.append(Token("num", text[start:i], line, start))
            continue
        if _is_id_start(ch):
            start = i
            while i < n and _is_id_part(text[i]):
                i += 1
            tokens.append(Token("id", text[start:i], line, start))
            continue
        if text.startswith("...", i):
            tokens.append(Token("sym", "...", line, i))
            i += 3
            continue
        tokens.append(Token("sym", ch, line, i))
        i += 1
    return tokens


# ---- 通用小工具 ---------------------------------------------------------------


def _without_comments(tokens: Sequence[Token]) -> List[Token]:
    return [token for token in tokens if token.kind not in ("comment", "javadoc")]


def _render(tokens: Sequence[Token]) -> str:
    parts: List[str] = []
    for token in tokens:
        if token.kind in ("comment", "javadoc"):
            continue
        if token.kind == "str":
            parts.append(f'"{token.value}"')
        else:
            parts.append(token.value)
    return "".join(parts)


def _matching(tokens: Sequence[Token], start: int, open_char: str, close_char: str) -> int:
    depth = 0
    for i in range(start, len(tokens)):
        token = tokens[i]
        if token.kind in ("comment", "javadoc"):
            continue
        if token.value == open_char:
            depth += 1
        elif token.value == close_char:
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def _split_top_level(tokens: Sequence[Token], sep: str = ",") -> List[List[Token]]:
    parts: List[List[Token]] = []
    current: List[Token] = []
    depth = 0
    for token in tokens:
        if token.kind in ("comment", "javadoc"):
            current.append(token)
            continue
        if token.value in ("(", "{", "[", "<"):
            depth += 1
            current.append(token)
            continue
        if token.value in (")", "}", "]", ">"):
            depth = max(0, depth - 1)
            current.append(token)
            continue
        if token.value == sep and depth == 0:
            parts.append(current)
            current = []
            continue
        current.append(token)
    if current or not parts:
        parts.append(current)
    return parts


def _parse_annotation(tokens: Sequence[Token], start: int) -> Tuple[Optional[Annotation], int]:
    """``start`` 指向 ``@``。返回 (注解, 下一个位置)；``@interface`` 返回 None。"""
    i = start + 1
    if i >= len(tokens) or tokens[i].kind != "id":
        return None, start + 1
    if tokens[i].value == "interface":
        return None, i + 1
    names = [tokens[i].value]
    i += 1
    while i + 1 < len(tokens) and tokens[i].value == "." and tokens[i + 1].kind == "id":
        names.append(tokens[i + 1].value)
        i += 2
    values: Dict[str, object] = {}
    if i < len(tokens) and tokens[i].value == "(":
        close = _matching(tokens, i, "(", ")")
        values = _parse_annotation_values(tokens[i + 1 : close])
        i = close + 1
    return Annotation(name=names[-1], values=values), i


def _parse_annotation_values(tokens: Sequence[Token]) -> Dict[str, object]:
    values: Dict[str, object] = {}
    for part in _split_top_level(tokens):
        part = _without_comments(part)
        if not part:
            continue
        eq = _top_level_eq(part)
        if eq == 1 and part[0].kind == "id":
            key = part[0].value
            value = _annotation_value(part[2:])
        else:
            key = "value"
            value = _annotation_value(part)
        if key in values:
            previous = values[key]
            values[key] = previous + [value] if isinstance(previous, list) else [previous, value]
        else:
            values[key] = value
    return values


def _top_level_eq(tokens: Sequence[Token]) -> int:
    depth = 0
    for i, token in enumerate(tokens):
        if token.value in ("(", "{", "[", "<"):
            depth += 1
        elif token.value in (")", "}", "]", ">"):
            depth = max(0, depth - 1)
        elif token.value == "=" and depth == 0:
            return i
    return -1


def _annotation_value(tokens: Sequence[Token]) -> object:
    tokens = _without_comments(tokens)
    if not tokens:
        return ""
    if tokens[0].value == "{" and tokens[-1].value == "}":
        return [_annotation_value(part) for part in _split_top_level(tokens[1:-1]) if _without_comments(part)]
    if len(tokens) == 1:
        token = tokens[0]
        if token.kind in ("str", "char"):
            return token.value
        if token.value == "true":
            return True
        if token.value == "false":
            return False
        if token.kind == "num":
            try:
                return int(token.value)
            except ValueError:
                try:
                    return float(token.value)
                except ValueError:
                    return token.value
        return token.value
    return _render(tokens)


def annotation(annotations: Iterable[Annotation], name: str) -> Optional[Annotation]:
    for item in annotations:
        if item.name == name:
            return item
    return None


def annotation_text(item: Optional[Annotation], *keys: str) -> str:
    if item is None:
        return ""
    for key in keys:
        value = item.values.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                return first
    return ""


def annotation_values(item: Optional[Annotation], *keys: str) -> List[object]:
    if item is None:
        return []
    for key in keys:
        value = item.values.get(key)
        if isinstance(value, list):
            return list(value)
        if value is not None and value != "":
            return [value]
    return []


def annotation_bool(item: Optional[Annotation], key: str, default: bool = False) -> bool:
    if item is None or key not in item.values:
        return default
    value = item.values[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return default


# ---- Javadoc ----------------------------------------------------------------


def parse_javadoc(raw: str) -> Javadoc:
    lines: List[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].lstrip()
        lines.append(line)

    main: List[str] = []
    params: Dict[str, str] = {}
    returns = ""
    tag = ""
    param_name = ""
    for line in lines:
        match = re.match(r"@(\w+)\s*(.*)", line)
        if match:
            tag = match.group(1)
            rest = match.group(2).strip()
            param_name = ""
            if tag == "param":
                param_match = re.match(r"(\S+)\s*(.*)", rest)
                if param_match and not param_match.group(1).startswith("<"):
                    param_name = param_match.group(1)
                    params[param_name] = param_match.group(2).strip()
            elif tag == "return":
                returns = rest
            continue
        if not line:
            if not tag:
                main.append("")
            continue
        if param_name:
            params[param_name] = (params[param_name] + " " + line).strip()
        elif tag == "return":
            returns = (returns + " " + line).strip()
        elif not tag:
            main.append(line)
    return Javadoc(text="\n".join(main).strip(), params=params, returns=returns)


# ---- 语法 -------------------------------------------------------------------


class JavaParser:
    def __init__(self, tokens: List[Token], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.package = ""
        self.imports: Dict[str, str] = {}
        self.wildcard_imports: List[str] = []
        self.types: List[JavaType] = []

    def parse(self) -> JavaUnit:
        self._parse_header()
        self._parse_top_level()
        return JavaUnit(
            package=self.package,
            imports=self.imports,
            wildcard_imports=self.wildcard_imports,
            types=self.types,
        )

    # -- 文件头 ---------------------------------------------------------------

    def _parse_header(self) -> None:
        i = 0
        while i < len(self.tokens):
            token = self.tokens[i]
            if token.kind in ("comment", "javadoc"):
                i += 1
                continue
            if token.kind == "id" and token.value == "package":
                name, i = self._dotted_until_semicolon(i + 1)
                self.package = name
                continue
            if token.kind == "id" and token.value == "import":
                i += 1
                if i < len(self.tokens) and self.tokens[i].value == "static":
                    i += 1
                name, i = self._dotted_until_semicolon(i)
                if name.endswith(".*"):
                    self.wildcard_imports.append(name[:-2])
                elif name:
                    self.imports[name.rsplit(".", 1)[-1]] = name
                continue
            break

    def _dotted_until_semicolon(self, start: int) -> Tuple[str, int]:
        parts: List[str] = []
        i = start
        while i < len(self.tokens):
            token = self.tokens[i]
            if token.value == ";":
                return "".join(parts), i + 1
            if token.kind in ("id", "sym") and token.value not in ("comment",):
                parts.append(token.value)
            i += 1
        return "".join(parts), i

    # -- 类型 -----------------------------------------------------------------

    def _parse_top_level(self) -> None:
        i = 0
        annotations: List[Annotation] = []
        modifiers: List[str] = []
        javadoc = Javadoc()
        while i < len(self.tokens):
            token = self.tokens[i]
            if token.kind in ("comment", "javadoc"):
                if token.kind == "javadoc":
                    javadoc = parse_javadoc(token.value)
                i += 1
                continue
            if token.value == ";":
                annotations, modifiers, javadoc = [], [], Javadoc()
                i += 1
                continue
            if token.value == "@":
                item, next_i = _parse_annotation(self.tokens, i)
                if item is None:
                    i = self._skip_balanced_body(next_i)
                    annotations, modifiers, javadoc = [], [], Javadoc()
                    continue
                annotations.append(item)
                i = next_i
                continue
            if token.kind == "id" and token.value in MODIFIERS:
                modifiers.append(token.value)
                i += 1
                continue
            if token.kind == "id" and token.value in TYPE_KEYWORDS:
                i = self._parse_type(i, annotations, modifiers, javadoc, None)
                annotations, modifiers, javadoc = [], [], Javadoc()
                continue
            i += 1

    def _skip_balanced_body(self, start: int) -> int:
        i = start
        while i < len(self.tokens) and self.tokens[i].value != "{":
            i += 1
        if i >= len(self.tokens):
            return len(self.tokens)
        return _matching(self.tokens, i, "{", "}") + 1

    def _parse_type(
        self,
        keyword_index: int,
        annotations: List[Annotation],
        modifiers: List[str],
        javadoc: Javadoc,
        owner_fqn: Optional[str],
    ) -> int:
        keyword = self.tokens[keyword_index].value
        line = self.tokens[keyword_index].line
        i = keyword_index + 1
        if i >= len(self.tokens) or self.tokens[i].kind != "id":
            return i
        name = self.tokens[i].value
        i += 1

        type_params: List[str] = []
        if i < len(self.tokens) and self.tokens[i].value == "<":
            close = _matching(self.tokens, i, "<", ">")
            type_params = self._parse_type_param_names(self.tokens[i + 1 : close])
            i = close + 1

        record_fields: List[JavaField] = []
        if keyword == "record" and i < len(self.tokens) and self.tokens[i].value == "(":
            close = _matching(self.tokens, i, "(", ")")
            for param in self._parse_params(self.tokens[i + 1 : close]):
                record_fields.append(
                    JavaField(
                        name=param.name,
                        type_text=param.type_text,
                        annotations=param.annotations,
                        record_component=True,
                    )
                )
            i = close + 1

        while i < len(self.tokens) and self.tokens[i].value not in ("{", ";"):
            i += 1
        if i >= len(self.tokens) or self.tokens[i].value == ";":
            return i + 1

        body_start = i
        body_end = _matching(self.tokens, body_start, "{", "}")

        if owner_fqn:
            fqn = f"{owner_fqn}.{name}"
        elif self.package:
            fqn = f"{self.package}.{name}"
        else:
            fqn = name

        type_info = JavaType(
            name=name,
            fqn=fqn,
            kind=keyword,
            package=self.package,
            imports=dict(self.imports),
            wildcard_imports=list(self.wildcard_imports),
            annotations=list(annotations),
            javadoc=javadoc,
            fields=record_fields,
            type_params=type_params,
            modifiers=list(modifiers),
            line=line,
            source=self.source,
        )
        self._parse_inheritance(type_info, keyword_index, body_start)
        self.types.append(type_info)

        member_start = body_start + 1
        if keyword == "enum":
            type_info.enum_values, member_start = self._parse_enum_constants(member_start, body_end)
        self._parse_members(member_start, body_end, type_info)
        return body_end + 1

    def _parse_inheritance(self, type_info: JavaType, keyword_index: int, body_start: int) -> None:
        header = list(self.tokens[keyword_index:body_start])
        extends_at = -1
        implements_at = -1
        depth = 0
        for i, token in enumerate(header):
            if token.kind in ("comment", "javadoc"):
                continue
            if token.value in ("<", "("):
                depth += 1
                continue
            if token.value in (">", ")"):
                depth = max(0, depth - 1)
                continue
            if depth or token.kind != "id":
                continue
            if token.value == "extends" and extends_at < 0:
                extends_at = i
            elif token.value == "implements" and implements_at < 0:
                implements_at = i

        def names(start: int, end: int) -> List[str]:
            result: List[str] = []
            for part in _split_top_level(header[start:end]):
                text = _render(part).strip()
                if text:
                    result.append(text)
            return result

        if extends_at >= 0:
            end = implements_at if implements_at > extends_at else len(header)
            type_info.extends = names(extends_at + 1, end)
        if implements_at >= 0:
            type_info.implements = names(implements_at + 1, len(header))

    def _parse_type_param_names(self, tokens: Sequence[Token]) -> List[str]:
        names: List[str] = []
        for part in _split_top_level(tokens):
            for token in _without_comments(part):
                if token.kind == "id" and token.value not in MODIFIERS:
                    names.append(token.value)
                    break
        return names

    def _parse_enum_constants(self, start: int, end: int) -> Tuple[List[str], int]:
        values: List[str] = []
        segment_start = start
        depth = 0
        i = start

        def flush(part_start: int, part_end: int) -> None:
            j = part_start
            while j < part_end:
                token = self.tokens[j]
                if token.kind in ("comment", "javadoc"):
                    j += 1
                    continue
                if token.value == "@":
                    item, next_j = _parse_annotation(self.tokens, j)
                    j = next_j if item is not None else j + 1
                    continue
                if token.kind == "id":
                    values.append(token.value)
                    return
                return

        while i < end:
            token = self.tokens[i]
            if token.value in ("(", "{", "["):
                depth += 1
            elif token.value in (")", "}", "]"):
                depth = max(0, depth - 1)
            elif depth == 0 and token.value == ",":
                flush(segment_start, i)
                segment_start = i + 1
            elif depth == 0 and token.value == ";":
                flush(segment_start, i)
                return values, i + 1
            i += 1
        flush(segment_start, end)
        return values, end

    # -- 成员 -----------------------------------------------------------------

    def _parse_members(self, start: int, end: int, owner: JavaType) -> None:
        i = start
        while i < end:
            annotations: List[Annotation] = []
            modifiers: List[str] = []
            javadoc = Javadoc()

            while i < end:
                token = self.tokens[i]
                if token.kind in ("comment", "javadoc"):
                    if token.kind == "javadoc":
                        javadoc = parse_javadoc(token.value)
                    i += 1
                    continue
                if token.value == "@":
                    item, next_i = _parse_annotation(self.tokens, i)
                    if item is None:
                        i = self._skip_balanced_body(next_i)
                        annotations, modifiers, javadoc = [], [], Javadoc()
                        break
                    annotations.append(item)
                    i = next_i
                    continue
                if token.kind == "id" and token.value in MODIFIERS:
                    modifiers.append(token.value)
                    i += 1
                    continue
                break
            if i >= end:
                break

            token = self.tokens[i]
            if token.value == ";":
                i += 1
                continue
            if token.kind == "id" and token.value in TYPE_KEYWORDS:
                i = self._parse_type(i, annotations, modifiers, javadoc, owner.fqn)
                continue
            if token.value == "{":
                i = _matching(self.tokens, i, "{", "}") + 1
                continue

            declaration_start = i
            delimiter = self._member_delimiter(declaration_start, end)
            if delimiter >= end:
                break

            if self.tokens[delimiter].value == "(":
                close = _matching(self.tokens, delimiter, "(", ")")
                method = self._parse_method(
                    declaration_start, delimiter, close, annotations, javadoc, modifiers
                )
                if method is not None:
                    owner.methods.append(method)
                after = close + 1
                while after < end and self.tokens[after].value not in ("{", ";"):
                    after += 1
                if after < end and self.tokens[after].value == "{":
                    i = _matching(self.tokens, after, "{", "}") + 1
                else:
                    i = after + 1
                continue

            if self.tokens[delimiter].value in ("=", ";"):
                if self.tokens[delimiter].value == "=":
                    statement_end = self._statement_end(delimiter + 1, end)
                    i = statement_end + 1
                else:
                    i = delimiter + 1
                fields = self._parse_fields(
                    self.tokens[declaration_start:delimiter], annotations, javadoc, modifiers
                )
                owner.fields.extend(fields)
                continue

            i = delimiter + 1

    def _member_delimiter(self, start: int, end: int) -> int:
        angle = 0
        square = 0
        for i in range(start, end):
            token = self.tokens[i]
            value = token.value
            if token.kind in ("comment", "javadoc"):
                continue
            if value == "<":
                angle += 1
            elif value == ">" and angle > 0:
                angle -= 1
            elif value == "[":
                square += 1
            elif value == "]" and square > 0:
                square -= 1
            elif angle == 0 and square == 0 and value in ("(", "=", ";", "{"):
                return i
        return end

    def _statement_end(self, start: int, end: int) -> int:
        depth = 0
        for i in range(start, end):
            value = self.tokens[i].value
            if value in ("(", "{", "["):
                depth += 1
            elif value in (")", "}", "]"):
                depth = max(0, depth - 1)
            elif value == ";" and depth == 0:
                return i
        return end

    def _parse_method(
        self,
        start: int,
        paren: int,
        close: int,
        annotations: List[Annotation],
        javadoc: Javadoc,
        modifiers: List[str],
    ) -> Optional[JavaMethod]:
        head = _without_comments(self.tokens[start:paren])
        if not head:
            return None
        name_index = -1
        for i in range(len(head) - 1, -1, -1):
            if head[i].kind == "id":
                name_index = i
                break
        if name_index < 0:
            return None
        name = head[name_index].value

        return_tokens = head[:name_index]
        if return_tokens and return_tokens[0].value == "<":
            depth = 0
            for i, token in enumerate(return_tokens):
                if token.value == "<":
                    depth += 1
                elif token.value == ">":
                    depth -= 1
                    if depth == 0:
                        return_tokens = return_tokens[i + 1 :]
                        break
        return_type = _render(return_tokens)

        return JavaMethod(
            name=name,
            return_type=return_type,
            params=self._parse_params(self.tokens[paren + 1 : close]),
            annotations=list(annotations),
            javadoc=javadoc,
            modifiers=list(modifiers),
            line=self.tokens[paren].line,
        )

    def _parse_params(self, tokens: Sequence[Token]) -> List[JavaParam]:
        params: List[JavaParam] = []
        for part in _split_top_level(tokens):
            annotations: List[Annotation] = []
            remaining: List[Token] = []
            i = 0
            while i < len(part):
                token = part[i]
                if token.kind in ("comment", "javadoc"):
                    i += 1
                    continue
                if token.value == "@":
                    item, next_i = _parse_annotation(part, i)
                    if item is not None:
                        annotations.append(item)
                    i = next_i
                    continue
                if token.kind == "id" and token.value == "final":
                    i += 1
                    continue
                remaining.append(token)
                i += 1
            if not remaining:
                continue
            name_index = -1
            for i in range(len(remaining) - 1, -1, -1):
                if remaining[i].kind == "id":
                    name_index = i
                    break
            if name_index <= 0:
                continue
            params.append(
                JavaParam(
                    name=remaining[name_index].value,
                    type_text=_render(remaining[:name_index]),
                    annotations=annotations,
                )
            )
        return params

    def _parse_fields(
        self,
        tokens: Sequence[Token],
        annotations: List[Annotation],
        javadoc: Javadoc,
        modifiers: List[str],
    ) -> List[JavaField]:
        parts = _split_top_level(tokens)
        if not parts:
            return []
        fields: List[JavaField] = []

        def first_declarator(part: Sequence[Token]) -> Tuple[str, str, List[Annotation]]:
            extra_annotations: List[Annotation] = []
            remaining: List[Token] = []
            i = 0
            while i < len(part):
                token = part[i]
                if token.kind in ("comment", "javadoc"):
                    i += 1
                    continue
                if token.value == "@":
                    item, next_i = _parse_annotation(part, i)
                    if item is not None:
                        extra_annotations.append(item)
                    i = next_i
                    continue
                if token.kind == "id" and token.value == "final":
                    i += 1
                    continue
                remaining.append(token)
                i += 1
            name_index = -1
            for i in range(len(remaining) - 1, -1, -1):
                if remaining[i].kind == "id":
                    name_index = i
                    break
            if name_index <= 0:
                return "", "", extra_annotations
            return remaining[name_index].value, _render(remaining[:name_index]), extra_annotations

        name, type_text, extra = first_declarator(parts[0])
        if not name or not type_text:
            return []
        fields.append(
            JavaField(
                name=name,
                type_text=type_text,
                annotations=list(annotations) + extra,
                javadoc=javadoc,
                modifiers=list(modifiers),
            )
        )
        for part in parts[1:]:
            extra_name, _, extra_annotations = first_declarator(part)
            if extra_name:
                fields.append(
                    JavaField(
                        name=extra_name,
                        type_text=type_text,
                        annotations=list(annotations) + extra_annotations,
                        javadoc=javadoc,
                        modifiers=list(modifiers),
                    )
                )
        return fields


def parse_java_source(text: str, source: str = "") -> JavaUnit:
    return JavaParser(tokenize(text), source).parse()
