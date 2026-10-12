"""运维命令的安全判定。

这个模块是「让 AI 操作服务器 / 数据库」这件事的边界所在，被刻意写成一组
纯函数：不碰网络、不碰数据库，因此可以把各种绕过手法一条条钉进测试里。

三条判定的共同结论用 :class:`Verdict` 表达：

``readonly``      —— 确定是只读的，AI 可以直接执行；
``needs_confirm`` —— 看不出来是不是只读，必须先让用户点头（仅服务器侧存在）；
``forbidden``     —— 明确危险，连确认都不给。

需要强调的是：解析器只是第一道门。数据库侧真正的保证是连接建立后立刻下的
``SET SESSION TRANSACTION READ ONLY``——即使这里的解析被绕过，服务端自己
会拒绝写入。
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

VerdictKind = Literal["readonly", "needs_confirm", "forbidden"]


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    reason: str = ""

    @property
    def allowed(self) -> bool:
        """是否可以在没有人工确认的情况下直接执行。"""
        return self.kind == "readonly"


_READONLY = Verdict("readonly")


# ---- Shell 命令 -------------------------------------------------------------

# 只看第一个词的白名单是纸糊的：`ls; rm -rf /`、`ls && rm x`、`` `id` ``、
# `$(id)`、`cat a > b` 全都能过。所以这里先把所有能改变「执行什么」的元字符
# 一律挡掉，只对管道网开一面（且要求每一段都独立合法）。
_SHELL_METACHARS = (";", "&", ">", "<", "`", "$(", "${", "\n", "\r", "||", "&&")

# 命令名 → 允许的子命令 / 参数规则。值为 None 表示该命令的参数不受进一步限制
# （但依然受下面通用的「不能有元字符」和「不能带危险 flag」约束）。
_READONLY_COMMANDS: dict[str, frozenset[str] | None] = {
    "ls": None,
    "ll": None,
    "cat": None,
    "head": None,
    "tail": None,
    "grep": None,
    "egrep": None,
    "wc": None,
    "sort": None,
    "uniq": None,
    "cut": None,
    "df": None,
    "du": None,
    "free": None,
    "uptime": None,
    "who": None,
    "w": None,
    "id": None,
    "whoami": None,
    "date": None,
    "hostname": None,
    "uname": None,
    "ps": None,
    "top": None,
    "netstat": None,
    "ss": None,
    "ip": None,
    "ifconfig": None,
    "ping": None,
    "stat": None,
    "file": None,
    "lsof": None,
    "env": None,
    "pwd": None,
    "which": None,
    "md5sum": None,
    "nproc": None,
    "vmstat": None,
    "iostat": None,
    "find": None,  # 危险 flag 由 _FORBIDDEN_FLAGS 单独拦
    "systemctl": frozenset({"status", "is-active", "is-enabled", "is-failed",
                            "list-units", "list-unit-files", "list-timers", "show", "cat"}),
    "journalctl": None,
    "docker": frozenset({"ps", "logs", "images", "inspect", "stats", "top", "version", "info"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "version"}),
    "git": frozenset({"status", "log", "diff", "show", "branch", "remote"}),
}

# 刻意不进白名单的东西，列在这里只是为了让「为什么没有它」有据可查：
# awk / sed / perl / python / sh / bash —— 都能任意执行代码或就地改文件
# curl / wget / nc / scp —— 能把数据带出去，也能把东西下下来执行
# tee / dd / truncate —— 写文件
# xargs —— 把任意命令重新拼出来执行
# chmod / chown / kill / rm / mv / cp —— 直接改变系统状态

# 即使命令本身在白名单里，带上这些 flag 也不再是只读的。
_FORBIDDEN_FLAGS: dict[str, tuple[str, ...]] = {
    "find": ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fls", "-fprint"),
    "journalctl": ("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush"),
    "ip": ("add", "del", "set", "change", "replace", "flush"),
    "ps": (),
    "git": (),
}

# 明确的毁灭性操作：不给确认机会，直接拒绝。
# 这些东西没有任何「用户其实想这么干」的合理场景值得走一遍确认流程。
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\brm\s+(-\w+\s+)*-\w*[rR]\w*f|\brm\s+(-\w+\s+)*-\w*f\w*[rR]", re.I),
    re.compile(r"\brm\s+(-\S+\s+)*/\s*$", re.I),
    re.compile(r"\bmkfs(\.\w+)?\b", re.I),
    re.compile(r"\bdd\b[^|]*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/[sh]d[a-z]", re.I),
    re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:", re.S),  # fork 炸弹
    re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b", re.I),
    re.compile(r"\bchmod\s+(-\S+\s+)*(777|-R\s+777)\s+/\s*$", re.I),
    re.compile(r"\buserdel\b|\bgroupdel\b", re.I),
    re.compile(r"\bhistory\s+-c\b", re.I),
)


def classify_command(command: str) -> Verdict:
    """判定一条 shell 命令的安全级别。"""
    cmd = (command or "").strip()
    if not cmd:
        return Verdict("forbidden", "命令为空")

    for pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(cmd):
            return Verdict("forbidden", "命令包含破坏性操作，已拒绝执行")

    # 元字符检查放在管道拆分之前：`|` 是唯一被允许的连接符，其余一律不认。
    # 注意 `||` 要在 `|` 之前判掉，所以它在 _SHELL_METACHARS 里。
    for token in _SHELL_METACHARS:
        if token in cmd:
            return Verdict(
                "needs_confirm",
                f"命令包含 shell 特殊字符 {token!r}，无法确认它是否只读",
            )

    segments = [seg.strip() for seg in cmd.split("|")]
    if any(not seg for seg in segments):
        return Verdict("needs_confirm", "管道写法不完整")

    # 管道只有在每一段都独立通过白名单时才算只读；只要有一段不确定，
    # 整条命令就不确定。
    for segment in segments:
        verdict = _classify_segment(segment)
        if verdict.kind != "readonly":
            return verdict
    return _READONLY


def _classify_segment(segment: str) -> Verdict:
    try:
        parts = shlex.split(segment)
    except ValueError:
        return Verdict("needs_confirm", "命令引号不闭合，无法解析")
    if not parts:
        return Verdict("needs_confirm", "命令为空")

    # `VAR=x cmd` 会让 cmd 跑在被篡改过的环境里（LD_PRELOAD 是典型），
    # 这已经不是「读」了。
    if "=" in parts[0] and not parts[0].startswith("-"):
        return Verdict("needs_confirm", "命令前带环境变量赋值，无法确认它是否只读")

    name = parts[0].rsplit("/", 1)[-1]
    if name not in _READONLY_COMMANDS:
        return Verdict("needs_confirm", f"{name} 不在只读白名单内")

    args = parts[1:]

    for flag in _FORBIDDEN_FLAGS.get(name, ()):
        # 前缀匹配而不是相等：`--vacuum-size=1M` 和 `--vacuum-size 1M` 是同一件事。
        if any(arg == flag or arg.startswith(f"{flag}=") for arg in args):
            return Verdict("needs_confirm", f"{name} {flag} 可能产生副作用")

    subcommands = _READONLY_COMMANDS[name]
    if subcommands is not None:
        # 跳过前导的全局选项，找到真正的子命令。
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub is None:
            return Verdict("needs_confirm", f"{name} 缺少子命令")
        if sub not in subcommands:
            return Verdict("needs_confirm", f"{name} {sub} 不在只读白名单内")

    return _READONLY


# ---- SQL --------------------------------------------------------------------

_SQL_READ_KEYWORDS = frozenset(
    {"select", "show", "desc", "describe", "explain", "with", "analyze"}
)

# 这些即便出现在 SELECT 里也能写盘、读盘或把连接拖死。只读、可写两种模式
# 都拦——可写放开的是 DML/DDL，不是任意执行能力的口子。
_SQL_FORBIDDEN_ALWAYS = (
    re.compile(r"\binto\s+outfile\b", re.I),
    re.compile(r"\binto\s+dumpfile\b", re.I),
    re.compile(r"\bload_file\s*\(", re.I),
    re.compile(r"\bsleep\s*\(", re.I),
    re.compile(r"\bbenchmark\s*\(", re.I),
)

# 只在只读模式下追加拦截的：锁子句在可写会话里是正当用法。
_SQL_FORBIDDEN_READONLY = (
    re.compile(r"\bfor\s+update\b", re.I),
    re.compile(r"\block\s+in\s+share\s+mode\b", re.I),
)

# 可写模式下仍然不放行的账户 / 实例级操作：改数据是一回事，动权限和实例是
# 另一回事——这些在控制台里没有「只是想试试」的合理场景。
_SQL_WRITE_FORBIDDEN = (
    re.compile(r"\b(grant|revoke)\b", re.I),
    re.compile(r"\b(create|alter|drop|rename)\s+user\b", re.I),
    re.compile(r"\bset\s+(global|persist)", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\b(install|uninstall)\s+(plugin|component)\b", re.I),
    re.compile(r"\bload\s+data\b", re.I),
)

# ---- SQL 脚本扫描 -------------------------------------------------------------
#
# classify_sql 的判定、批量执行的语句拆分，都建立在同一个逐字符扫描器上——
# 各处对「什么算代码」的口径必须一致，否则会出现拆分能切、判定误报（或者
# 反过来）的漂移。

# 字符角色：code 参与安全判定与语句切分；literal 是字符串 / 标识符内容；
# comment 是注释内容。
_SCAN_CODE = "code"
_SCAN_LITERAL = "literal"
_SCAN_COMMENT = "comment"


def _scan_sql(text: str) -> Iterator[tuple[str, str]]:
    """逐字符扫描 SQL，产出 (字符, 角色) 流。

    单引号 / 双引号 / 反引号内部是 literal，``--``、``#``、``/* */`` 注释
    内部是 comment，其余是 code。字符串内识别 MySQL 默认的反斜杠转义和
    双写引号转义（反引号标识符只吃双写，不吃反斜杠）；未闭合的引号 / 注释
    保守地一直延续到文末——宁可少切，不可错切。
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("--", i) or ch == "#":
            end = text.find("\n", i)
            end = n if end == -1 else end
            for k in range(i, end):
                yield text[k], _SCAN_COMMENT
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for k in range(i, end):
                yield text[k], _SCAN_COMMENT
            i = end
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            yield ch, _SCAN_LITERAL
            i += 1
            while i < n:
                c = text[i]
                if c == "\\" and quote != "`" and i + 1 < n:
                    # MySQL 默认开反斜杠转义：\' 不终结字符串。
                    yield c, _SCAN_LITERAL
                    yield text[i + 1], _SCAN_LITERAL
                    i += 2
                    continue
                if c == quote:
                    yield c, _SCAN_LITERAL
                    i += 1
                    if i < n and text[i] == quote:
                        # 双写引号是转义（'it''s'），不退出字符串。
                        yield text[i], _SCAN_LITERAL
                        i += 1
                        continue
                    break
                yield c, _SCAN_LITERAL
                i += 1
            continue
        yield ch, _SCAN_CODE
        i += 1


