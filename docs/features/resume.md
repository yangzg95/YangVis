# 简历分析与多份对比

「办公 · 简历」里上传一条线：把 PDF / DOCX / 纯文本简历在上传时抽成纯文本存库，需要时点
「生成分析」让 LLM 读全文产出 markdown 报告与结构化改进意见；勾选 2–10 份可以发起一次
横向对比，产出一份对比报告。同一页还有「求职助手」六类一次性生成（简历优化、JD 匹配审计、
面试准备等），与分析/对比共用完全相同的执行链路。所有报告在独立的报告页新开标签页查看，
可下载 Markdown、存网盘、带着报告发起对话。在线编辑的简历工作台另见 resume-builder.md，
原件的网盘备份细节见 netdisk.md。

## 用户可见行为

- 页面四个页签（另有「在线简历」「面试记录」两条独立业务）：**简历列表**上传与单份分析、
  **对比记录**、**求职助手**（六张工具卡 + 生成记录）。
- 上传：拖拽或选文件（`.pdf/.docx/.txt/.md/.markdown`，单文件 10 MB），可填标题（留空取
  文件名）与补充描述；上传只做本地文本抽取，**不自动分析**，抽取失败或正文为空直接报错。
- 列表每行有状态标签（未分析 / 分析中 / 已完成 / 失败），失败时悬浮显示原因；点标签打开
  「认领这份简历的那次运行」的现场抽屉（渲染后的 prompt、原始输出、错误步骤）。
- 「生成分析」可重复触发（已有报告则显示「重新分析」），分析中禁止重复触发、禁止删除；
  完成后「查看报告」新开标签页，报告顶部单列「改进意见」编号列表，下方是 markdown 正文。
- 勾选至少 2 份（分析中的行不可勾）发起对比，可命名；进行中同样禁止删除。发起后不用停在
  页面等：完成时弹一条带「查看报告」按钮的通知。对比记录长期保留，事后某份简历被删，
  标题位置显示「（已删除）」，报告照常可看。
- 求职助手六类任务：简历优化、职业匹配分析、简历匹配审计、面试准备策略、证明构建计划、
  薪资最大化框架。简历来源三选一（选已上传简历 / 粘贴文本 / 直接描述背景，后两者按任务
  开放），各任务还有自己的必填项（JD、岗位、offer 年薪等），前端先拦一道、后端再校验。
- 报告页（`/office/report/:kind/:id`，kind = `resume` / `comparison` / `toolkit`）：
  改进意见列表只有 `resume` 有；三页都支持下载 `.md`、「发起对话」（新标签页打开对话，
  对话页按 kind/id 自己回拉报告全文），绑定网盘后还可一键存到网盘 `reports/`。
- 顶部有模型门槛横幅：没有验证过的默认对话模型时，分析/对比/助手入口全部禁用，并引导去
  模型设置；这个门槛在后端触发接口时还会再查一次。
- 上传时若已绑定百度网盘，原件顺带备份一份，行上出现云图标并开放「下载原件」；没绑定或
  同步失败都不影响简历保存，只是没有原件可下载。

## 关键取舍

### 业务行只存产物，状态从最近一次运行翻译

`resume` / `resume_comparison` / `resume_toolkit_task` 三张表上**没有** `status` 列
（旧列已在启动迁移中物理删除，见「升级行为」）。发起分析就是 `start_builtin()` 建一条
`workflow_run` 并以 `(subject_kind, subject_ref)` 认领这个业务对象（三种 subject 分别是
`resume` / `resume-comparison` / `resume-toolkit`）。列表序列化时用 `latest_runs()` 一次
查回每行最近一次运行，`subject_state()` 负责翻译：pending/running→分析中、
succeeded→已完成、failed/cancelled→失败（带 `error_msg`）；没有运行时按「有没有产物」
决定——简历没产物是 `uploaded`（未分析），对比/助手记录创建即带着运行，没运行又没报告
只可能是改造前的存量失败行，所以它们的 idle 态直接翻译成 `error`。反过来运行成功却没有
产物（单步重跑会造出这种组合：步跑通了但功能侧后处理没写回）也不报已完成，而是失败并
提示重新发起完整任务。删除闸门与重复触发闸门读的是同一份事实：
`running_subjects()` 只看最近一次运行是否还在 pending/running。

