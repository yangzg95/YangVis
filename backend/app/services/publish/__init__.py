"""发布域：把渲好的各镜片段拼成成片，并组织成手动发布包。

与 :mod:`app.services.media` 的分工：媒体作业对接火山方舟（外部异步任务，常驻循环
推进），这里的发布作业是**本机活**——ffmpeg 拼接已经落盘的片段，一次请求级后台
任务几秒到几分钟跑完，事实源是 ``publish_job`` 表。

目前只有一个通道：

* ``manual`` —— 合成成片 + 封面 + 文案打包，由人拿去抖音创作者平台手动上传。
  ``open_api``（开放平台 API）/ ``browser``（浏览器自动化）是预留通道，词表先占位，
  接入时同一张表加分支，不需要迁移。

模块划分与媒体层同一形状：

* :mod:`app.services.publish.composer` —— ffmpeg 调用与 concat 策略（直拼优先、
  失败回退重编码）；
* :mod:`app.services.publish.runner` —— 发布作业的执行体、状态落库与重启收尾。
"""
from __future__ import annotations

PUBLISH_QUEUED = "queued"
PUBLISH_RUNNING = "running"
PUBLISH_SUCCEEDED = "succeeded"
PUBLISH_FAILED = "failed"

# 终态：重启收尾与删除闸门都按它判断。
PUBLISH_FINAL_STATES = frozenset({PUBLISH_SUCCEEDED, PUBLISH_FAILED})
# 还占着「这个项目正在合成」位子的状态。
PUBLISH_ACTIVE_STATES = frozenset({PUBLISH_QUEUED, PUBLISH_RUNNING})

# 发布通道。后两个预留：通道枚举先只放 manual，schema 那边的 Literal 也只收它。
CHANNEL_MANUAL = "manual"
CHANNEL_OPEN_API = "open_api"
CHANNEL_BROWSER = "browser"
