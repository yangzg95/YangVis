"""时间工具：模型自己不知道「今天」是哪天。

问题里出现相对时间（今天、本周、最近）时，不论是作答还是组织检索词，都得先
换算成具体日期。固定按北京时间（UTC+8）输出，与部署机器的时区无关；这与
``app/timeutil.db_now()`` 的存库墙上时间是两套口径，不要混用。
"""
from datetime import datetime, timedelta, timezone

from app.services.tools import local_tool

# 用固定偏移而不是 zoneinfo：Windows 没有 IANA 时区库，
# ZoneInfo("Asia/Shanghai") 在开发机上得额外装 tzdata 才能跑。
_BEIJING_TZ = timezone(timedelta(hours=8))
_WEEKDAYS = "一二三四五六日"


@local_tool(title="查询当前时间")  # 标题与 chatTools 时间线文案保持一致
def current_datetime() -> str:
    """获取当前的日期和时间（北京时间）。

    问题涉及「今天」「现在」「本周」「最近」等相对时间时调用，先把
    相对时间换算成具体日期再继续。
    """
    now = datetime.now(_BEIJING_TZ)
    return f"{now:%Y-%m-%d %H:%M:%S} 星期{_WEEKDAYS[now.weekday()]}"
