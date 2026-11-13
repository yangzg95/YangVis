"""素材库（跨项目复用的图片/视频素材）。

与短视频的「项目内参考图」分界线在这里：参考图跟着项目走、删项目就连文件一起删，
而人物形象、片头 logo、常用转场这类素材要换一个选题还能接着用，所以它不属于任何
项目，只属于上传它的那个账号。

两个模块各管一段：

* :mod:`app.services.materials.store` —— 落盘与路径守卫（魔数定扩展名、随机文件名、
  ``rel_path`` 读出来先重新解析一次）；
* :mod:`app.services.materials.service` —— 存取、条数闸门、删除时解掉引用它的镜头。

镜头对这些素材的引用长在 ``video_shot.material_ids`` 上（id 的字符串形式），所以
删一条素材必须能反查出谁在用——这也是它不再塞进任何 JSON 列的原因。
"""
from __future__ import annotations

# 素材类别，与 ``media_job.kind`` 巧合同名但语义无关：这里说的是「这条素材是
# 一张图还是一段视频」，由文件头魔数判定，不是作业的类别。
KIND_IMAGE = "image"
KIND_VIDEO = "video"
MATERIAL_KINDS = (KIND_IMAGE, KIND_VIDEO)
