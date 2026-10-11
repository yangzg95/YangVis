"""工作流层共用的词汇表：运行与步骤的状态值、业务对象 kinds。

一条工作流 = 若干**步骤组**，组之间顺序执行、组内并行；每个步骤就是一次
:func:`app.services.agent_run.run_agent` 调用。这是把简历、幻灯片、面试那些
「功能里硬编码的一次模型调用」搬进配置的地基。

状态值跟 ``resume.status`` 一样用裸字符串：实体和 schema 里都没有枚举，加一层
只让读写两边各多一套转换。这里放的是这一层的**取值词表**：一套运行状态、一套
步骤状态、一套业务对象 kinds——定义存取、执行器、运行翻译和简历幻灯片那些功能
读的都是同一批字面量，跟着实现拆走就会各写一份。模块自己用的调参、校验正则与
翻译表则跟着实现走。实现按职责在包内的这些模块里——这一层没有唯一的入口类，
所以 `__init__` 只当词表、不做转发门面，调用方按子模块取名字：

* :mod:`app.services.workflows.builtin` —— 内置工作流目录、启动时播种，以及
  业务侧「一键起一个内置运行」的入口；
* :mod:`app.services.workflows.defs` —— 定义存取（工作流、步骤、运行详情）；
* :mod:`app.services.workflows.template` —— ``{{name}}`` 模板渲染与步骤分组；
* :mod:`app.services.workflows.exec` —— 执行器：逐组跑步骤、写运行现场；
* :mod:`app.services.workflows.runs` —— 运行与业务对象之间的状态翻译、遗留
  运行收尾与留存清理；
* :mod:`app.services.workflows.stream` —— 脱离响应把运行跑完并流式回放，以及
  中途接上正在跑的运行；
* :mod:`app.services.workflows.rerun` —— 单步重跑与断点续跑。

依赖是单向的：template 谁都不接，runs 只接这里的词汇表；defs 与 exec 接 template，
stream 接 exec 与 runs，rerun 接 exec、runs 与 template，builtin 接 defs 与
exec；没有回边。
"""
from __future__ import annotations

# 运行与步骤的状态值。跟 ``resume.status`` 一样用裸字符串：实体和 schema
# 里都没有枚举，加一层只让读写两边各多一套转换。
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

# 一次运行为哪个业务对象跑的，写进 ``workflow_run.subject_kind``。值的形状与
# workflow.slug 一致（小写-连字符），业务侧只认这几个名字。
SUBJECT_RESUME = "resume"
SUBJECT_RESUME_COMPARISON = "resume-comparison"
SUBJECT_RESUME_TOOLKIT = "resume-toolkit"
SUBJECT_SLIDE_DECK = "slide-deck"
SUBJECT_INTERVIEW_QUESTION = "interview-question"

# 业务行的状态词表：以前直接长在业务表上，现在由最近一次运行翻译出来。
# 取值一个都没改——前端那几套 statusText 映射不用跟着动。
SUBJECT_UPLOADED = "uploaded"  # 还没有过任何运行
SUBJECT_ANALYZING = "analyzing"
SUBJECT_READY = "ready"
SUBJECT_ERROR = "error"