### 请求里建运行，BackgroundTasks 里跑模型

一次分析要等模型读完整份（对比最多 10 份）简历全文，远超前端 30s axios 超时。所以接口只
做校验、建运行、挂 `BackgroundTasks` 就返回——返回的那一刻状态已经落库，界面立刻是
「分析中」。后台任务体在**新的 DB session** 里（函数内延迟 import `SessionLocal`），跑完
只写产物列：分析写 `report` / `suggestions` / `analyzed_at`，对比与助手只写 `report`。
模型失败的现场留在 `workflow_run_step` 上，任务体只记日志；唯独「每步都成功、后处理发现
报告为空」这类功能侧失败才用 `fail_run()` 把运行降级。前端用单个 2.5s 轮询定时器同时刷
三张列表，没有分析中的行就停表；keep-alive 切走停表、回来恢复，组件卸载必清。

### 不直接调智能体，走内置工作流 slug

分析与对比不是代码里拼好 prompt 直接调模型，而是各自走一条单步内置工作流
（`resume-analysis`、`resume-comparison`，助手六类各有一条 `toolkit-*`）。分工是：人设与
输出格式要求在内置智能体上（`resume-analyzer`、`resume-compare`、`resume-optimizer` 等
slug），框材料的 prompt 骨架在内置工作流的唯一步骤上，模型仍用用户自己的默认对话模型。
收益是执行现场统一落 `workflow_run` / `workflow_run_step`（渲染后的 prompt、原始输出、
耗时都在），prompt 改进随 seed 管线重启即上线，用户也可以在「工作流」页把内置流程复制
成自己的版本改造——功能代码只认 slug，不碰 prompt 文本。代价是多一层间接：简历变量
（全文、补充描述）走运行输入传进去，不进模板 seed。

### JSON 契约只用于单份分析，且有兜底

`resume-analysis` 的那一步声明 `output_kind=json` 与 `output_keys=("report","suggestions")`，
引擎剥代码栅栏、解析成对象落在 `step.parsed`，service 只拆字段：`report` 是 markdown
正文，`suggestions` 必须是数组（字符串/对象形态一律丢弃，否则遍历字符串会把建议拆成单字）。
契约没满足（parsed 不是对象、或 report 为空）时，`_analysis_product()` 把模型原始输出整段
当报告——一份格式跑偏的报告对用户还有用，一条「解析失败」的空白页没有。对比与助手都是
普通 markdown 步，直接取 `step.output`，但空字符串会被功能侧判失败。

### 材料在发起那一刻定稿

对比在创建时就把各简历全文拼成 `=== 简历 N：标题 ===` 加分隔三引号的整块，连同简历份数
写进运行输入快照；`resume_comparison.resume_ids` 只是 id 列表。此后简历被删或被改，进行
中的对比与历史报告喂给模型的仍是当时那几份全文，报告始终成立。助手任务更进一步：发起时
就把简历来源解析成最终文本（引用简历则取库内全文、粘贴则取粘贴值，都过截断），存进
`resume_toolkit_task.inputs.resolved_resume`，任务自包含；但这个中间字段不回传前端
（详情序列化时弹出），历史记录只留「当时用了哪份简历」的 id 与标题。

### 条件段落留在 Python，模板只做变量替换

「有补充描述才加那一段」「有 JD 才加 JD 块」这类条件由 service 在组装输入变量时拼成整块
空串或整段文字（`_analysis_inputs` / `_comparison_inputs` / `_toolkit_inputs`），模板里
只有 `{{变量}}` 和固定骨架——不为模板发明一门判断小语言，拼装逻辑读 Python 最便宜。材料
段落统一用 `labeled_section()` 包三引号边界，简历正文里夹带的「请忽略上面指令」只会被当
成引号内的内容。每份简历全文截断到 `MAX_RESUME_CHARS = 12000` 字符并追加截断提示
（对比最多 10 份，留足模型窗口）；JD 等粘贴字段同样截断，pydantic 层另限 20000 字符。

### 上传：本地解析有界、全文持久化、原件交给网盘

