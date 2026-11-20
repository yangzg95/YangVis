"""人物库（跨项目复用的角色档案与形象图）。

与素材库的分工：素材（:mod:`app.services.materials`）是无结构的图/视频文件，人物库
是**带文字档案的角色**——名字、人设、衣着、外形特色加一张专属形象图。一条短视频
通过 ``video_project.character_ids`` 认领项目角色组，档案在脚本生成时喂给模型，
形象图在渲染时作为参考图注入首帧/参考图作业。

两个模块各管一段（与素材库同一形状）：

* :mod:`app.services.characters.store` —— 形象图落盘与缩略图（魔数定扩展名、随机
  文件名、只收图片、``rel_path`` 读出来先过媒体层的路径守卫）；
* :mod:`app.services.characters.service` —— 存取、条数闸门、删除时的引用反查。

删除是硬删除：项目里残留的角色 id 由视频侧在保存/渲染时按「已不存在」跳过，与
素材文件丢失的降级口径一致。
"""
from __future__ import annotations

# 形象图只收图片：角色要的是一张稳定的人脸/穿搭参考，视频容器没有入口。
KIND_IMAGE = "image"