def strip_sql_comments(sql: str) -> str:
    """去掉注释（注释内容替换成等长空格，字符位置不变）。

    注释是绕过关键字检查最省事的办法（``/*x*/DELETE ...``、
    ``SELECT 1 -- \\n; DROP TABLE t``），所以判定必须在去注释之后做。
    字符串字面量里的 ``--`` / ``/*`` 不是注释（``'a--b'``），扫描器不会误伤。
    """
    return "".join(
        " " if role == _SCAN_COMMENT else ch for ch, role in _scan_sql(sql or "")
    )


def _mask_sql_literals(sql: str) -> str:
    """把字符串 / 标识符内容替换成等长空格（引号本身一并打码）。

    安全判定只关心 code 部分：``'a;b'`` 里的分号、``'for update'`` 里的
    关键字都不该触发多语句 / 危险子句误报。
    """
    return "".join(
        " " if role != _SCAN_CODE else ch for ch, role in _scan_sql(sql)
    )


def split_sql_script(script: str) -> list[str]:
    """把一段 SQL 脚本拆成语句列表（批量执行的入口）。

    只在 code 状态下按分号切；纯注释 / 空白的片段丢弃；每句 strip 后返回。
    只读平台没有存储过程，不支持 DELIMITER。
    """
    statements: list[str] = []
    current: list[str] = []

    def flush() -> None:
        statement = "".join(current).strip()
        # 去掉注释后没剩下代码的片段（整段注释、连续空分号）不值得执行。
        if strip_sql_comments(statement).strip():
            statements.append(statement)

    for ch, role in _scan_sql(script or ""):
        if ch == ";" and role == _SCAN_CODE:
            flush()
            current = []
        else:
            current.append(ch)
    flush()
    return statements