大小上限复用知识库配置 `KB_MAX_UPLOAD_BYTES`（默认 10 MB）：先按 Content-Length 提前
拒绝（另留 1 MB 给 multipart 表单开销），再以 256 KB 分块有界读取，超限立即停读返回
`code=-1`，绝不 `await file.read()` 把整个请求体物化进内存。PDF 走 pypdf、DOCX 走
python-docx，都是本地 CPU 解析，用 `asyncio.to_thread()` 扔线程池避免卡住 async handler
的事件循环；解析器抛出的各类异常统一包装成 `UnsupportedFileType`（文案列出支持格式）。
`.txt/.md/.markdown` 复用知识库的多编码回退抽取。抽出的纯文本**持久化在 `resume.content`**：
重新分析、发起对比都要原文，不能每次重解 PDF。原始文件不落本地磁盘，唯一备份通道是用户
自己的百度网盘：上传成功后 best-effort 同步到 `resumes/{id}_{filename}`，失败只记日志；
下载刻意做成服务端代理流而不是 302 到 dlink，避免 access_token 留在浏览器历史里；删简历
时联动删网盘原件，同样 best-effort。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `Resume` / `ResumeComparison` / `ResumeToolkitTask` | 三张业务表，只存产物不存状态；`Resume.content` 留抽取全文、`netdisk_*` 两列记原件位置、`suggestions` 为 JSON 数组；对比存 `resume_ids` 快照；助手存 `kind`/`title`/`inputs` 快照 |
| `backend/app/models/schemas.py` | `ResumeItem`（列表大字段不含 content/report）/`ResumeDetail`/`ResumePreview`、`ResumeCompareRequest`（ids 2–10）、`ToolkitKind` 六值字面量与 `ResumeToolkitCreate`（字段全集，各 kind 取用） |
| `backend/app/services/resume.py` | `ResumeService`：owner 作用域 CRUD、上传后文本入库、`start_analysis`/`run_analysis`、对比与助手的快照拼装与后台任务体、`TOOLKIT_KINDS` 规则表、`extract_resume_text()`、`_analysis_product()` 兜底、运行状态→列表项的序列化 |
| `backend/app/services/uploads.py` | `content_length_exceeds()` / `read_upload_limited()`：有界上传读取（10 MB + 1 MB 表单余量，256 KB 分块） |
| `backend/app/services/chunking.py` | 纯文本格式的 `extract_text()` / `normalise()` 与 `UnsupportedFileType` |
| `backend/app/services/workflows/builtin.py` | `BUILTIN_WORKFLOWS` 目录中的 `resume-analysis`（JSON 契约步，output_keys report/suggestions）、`resume-comparison` 与六条 `toolkit-*`（markdown 步）；`start_builtin()` 建运行、`finish_builtin_step()`/`builtin_output()` 后台跑完取产物；`seed_builtin_workflows()` 启动就地刷新 |
| `backend/app/services/workflows/runs.py` | `subject_state()` 运行→状态翻译、`latest_run(s)` 批量取最近运行、`running_subjects()` 删除/重触发闸门、`fail_run()`、`reset_stale_runs()` 重启收尾、`prune_runs()` 历史清理 |
| `backend/app/routers/resume.py` | `/office` 下 resumes / comparisons / toolkit 三组接口；上传同步解析、网盘 best-effort 备份与代理下载、BackgroundTasks 调度；ValueError→`code=-1` 信封、LookupError→404 |
| `backend/app/services/agent_run.py` | `require_chat_model()` 模型门槛、`labeled_section()` 材料边界、`model_error_text()` |
| `frontend/src/views/Resume.vue` | 四页签整页：上传/编辑弹窗、对比确认弹窗、TOOL_DEFS 驱动的助手表单、预览抽屉、2.5s 轮询与完成通知、状态标签打开运行现场抽屉 |
| `frontend/src/views/ReportView.vue` | 三类报告共用整页 `/office/report/:kind(resume|comparison|toolkit)/:id(\\d+)`：改进意见列表（仅 resume）、markdown 渲染（`html:false`）、下载、存网盘、发起对话 |
| `frontend/src/components/WorkflowRunsDrawer.vue` | 状态标签点开的运行现场（按 subject 查，不绑定具体工作流） |

