"""短视频业务包：项目的存取、脚本触发与首帧渲染入队。

对外只有 :class:`app.services.videos.service.VideoService` 这一张脸；媒体层的
方舟客户端、落盘与作业循环都从 :mod:`app.services.media` 按子模块取名字，这里不
再转发一遍——转发门面会让「渲染作业」看起来像是本包的私有实现，而它其实是图片、
视频、以后可能还有别的功能共用的那一层。
"""
from app.services.videos.service import VideoService  # noqa: F401