def classify_sql(sql: str) -> Verdict:
    """判定一条 SQL 是不是纯查询。数据库侧没有「确认后执行」，只有能与不能。"""
    text = _mask_sql_literals(strip_sql_comments(sql)).strip()
    if not text:
        return Verdict("forbidden", "SQL 为空")

    # 去掉结尾分号之后仍然含分号，说明是多语句。
    body = text.rstrip().rstrip(";")
    if ";" in body:
        return Verdict("forbidden", "不允许一次提交多条语句")

    keyword = re.split(r"[\s(]+", body.lstrip("("), maxsplit=1)[0].lower()
    if keyword not in _SQL_READ_KEYWORDS:
        return Verdict("forbidden", f"只允许查询语句，不支持 {keyword.upper() or '该'} 操作")

    # WITH ... 后面可以跟 INSERT/UPDATE/DELETE，光看首关键字不够。
    if keyword == "with" and re.search(r"\)\s*(insert|update|delete|replace)\b", body, re.I):
        return Verdict("forbidden", "WITH 之后不允许写操作")

    for pattern in _SQL_FORBIDDEN_ALWAYS + _SQL_FORBIDDEN_READONLY:
        if pattern.search(body):
            return Verdict("forbidden", "SQL 包含不允许的函数或子句")

    return _READONLY


