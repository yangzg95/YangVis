"""对话业务包：ReAct 回答、长期记忆与会话摘要。

``service`` 是对话主链路——会话与消息的读写、ReAct 循环、SSE 流式回答、运维确认
卡片的上半场接线，以及一轮问答落库后的收尾任务。``memory`` 只管记忆提取与摘要
重算，它对会话和模型配置一无所知：收尾任务放在 ``service`` 侧正是因为要读的是
会话归属和「用哪份对话模型配置」，那两项都是 ``ChatService`` 的带权限加载器。

门面按「外面到底从包名取哪些名字」转发：routers 取 ``ChatService``、``ROLE_USER``、
``derive_title``，测试还取 ``ROLE_ASSISTANT``，就这四个。其余一律走子模块路径——
routers/memory.py 用 ``from app.services.chat.memory import MemoryService``；测试要
patch 模块全局（``build_chat_model``、``fire_and_forget`` 这些）必须指到
``chat.service``，隔着门面 patch 不会作用到真正执行的那份名字。

``chat_ops``（运维工具箱与审计）没有收进来：``routers/ops.py`` 也在用它的 ``Auditor``，
它是跨域共用的工具，收进对话包等于让运维那一侧隔着对话去拿。
"""
from app.services.chat import memory  # noqa: F401
from app.services.chat.service import (  # noqa: F401
    ROLE_ASSISTANT,
    ROLE_USER,
    ChatService,
    derive_title,
)
