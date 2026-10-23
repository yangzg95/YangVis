"""百度网盘上传工具：对话里产出的报告/文本可直接存进用户本人绑定的网盘。

OAuth 绑定、token 刷新、xpan 三步上传都在 ``services/netdisk.py`` 的
:class:`NetdiskService` 里，本文件只做「模型面」：参数校验 + 把成功/失败
变成模型可读的一句话。文件固定落在应用目录的 ``chat/`` 子目录，与页面手动
保存报告的 ``reports/`` 分开，便于区分来源。

异步工具自开 ``SessionLocal()``（registry.py 的硬约定）；网盘服务本身是
httpx 异步实现，里面的同步 DB 触碰只有单行账号读写，与路由侧的用法一致。
"""
from __future__ import annotations

from app.database import SessionLocal
from app.errors import BusinessError
from app.services.netdisk import NetdiskService
from app.services.tools import ToolContext, local_tool

# 与 /office/netdisk/save-text 的 1MB 上限同口径：够装一份报告，
# 也挡住把对话通道当免费文件存储的用法。
_MAX_CONTENT_BYTES = 1024 * 1024


@local_tool(title="保存文件到百度网盘")
async def save_text_to_netdisk(ctx: ToolContext, filename: str, content: str) -> str:
    """把一段文本（分析报告、整理结果、对话中生成的文档等）保存成文件，上传到当前用户本人绑定的百度网盘。

    用户要求把对话里产出的内容「存到网盘 / 存到百度网盘 / 备份一下」时调用。
    文件保存在网盘应用目录的 chat/ 子目录下，上传成功后返回网盘里的完整路径。
    需要用户已在页面上绑定自己的百度网盘；未绑定或功能未启用时会返回提示，
    那时要如实告诉用户先去绑定，绝不可以假装上传成功。

    Args:
        filename: 保存的文件名，要带扩展名（如 月报.md、分析.txt），不要包含目录或路径。
        content: 文件的完整文本内容（UTF-8），不能为空，大小不超过 1MB。
    """
    # 模型可能把 "chat/月报.md" 之类的路径也塞进来：只留最后一段，
    # 子目录由工具固定，路径段清洗仍由 NetdiskService._remote_path 兜底。
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        raise ValueError("filename 不能为空，也不能只是路径，请给出带扩展名的文件名")

    data = content.encode("utf-8")
    if not data:
        raise ValueError("content 不能为空")
    if len(data) > _MAX_CONTENT_BYTES:
        raise ValueError("content 超过 1MB 上限，请精简内容后再保存")

    with SessionLocal() as db:
        service = NetdiskService(db, ctx.owner_id)
        # 不配置/未绑定不作为异常抛：把「做不了，该怎么办」作为工具结果交回
        # 模型，由它向用户说明，而不是让一次工具错误把整轮换掉。
        status = service.status_view()
        if not status.configured:
            return "百度网盘功能尚未启用（后端未配置 AppKey），请联系管理员后再试。"
        if not status.bound:
            return "当前用户还没有绑定百度网盘，请先在页面的百度网盘入口完成授权绑定，再重新保存。"

        try:
            fs_id, path = await service.upload_file(f"chat/{name}", data)
        except BusinessError as exc:
            return f"上传到百度网盘失败：{exc.msg}"

    return f"已上传到百度网盘：{path}（{len(data)} 字节，fs_id：{fs_id}）"
