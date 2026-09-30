"""时间戳约定：写进 ``DATETIME`` 列的「此刻」必须是本地 naive。

这个约定在测试里**测不出来**：测试库是 SQLite，``CURRENT_TIMESTAMP`` 落 UTC，
写本地 naive 和写 UTC naive 两种代码跑出来的断言全都一样；上线到 MySQL（``NOW()``
落 +08 的墙上时间）才会露馅——界面上出现「创建时间 15:02、开始时间 07:02」这种
比它自己晚 8 小时才被创建的运行。所以这里不看行为，直接扫源码，把写法钉住。

:func:`app.timeutil.db_now` 是唯一的入口；例外只有 token 有效期那几处，它们比的
是绝对时刻，从不和 ``created_at`` 摆在一起显示。
"""
from __future__ import annotations

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
