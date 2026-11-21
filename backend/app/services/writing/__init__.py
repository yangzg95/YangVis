"""智能应用 · 创作（写小说 / 写文章）。

包内分工：

- ``words``：字数统计纯函数，服务端是字数的唯一事实源；
- ``outline``：大纲树清洗、cid 发放、参数白名单与 part 对账；
- ``service``：按 owner 隔离的作品/正文块存取与导出。

M1 只有手工写作（CRUD + 导出），M2/M3 再接内置工作流的后台生成。
"""
from app.services.writing.service import WritingService

__all__ = ["WritingService"]
