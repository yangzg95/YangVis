"""写库与展示的时间戳约定：固定的 +08 墙上时间，naive。

库里所有 ``DATETIME`` 列都是无时区的墙上时间，因为 ``created_at`` / ``updated_at``
走的是 MySQL 的 ``NOW()``——落的是数据库所在时区（+08）的时间。显式写 UTC 的话，
同一行会出现「创建时间 15:02、开始时间 07:02」，界面上就是一次比它自己晚 8 小时
才被创建的运行；前端 ``new Date(值).toLocaleString()`` 把无偏移的字符串按本地解析，
差值直接显示出来。

所以：写进这些列、或序列化给前端显示的「此刻」，都用 :func:`db_now`。

token 有效期（``security.py``、``services/login_guard.py``、``services/netdisk.py``）
不在这一类——那是 OAuth 语义上的绝对时刻，本来就该带 UTC，别顺手改。而发给
系统外面的文本（群消息正文、自建接收端的 payload）走 :func:`beijing_now`：
读者在 +08，跑这段代码的容器却不一定。

测试库是 SQLite（``CURRENT_TIMESTAMP`` 为 UTC），两种写法在那儿看不出区别，
所以这条约定由 ``tests/test_time_convention.py`` 从源码上钉住。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# 与 datetime_tools 同一口径：不用 ZoneInfo，省掉开发机上的 tzdata 依赖。
BEIJING_TZ = timezone(timedelta(hours=8))


def db_now() -> datetime:
    """写进 ``DATETIME`` 列的「此刻」：+08 墙上时间、naive，与 ``created_at`` 同一口径。

    取的是真实时刻再剥掉偏移，不跟进程时区。跟进程时区的写法（裸 ``datetime.now()``）
    等于把库里的时间戳押在部署侧那一行 ``TZ`` 上：网关 compose 曾写 ``TZ: PRC``，基础
    镜像升到 Debian 13 后 ``PRC`` 这个 zone 文件被拆进 ``tzdata-legacy``（slim 不装），
    glibc 找不到不报错、静默退回 UTC——同一行就成了「创建时间 17:53、开始时间 09:53」，
    而 ``created_at`` 那半边由 MySQL 的 ``NOW()`` 提供，照旧是对的，所以没人察觉。
    ``bootstrap.verify_clock_agreement()`` 每次启动实测两边差值，就是不让它再静默。
    """
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_now() -> datetime:
    """发出去给系统外面看的「此刻」：固定北京时间，带 ``+08:00`` 偏移。

    与 :func:`db_now` 的分工是读者不同：那个要和库里的 ``created_at`` 摆在一起
    比较，这个只出现在群消息正文与自建接收端的 payload 里。这类文本不能跟随
    进程所在容器的时区——部署侧只给 mysql 配了 ``TZ`` 而 app 容器还是默认的 UTC
    时，群里收到的消息会整整早 8 小时。
    """
    return datetime.now(BEIJING_TZ)