### 接口一览（均在 `/office` 前缀下，需登录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/resumes` | 列表 / 上传（multipart：file、title、description）；超限、格式不支持、正文为空均 `code=-1` |
| GET/PUT/DELETE | `/resumes/{id}` | 详情（带报告）/ 改标题描述 / 删除（分析中拒绝，并联动删网盘原件） |
| GET | `/resumes/{id}/preview` | 上传时抽取的纯文本，预览抽屉专用 |
| GET | `/resumes/{id}/download` | 经网盘代理流式下载原件；未同步网盘 404 |
| POST | `/resumes/{id}/analyze` | 模型门槛检查后建分析运行并挂后台任务 |
| POST/GET | `/comparisons`、GET/DELETE `/comparisons/{id}` | 发起（ids 2–10）/列表/详情（带报告）/删除（进行中拒绝） |
| POST/GET | `/toolkit`、GET/DELETE `/toolkit/{id}` | 六类生成任务的发起/列表/详情/删除 |

## 升级行为

- `migrate_schema()` 物理删除 `resume`、`resume_comparison`、`resume_toolkit_task` 三张表
  遗留的 `status` / `error_msg` 列：状态改由运行翻译后这两列再无写入者，而旧表结构里它们
  是 NOT NULL 且无 DB 默认值，留着会让新插入直接报错。存量行的状态靠 `subject_state()`
  「无运行 + 有产物即已完成」在读取时自然延续，不做数据回填。
- 旧库缺 `resume.netdisk_fs_id` / `resume.netdisk_path` 时幂等补列；SQLite 测试库由
  `create_all` 直接建成新结构。
- 每次启动 `seed_builtin_workflows()` 就地刷新八条内置流程：步骤按 `key` 对齐覆盖、消失的
  删除，prompt 与结构改进随发布上线；`enabled` 不动（藏起流程是用户自己的决定），用户复制
  出去的同名流程不在刷新范围内。
- 启动 `reset_stale_runs()` 把上个进程遗留在 pending/running 的运行（含简历三类 subject）
  统一收尾为失败，原因固定为「服务重启导致本次运行中断，请重新发起」，用户随即可以重新
  触发；更早、又不是任一业务对象最近一次的运行由 `prune_runs()` 按 30 天窗口清理。

## 测试

- `backend/tests/test_resume_service.py`（service 层，SQLite 文件库，不打模型）：
  上传解析——纯文本多编码规整、真实 DOCX 逐段读取、坏 PDF 报「解析失败」、不支持格式的
  报错列出接受格式、扩展名白名单、截断只在超长时加标记；助手规则表——六类标题取值与 128
  字符截断、`_toolkit_inputs` 拼出的变量与各内置工作流声明**逐一对上**（模板里不能剩
  `{{变量}}`）、条件段落有无、粘贴材料包三引号边界、JD 截断、未知 kind 在落表前拒绝、
  规则表与 pydantic `ToolkitKind` 一致；各类必填字段与简历来源（必选/可选/背景二选一）
  校验、来源解析进快照、建出的运行认领对应 slug 的工作流、他人/缺失简历读作不存在、来源
  删除后任务自包含；对比——发起时材料冻结、重复 id 拒绝、owner 隔离、进行中禁止删除；
  简历——按新到旧排序、更新只写传入字段且空描述清空、分析中禁删与禁重复触发、运行输入
  带上材料与可选描述段、网盘标记回流 item、分析与对比共用同一道模型门槛。
- `backend/tests/test_resume_workflow.py`（打桩 `workflows.exec.run_agent` 的集成测试）：
  `_analysis_product` 读契约字段 / parsed 非对象或无 report 时回退原文 / 非列表
  suggestions 丢弃；分析与对比真实走工作流引擎执行、写回 parsed 字段；状态断言全部走
  `service.to_item()` 而非读实体；分析中禁删禁重发；重启后没有简历永远卡在分析中；无补充
  描述时该段不进 prompt；工作流失败时运行与行状态标错；内置流程缺失时在触发当口报错。
- 路由层（multipart 上传、BackgroundTasks 调度、网盘代理流）目前没有独立 HTTP 测试，
  覆盖集中在 service 与引擎两层；`test_tool_resume.py` 针对的是在线简历工作台的对话本地
  工具 `get_resume_doc`，不属于本文链路。
