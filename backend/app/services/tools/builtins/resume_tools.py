"""在线简历读取工具：对话里的模型可以按 id 拿到用户本人简历的最新版本。

简历咨询会话在首条消息里贴过一份全文快照，但用户可能随后又在简历工作台改了
内容——工具读到的永远是服务端实时数据，模型不该抱着对话早期的旧快照作答。

同步函数 + 自开 Session：同步本地工具本身在线程池里执行（见 registry.py 的
约定），不碰请求级 session；归属权由 ResumeBuilderService 的 owner 作用域兜底，
只能读到本人简历。
"""
from __future__ import annotations

from app.database import SessionLocal
from app.services.resume_builder import ResumeBuilderService, _full_resume_text
from app.services.tools import ToolContext, local_tool


@local_tool(title="读取在线简历")
def get_resume_doc(ctx: ToolContext, doc_id: int) -> str:
    """读取当前用户指定的一份在线简历的最新完整内容（个人信息、工作/项目/教育经历、专业技能）。

    用户围绕某份在线简历咨询、要求点评或修改建议，且消息中给出了简历 id 时调用。
    返回服务端实时内容：用户刚在简历工作台改过也以本工具结果为准，不要使用对话
    早期贴过的旧版本。只能读取本人的简历，id 不存在或不属于本人时返回错误说明。

    Args:
        doc_id: 在线简历 id（用户消息中给出的整数）
    """
    with SessionLocal() as db:
        service = ResumeBuilderService(db, ctx.owner_id)
        try:
            row = service.get_doc(doc_id)
        except LookupError:
            # 不抛异常：把「读不到」作为工具结果交回模型，由它向用户澄清 id，
            # 而不是让一次工具错误把整轮换掉。
            return f"未找到 id 为 {doc_id} 的在线简历（可能已删除，或不属于当前用户）。"
        detail = service.to_detail(row)
        updated = row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "未知"

    body = _full_resume_text(detail["doc"])
    if not body:
        body = "（这份简历目前没有任何已填写内容）"
    return f"在线简历《{detail['title']}》（id：{doc_id}，最后更新：{updated}）\n---\n{body}"
