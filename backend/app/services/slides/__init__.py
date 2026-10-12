"""幻灯片业务包：deck 的存取与 AI 生成、HTML 渲染、本地配图存储。

对外只有 ``SlideService`` 这一张脸（router 和其它 service 都从包名取它），所以
门面只重导出它一个；渲染与存储没有入口类，调用方按子模块取名字。三个模块的分工：

* :mod:`app.services.slides.service` —— deck 的读写、后台生成任务与模型输出解析；
* :mod:`app.services.slides.html` —— 从 ``slides`` 这一列确定性渲染自包含 HTML，
  单独成模块是因为它是整份功能唯一的注入面：转义与白名单都住在这里，改动它的
  理由（加版式、调字号）跟存取无关；
* :mod:`app.services.slides.store` —— 配图落盘与 URL 解析（扩展名按魔数判定、
  文件名是随机 id），router 的图片上传接口直接用它。

依赖是单向的：store 谁都不接，html 接 store 的 ``resolve_url``，service 接 html
与 store；没有回边。logger 名保持原样（``yangvis.slides`` / ``yangvis.slides.store``），
它们是日志通道而不是模块路径。
"""
from app.services.slides import html, store  # noqa: F401
from app.services.slides.service import SlideService  # noqa: F401
