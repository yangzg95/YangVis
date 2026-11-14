"""时间戳约定：写进 ``DATETIME`` 列的「此刻」必须是本地 naive。

这个约定在测试里**测不出来**：测试库是 SQLite，``CURRENT_TIMESTAMP`` 落 UTC，
写本地 naive 和写 UTC naive 两种代码跑出来的断言全都一样；上线到 MySQL（``NOW()``
落 +08 的墙上时间）才会露馅——界面上出现「创建时间 15:02、开始时间 07:02」这种
比它自己晚 8 小时才被创建的运行。所以这里以扫源码为主，把写法钉住；行为断言只在非 +08 的机器上才分得出对错。

:func:`app.timeutil.db_now` 是写库这一类里唯一的入口。两种时间不归它管：token
有效期比的是绝对时刻（下面白名单里那几个文件），发给系统外面的文本走
:func:`app.timeutil.beijing_now`——读者在 +08，跑代码的容器不一定。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# UTC 的「此刻」写在这些文件里是刻意的：OAuth/JWT 的有效期与锁定窗口。
# - security.py：access token 的 exp
# - login_guard.py：内存里的失败计数，只在自身口径内比较
# - netdisk.py：网盘授权按 expires_in（秒数）推算的到期时刻，从不显示
UTC_ALLOWED = {
    APP_DIR / "security.py",
    APP_DIR / "services" / "login_guard.py",
    APP_DIR / "services" / "netdisk.py",
}

UTC_NOW = re.compile(r"datetime\.now\(\s*(?:timezone\.utc|tz\s*=\s*timezone\.utc)\s*\)|datetime\.utcnow\(")


def test_no_utc_now_outside_the_token_exceptions():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path in UTC_ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if UTC_NOW.search(line):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "把 UTC 的此刻写进了 naive DATETIME 列——MySQL 的 NOW() 落的是本地墙上时间，"
        "同一行会差 8 小时。用 app.timeutil.db_now()；确实是 token 有效期的话，"
        "把文件加进本测试的 UTC_ALLOWED 并写明理由。\n" + "\n".join(offenders)
    )


def test_db_now_is_naive_local():
    from datetime import datetime

    from app.timeutil import db_now

    moment = db_now()
    assert isinstance(moment, datetime)
    assert moment.tzinfo is None, "写库的时间戳必须 naive，DATETIME 列存不下偏移量"
    assert moment.year == datetime.now().year


def test_db_now_does_not_read_the_process_timezone():
    """``db_now`` 不许出现裸 ``datetime.now()``：那是把库里的时间押在部署侧一行 ``TZ`` 上。

    线上真实踩过：网关 compose 写 ``TZ: PRC``，基础镜像（浮动 tag ``python:3.12-slim``）
    升到 Debian 13 后 ``/usr/share/zoneinfo/PRC`` 被拆进 ``tzdata-legacy``，slim 镜像不装，
    glibc 找不到这个 zone 既不报错也不回退到 ``/etc/localtime``，而是退回 UTC——于是
    ``db_now`` 写的列整体早 8 小时，同一行里 MySQL ``NOW()`` 给的 ``created_at`` 照旧是对的。
    四个月没人发现，就是因为没有任何一行日志会提示。

    开发机本身在 +08，行为断言在这儿看不出裸 ``now()`` 与固定 +08 的区别，所以扫源码。
    """
    from app import timeutil

    tree = ast.parse(Path(timeutil.__file__).read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "db_now")
    # 只看可执行语句：docstring 里要是有「裸 datetime.now()」这种反面写法，扫到它会误报。
    stmts = fn.body[1:] if isinstance(fn.body[0], ast.Expr) else fn.body
    body = ast.unparse(ast.Module(body=stmts, type_ignores=[]))

    assert not re.search(r"datetime\.now\(\s*\)", body), (
        "db_now 回到了进程时区；容器 TZ 一失效，写库的时间就整体偏移，"
        "而 created_at 那半边由 MySQL 给、看不出错。用 datetime.now(BEIJING_TZ).replace(tzinfo=None)。"
    )
    assert "BEIJING_TZ" in body, "db_now 必须以 BEIJING_TZ 取真实时刻，再剥掉偏移写库"
    assert ".replace(tzinfo=None)" in body, "写库必须 naive：DATETIME 列存不下偏移量"


def test_db_now_is_the_beijing_wall_clock():
    """``db_now`` 确实是那一刻的 +08 墙上时间，不是给本地时间贴个标签。"""
    from datetime import datetime, timedelta, timezone

    from app.timeutil import db_now

    moment = db_now()
    expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    assert abs((moment - expected).total_seconds()) < 5


def test_beijing_now_is_a_real_instant_not_a_label():
    """群消息与自建接收端用它：偏移恒为 +08，且确实是那一刻的北京时间。

    第二条断言防的是 ``datetime.now().replace(tzinfo=BEIJING_TZ)`` 这种写法——
    它只是给进程本地时间贴了个 +08 标签，容器在 UTC 时发出去的消息照旧早 8 小时。
    """
    from datetime import datetime, timedelta, timezone

    from app.timeutil import beijing_now

    moment = beijing_now()
    assert moment.utcoffset() == timedelta(hours=8)
    assert abs((moment - datetime.now(timezone.utc)).total_seconds()) < 5
