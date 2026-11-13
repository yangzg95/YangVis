"""媒体生成层（火山方舟 Ark 的图片与视频）共用的词汇表。

这一层和 :mod:`app.services.workflows` 是两条独立的执行路径，分界线是**谁来记状态**：

* 脚本阶段（想法 → 创作卡 → 分镜文本）是一次模型调用，几十秒量级、跑完就结束，
  所以走工作流引擎，状态在 ``workflow_run`` 上；
* 渲染阶段（分镜 → 首帧图 → 视频片段）是**外部异步任务**：一条 5 秒 720p 的视频
  要排队几十秒到几分钟，有上游任务 id、能取消、能按 id 重查，而且一个项目是几十条
  并发作业。引擎那层「一条运行 = 一个进程活到结束」的模型套不上它，``MODEL_TASK_TIMEOUT``
  和 ``reset_stale_runs`` 都会把这种长任务误判成失败。

所以作业状态只写在 ``media_job`` 一张表上，由 :mod:`app.services.media.jobs` 里的常驻
循环推进。两边的连接点是 ``biz_type`` + ``biz_id``（寻址方式与
``workflow_run.subject_kind``/``subject_ref`` 一致），业务表自己不放任何状态列。

实现按职责分在这几个模块里——这一层没有唯一的入口类，``__init__`` 只当词表：

* :mod:`app.services.media.ark` —— 方舟的 HTTP 客户端（图片同步生成、视频任务
  创建/查询/取消）与错误码翻译；
* :mod:`app.services.media.store` —— 产物落盘与路径守卫（上游 URL 24 小时就失效，
  必须转存）；
* :mod:`app.services.media.jobs` —— 入队、常驻执行/轮询循环、取消与留存清理。
"""
from __future__ import annotations

# 作业状态。前六个与方舟任务接口的 status **同名同义**，这样重启后按上游查到的值
# 可以直接写回来，不需要中间再翻译一层；``JOB_DOWNLOADING`` 是本地的中间态——已经
# 拿到产物 URL、正在往磁盘转存，此时上游已经算终态了。
#
# ``queued`` 有两种含义，靠 ``submitted_at`` 是不是空来区分：空 = 本地排队的队、还没
# 提交；非空 = 已提交（视频在上游排队，图片那次同步调用正在进行或已崩在中途）。
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DOWNLOADING = "downloading"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_EXPIRED = "expired"

# 终态：循环不再碰它们（除了按保留期清理）。``downloading`` 故意不在这里——
# 它是「重启后要立刻接着转存」的那个状态，进程死在下载中途不该把一条已经出片的
# 作业判成失败。
JOB_FINAL_STATES = frozenset({JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED, JOB_EXPIRED})
# 已经提交给上游、要按 ``remote_task_id`` 继续查的状态。
JOB_SUBMITTED_STATES = frozenset({JOB_RUNNING, JOB_DOWNLOADING})
# 占并发位子的状态：从提交成功到产物落地，上游一直在为我占着额度。
JOB_ACTIVE_STATES = frozenset({JOB_RUNNING, JOB_DOWNLOADING})

KIND_VIDEO = "video"
KIND_IMAGE = "image"

# 为哪类业务对象跑的。值形状与 ``workflow_run.subject_kind`` 一致（小写-连字符）。
BIZ_VIDEO_SHOT = "video-shot"
BIZ_VIDEO_COVER = "video-project-cover"

# 镜头的生成方式（``video_shot.gen_mode``）。名字直接取自方舟的入参形状：
# first_frame 只喂一张首帧；first_last_frame 再加一张收束帧；reference 挂若干
# 参考图（人物/场景一致性靠它，不靠逐镜重画首帧）；t2v 什么图都不给。
MODE_T2V = "t2v"
MODE_FIRST_FRAME = "first_frame"
MODE_FIRST_LAST_FRAME = "first_last_frame"
MODE_REFERENCE = "reference"
VIDEO_GEN_MODES = frozenset({MODE_T2V, MODE_FIRST_FRAME, MODE_FIRST_LAST_FRAME, MODE_REFERENCE})

# 方舟的 image_url role 取值（``reference_audio`` / ``reference_video`` 一期用不到，
# 但入参形状在这里定齐，免得以后各处自己拼字符串）。
ROLE_FIRST_FRAME = "first_frame"
ROLE_LAST_FRAME = "last_frame"
ROLE_REFERENCE_IMAGE = "reference_image"
ROLE_REFERENCE_VIDEO = "reference_video"
ROLE_REFERENCE_AUDIO = "reference_audio"

# 画幅与分辨率的白名单：界面只允许这几个值进请求体。``adaptive`` 是让模型按首帧
# 自己定的那一档——首帧是方图就出方片，所以竖屏不靠它；1.0 系纯文生更是不支持
# adaptive 且默认 16:9。这一档必须显式传，项目级的默认在 ``schemas.VideoParams``。
VIDEO_RATIOS = frozenset({"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"})
VIDEO_RESOLUTIONS = frozenset({"480p", "720p", "1080p"})
IMAGE_SIZES = frozenset({"1K", "2K", "4K"})