def classify_sql_writable(sql: str) -> Verdict:
    """可写控制台（连接开了「允许写入」）的判定。

    与只读判定的差异只在两点：首关键字不限于查询（DML/DDL 都放行），锁子句
    是正当用法。仍然成立的红线：一次一句、高危函数（读盘写盘 / 拖死连接）、
    账户与实例级操作——可写放开的是数据，不是服务器本身。

    可写会话里「不确定」没有意义：连接是用户自己开的写开关，能过这几条红线
    就放行，所以这里只有 readonly（放行）与 forbidden 两种结论。
    """
    text = _mask_sql_literals(strip_sql_comments(sql)).strip()
    if not text:
        return Verdict("forbidden", "SQL 为空")

    body = text.rstrip().rstrip(";")
    if ";" in body:
        return Verdict("forbidden", "不允许一次提交多条语句")

    for pattern in _SQL_FORBIDDEN_ALWAYS + _SQL_WRITE_FORBIDDEN:
        if pattern.search(body):
            return Verdict("forbidden", "SQL 包含不允许的函数、子句或账户级操作")

    return _READONLY


_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.I)


def ensure_limit(sql: str, limit: int) -> str:
    """给没写 LIMIT 的 SELECT 补一个上限。

    一条 ``SELECT * FROM big_table`` 既能把浏览器卡死，也能把整张表塞进模型
    上下文。SHOW / DESC 这类语句不接受 LIMIT，原样返回。
    """
    body = strip_sql_comments(sql).strip().rstrip(";").strip()
    if not body.lower().startswith(("select", "with")):
        return body
    if _LIMIT_RE.search(body):
        return body
    return f"{body} LIMIT {limit}"


# ---- Redis ------------------------------------------------------------------

# Redis 没有「语句」的概念，只有命令。这里用显式白名单，而不是「排除危险的」：
# 命令表一直在变，漏掉一个新增的写命令，代价就是数据被改。
_REDIS_READ_COMMANDS = frozenset({
    "get", "mget", "strlen", "getrange", "substr",
    "exists", "type", "ttl", "pttl", "keys", "scan", "randomkey", "dbsize",
    "hget", "hmget", "hgetall", "hkeys", "hvals", "hlen", "hexists", "hscan", "hstrlen",
    "lrange", "llen", "lindex", "lpos",
    "smembers", "scard", "sismember", "smismember", "srandmember", "sscan", "sinter", "sunion",
    "sdiff",
    "zrange", "zrangebyscore", "zrevrange", "zrevrangebyscore", "zrangebylex",
    "zcard", "zcount", "zscore", "zmscore", "zrank", "zrevrank", "zscan",
    "getbit", "bitcount", "bitpos",
    "pfcount", "geopos", "geodist", "geosearch",
    "xrange", "xrevrange", "xlen", "xinfo",
    "info", "time", "lastsave", "ping", "echo", "dbsize", "memory", "slowlog",
    "object", "client", "command", "latency", "lolwut", "config",
})

