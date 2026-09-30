"""写库与展示的时间戳约定：本地 naive。

库里所有 ``DATETIME`` 列都是无时区的墙上时间，因为 ``created_at`` / ``updated_at``
走的是 MySQL 的 ``NOW()``——落的是数据库所在时区（+08）的时间。显式写 UTC 的话，
同一行会出现「创建时间 15:02、开始时间 07:02」，界面上就是一次比它自己晚 8 小时
才被创建的运行；前端 ``new Date(值).toLocaleString()`` 把无偏移的字符串按本地解析，
差值直接显示出来。

所以：写进这些列、或序列化给前端显示的「此刻」，都用 :func:`db_now`。

token 有效期（``security.py``、``services/login_guard.py``、``services/netdisk.py``）
不在这一类——那是 OAuth 语义上的绝对时刻，本来就该带 UTC，别顺手改。

测试库是 SQLite（``CURRENT_TIMESTAMP`` 为 UTC），两种写法在那儿看不出区别，
所以这条约定由 ``tests/test_time_convention.py`` 从源码上钉住。
"""
from __future__ import annotations

from datetime import datetime


def db_now() -> datetime:
    """写进 ``DATETIME`` 列的「此刻」：本地 naive，与 ``created_at`` 同一口径。"""
    return datetime.now()
