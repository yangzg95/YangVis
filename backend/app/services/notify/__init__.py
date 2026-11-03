"""Webhook 机器人消息通知。

定位是**尽力而为的旁路**：工作流失败、运维待确认这些事件发生时，把一条
简短消息推到用户配置的群机器人上。发送失败绝不影响主流程——所有异常在
dispatch 内部兜住，结果（成功或失败）落 ``notify_log`` 供排障。

安全基线：通知正文只带 id、标题、原因、错误摘要；命令全文、SQL 全文、
任何密钥都不进 webhook。

注意：这里**不** re-export ``dispatch`` 函数——它会遮蔽同名子模块
（``from app.services.notify import dispatch`` 将永远拿到函数而非模块）。
调用方一律 ``from app.services.notify.dispatch import dispatch``。
"""
from app.services.notify.events import EVENTS, EventDef, event_infos

__all__ = ["EVENTS", "EventDef", "event_infos"]