# 两段式命令里，只有这些子命令是只读的。
_REDIS_READ_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "config": frozenset({"get"}),          # CONFIG SET 能改 dir/dbfilename，进而写任意文件
    "client": frozenset({"list", "info", "id", "getname"}),
    "memory": frozenset({"usage", "stats", "doctor"}),
    "slowlog": frozenset({"get", "len"}),
    "object": frozenset({"encoding", "refcount", "idletime", "freq", "help"}),
    "xinfo": frozenset({"stream", "groups", "consumers"}),
    "command": frozenset({"count", "docs", "info", "getkeys", "list"}),
    "latency": frozenset({"latest", "history", "doctor"}),
}

# 即使名字看着无害也一律禁止的：EVAL / EVALSHA / FUNCTION 能跑 Lua，
# Lua 里可以写任何东西；DEBUG 能让实例崩溃；MIGRATE 能把数据搬走。
_REDIS_HARD_DENY = frozenset({
    "eval", "evalsha", "eval_ro", "evalsha_ro", "function", "fcall", "fcall_ro",
    "script", "debug", "shutdown", "migrate", "replicaof", "slaveof", "failover",
    "flushall", "flushdb", "swapdb", "reset", "monitor", "subscribe", "psubscribe",
})


def classify_redis(command: str) -> Verdict:
    """判定一条 Redis 命令是不是只读。"""
    try:
        parts = shlex.split((command or "").strip())
    except ValueError:
        return Verdict("forbidden", "命令引号不闭合，无法解析")
    if not parts:
        return Verdict("forbidden", "命令为空")

    name = parts[0].lower()
    if name in _REDIS_HARD_DENY:
        return Verdict("forbidden", f"{name.upper()} 可能修改数据或影响实例，已禁止")
    if name not in _REDIS_READ_COMMANDS:
        return Verdict("forbidden", f"{name.upper()} 不在只读命令白名单内")

    subcommands = _REDIS_READ_SUBCOMMANDS.get(name)
    if subcommands is not None:
        if len(parts) < 2:
            return Verdict("forbidden", f"{name.upper()} 缺少子命令")
        sub = parts[1].lower()
        if sub not in subcommands:
            return Verdict("forbidden", f"{name.upper()} {sub.upper()} 不是只读操作")

    return _READONLY


# 可写模式下按子命令拦的：CONFIG SET / REWRITE 能改 dir/dbfilename，
# 借 RDB 落盘写任意文件——这和「key 可写」不是一个量级的口子。
_REDIS_WRITABLE_DENY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "config": frozenset({"set", "rewrite"}),
}


def classify_redis_writable(command: str) -> Verdict:
    """可写控制台的 Redis 判定：从白名单翻转为黑名单。

    硬禁用清单（Lua / DEBUG / FLUSH* / 主从拓扑 / SHUTDOWN 等）依旧有效——
    可写放开的是 key 的增删改，不是实例本身。新出现的命令默认放行：用户既然
    打开了写开关，「这个命令认不认识」不该再是门槛，危险类目都已在硬禁清单里。
    """
    try:
        parts = shlex.split((command or "").strip())
    except ValueError:
        return Verdict("forbidden", "命令引号不闭合，无法解析")
    if not parts:
        return Verdict("forbidden", "命令为空")

    name = parts[0].lower()
    if name in _REDIS_HARD_DENY:
        return Verdict("forbidden", f"{name.upper()} 可能影响整个实例，已禁止")

    denied_subs = _REDIS_WRITABLE_DENY_SUBCOMMANDS.get(name)
    if denied_subs and len(parts) >= 2 and parts[1].lower() in denied_subs:
        return Verdict("forbidden", f"{name.upper()} {parts[1].upper()} 可能改写实例文件，已禁止")

    return _READONLY
