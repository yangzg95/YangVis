# 工作流运行

一次运行的生命周期：发起时（`create_run`）先落一条 `workflow_run`（pending）并把
这条流程当前定义里的**每一步**的 `workflow_run_step` 行一次建齐（全 pending），随后
执行器 `execute_run` 按步骤组调度——组间顺序、组内并发，每一步一进来就先把行提交成
running，再渲染 prompt、解析实际要用的模型配置、调 `run_agent`、按输出契约解析，终态
（succeeded/failed）连同渲染后的 prompt、原始输出、parsed、耗时、模型名、人设版本与
步内 trace 即时落库。非可选步骤失败就停在这一组，没轮到的步标 skipped。跑的过程通过
SSE 往外推，但进度的事实源永远是数据库：关掉页面运行照跑，中途随时可以用 follow 流
接上。终态运行上可以单步重跑（只重做这一步）或从某步断点续跑（目标组及之后整段重
跑），历史则由 prune 按保留窗口清理。步骤怎么定义见 workflow-designer.md，双运行
对比见 workflow-eval.md，人设版本绑定见 agent-versions.md，步内执行过程的 trace
节点模型见 unified-trace.md。

## 用户可见行为

- 工作流列表卡片上有「最近一次运行」：状态 tag + 时间/耗时一行摘要，从没跑过显示
  破折号；同一屏一次查询取回，不是每行一问。
- 查历次运行有两个外壳，里面是同一份面板：业务页（简历、幻灯片、面试详情）右侧
  抽屉，按「这个对象跑过哪几次」收敛，打开默认展开最近一次；工作流页点「运行」
  新开应用内标签，列这条流程的全部运行，给发起入口。
- 历次运行列表支持按状态筛选、分页（每页 20，「共 N 次」是筛选后的总数）；每行有
  `#id`、发起时间、业务对象名快照（手动运行没有）、各步实际用到的模型名（去重）、
  总耗时、状态 tag，以及业务对象深链、「完整现场」「对比」入口。
- 手动发起：必填输入平铺、选填默认折叠（折叠行报「已填 N」），开始后提示
  「关掉页面不会中断运行，进度随时回来接着看」。
- 实时进度区：运行状态 + 每步一行（状态、key、人设、可选标记、实际模型名、输出
  字数、耗时、错误），步下实时累积「执行过程」（模型往返/工具调用，详见
  unified-trace.md）；可以「停止跟随」，停的只是观察流。
- 逐步现场（抽屉内展开或整页查看）：运行输入快照、每步可折叠（失败步默认展开），
  步内分「执行过程 / 输入（上下文变量 + 渲染后发给模型的 prompt）/ 输出（模型原始
  输出 + JSON 步的 parsed 对象）」三组，每块文本可复制；步头有 group_key、人设版本
  标与实际模型名。
- 跑挂的步（以及任何终态运行的任意步）可以「重跑这步」或「从此步继续」，提交前
  弹框调整这一步收到的上下文变量（JSON 产物按 JSON 编辑）；两处都明示不写回业务
  结果——要新产物请回功能页面重新发起完整任务。
- 列表刷新时若最新一次还在 pending/running，自动接 follow 流；单步重跑/续跑提交后
  也立刻接上。用户明点过「停止跟随」后，本次打开不再自动接回；切走标签/关抽屉只
  断流不撤忙线，切回来发现还在跑会重新接上。
- 续跑沿用同一条运行记录：开始时间保留，耗时在原值上累加；现场头部那行时间因此始终
  对得上「这条任务最初是什么时候发起的」。
- 抽屉被简历（`Resume.vue`）、幻灯片（`Slides.vue`）、面试详情
  （`InterviewDetailView.vue`）三处复用，标题与过滤都按当前业务对象走，不给发起
  表单——那条流水线由功能自己在发起时跑。
- 工作流页有「清理运行记录」：填保留天数手动 prune；同一口径服务启动时全站自动跑
  一次。重启时还挂着的在跑运行统一收尾为失败（「服务重启导致本次运行中断，请重新
  发起」），不会永远转圈。

## 关键取舍

### 并行组怎么调度

步骤按 `group_key` 由 `_group_order` 切成有序的组：组之间严格顺序，同组步骤用
`asyncio.gather` 并发。每个并发步骤**自己开一条 SessionLocal**——SQLAlchemy
Session 不支持并发共享，同组步骤写各自的行，共用请求 session 会互相覆盖。执行器用
一个 asyncio 队列收口各步事件，组驱动协程跑完后投 `_GROUP_DONE` 哨兵，所以同组的
`step_start/step_end` 按各步实际完成先后到达，不保证成对相邻。

组跑完才把产物合并进上下文：成功步以 `output_var` 放入（JSON 步放 parsed 对象，
模板里 `{{x.字段}}` 才取得到），可选步失败放**空串**（下游模板那句「如果有配图建议
就附上」自然不附上，optional 不会形同虚设）；组内出现非可选失败步则不再跑后面的
组，所有仍处 pending/running 的步骤行批量标成 skipped——「没轮到」和「跑挂了」在
历史里分得开。MCP 工具只连「工作流级挂载 ∪ 待执行步挂载」的并集，同一服务只
initialize 一次，连接失败发生在首事件之前、运行直接落 failed。

### 单步重跑为什么重绑最新版本

两条路都是「prepare 在请求里把行打回 pending，执行交给路由后台任务」。闸门都在
prepare：还在跑的运行拒绝（409，两个执行体会抢同一行）；流程当前定义里已经没有的
步骤拒绝——prompt 模板只存在定义里，步骤行只存渲染出来的那一份。重跑用的是
**当前定义**的模板、开跑这一刻重新解析的模型配置与人设版本（绑定规则见
agent-versions.md），上游上下文则由 `_rebuild_context` 按**当时落库的步骤行**
重建，而且只带严格早于目标组的产物——同组并行兄弟第一次跑时也看不见彼此，重跑要
复刻的就是当时那张 prompt。

- 单步重跑只重置这一步的现场行，整条运行回 running；跑完由 `settle_run_status` 按
  全部步骤行重算：还有 failed 则 failed；这一步之后存在没跑的步（skipped/pending）
  也算 failed，报「重跑的这一步之后还有 N 步没有跑」——想顺着跑完请用续跑。
- 断点续跑重置的是**目标组及之后各组的全部行**：并行组不可分割，没法只接组里一步
  再往下。续跑保留原始 `started_at`，本段耗时累加在原 `elapsed_ms` 上。
- `inputs` 覆盖值只影响这一次执行：prepare 期先校验（变量名必须在这一步的上下文
  里、JSON 产物的覆盖值必须是合法 JSON，非法一律 400），不写回运行输入快照也不改
  上游行；报错发生在行被重置之前，不留半成品。
- 重跑/续跑都不重写业务行上的产物（后处理在功能侧，只认整条流程跑完）。业务状态由
  `subject_state` 兜：运行 succeeded 却没有产物时显示明确错误，不会出现一页空白的
  「已完成」。

### follow 为什么不补 trace

`follow_run` 一个东西都不执行，只读 `workflow_run_step` 里已落库的事实：开头先
`commit()` 结束自己的旧事务（写行的是后台任务那条连接，留在旧快照里永远读到
pending），发一帧带每步当前状态的 `run_start` 回放现状，之后每 1.2 秒轮询一次，只
推状态发生变化的步——定下来的推 `step_end`，被重跑打回 pending 的推 `step_start`
先清掉旧显示。事件名与载荷形状跟 `execute_run` 完全一致，前端只为 SSE 写一套解析。
follow 不补推 `step_trace`：接上时步往往已在跑，推不全反而是误导；跑完后现场随详情
接口重取，trace 一行不少（只有发起那条实时流推 `step_trace`，详见
unified-trace.md）。另带一道自愈：所有步骤都定下来了、运行行却还停在 running（执行
体半路没了），follow 按步骤行结算一次，不让界面永远转圈。接上时运行已经终态也
是合法用法：回放一帧 `run_start` 后立刻收一帧 `run_end`，打开历史详情不会挂着一条
空流；运行记录在轮询间隙被 prune 删掉，则以 404 的 `error` 事件收流。

### prune 规则

`prune_runs` 删一条运行要**同时**满足三条：

1. `created_at` 早于保留窗口（默认 `RUN_RETENTION_DAYS = 30` 天，手动入口传入
   `keep_days`）；
2. 状态不在 pending/running——在跑的删掉只留下没人跑完的步；
3. 不是任一业务对象（按 `subject_kind + subject_ref`）id 最大的那次运行——业务行的
   状态就是从最近一次运行翻译出来的，删了等于删掉那份简历/deck 现在的状态。

「最近一次」在**全表**里按 `max(id)` 单查，不能在过窗候选集里比，否则会把该删的
老运行算成该留的那次。没有认领业务对象的手动运行没有状态要担，过窗口就删。评测
引用（A 或 B）是第四条硬豁免，详见 workflow-eval.md。删除先删步骤行再删运行行，
返回 `(删除运行数, 删除步骤数)` 并记日志。手动接口只清调用方自己的（owner 收敛），
启动时 `bootstrap.prune_workflow_runs` 跑全站口径，并排在 `reset_stale_runs`
之后——先把上个进程遗留的 running 收成失败，再按窗口清。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/workflows/defs.py` | `create_run`（输入校验、建运行与全量 pending 步）、`list_runs`/`get_run`、`step_models`/`run_model_names`（整页一次查模型名）、`to_run_item`/`to_run_detail`（步的 agent_name/output_kind/optional 按 key 对当前定义，对不上按行显示）、`last_runs`、`require_chat_config` |
| `backend/app/services/workflows/exec.py` | `execute_run`（分组调度、run 终态落库）、`_execute_step`（独立 session、running 即时提交、渲染 prompt、模型解析、`run_agent`、契约解析）、`_finish_step`、`_rebuild_context`、`_group_order` 调度 |
| `backend/app/services/workflows/stream.py` | `stream_run`（执行挂在 `fire_and_forget` 自己的任务上，SSE 只转发，异常兜底落终态）、`follow_run`/`_follow_state`（`FOLLOW_POLL_SECONDS = 1.2`，只读轮询、卡死自愈） |
| `backend/app/services/workflows/rerun.py` | `prepare_step_rerun`/`prepare_resume`（闸门与行重置）、`rerun_step`/`resume_run`（后台执行）、`_coerce_overrides`/`_context_fields`、`OverrideValidationError` |
| `backend/app/services/workflows/runs.py` | `reset_stale_runs`（重启收尾）、`prune_runs`（窗口 + 两条硬豁免 + 评测豁免）、`settle_run_status`（按全部步重算）、`latest_runs`/`subject_state`/`running_subjects`/`fail_run`（运行 ↔ 业务对象状态翻译） |
| `backend/app/services/workflows/builtin.py` | `start_builtin`（功能侧在请求里建运行并认领 subject）、`finish_builtin_step`/`builtin_output`（后台跑完取产物步） |
| `backend/app/routers/workflows.py` | 运行相关路由（下表）；prepare 异常映射 404/400/409；两条流都套 `with_heartbeat` |
| `backend/app/models/entities.py` | `WorkflowRun`（inputs 快照、subject 三元、状态/耗时/时间戳）、`WorkflowRunStep`（prompt/output/parsed/trace/model_config_id/agent_id/agent_version 等现场列） |
| `backend/app/models/schemas.py` | `WorkflowRunItem`/`WorkflowRunDetail`/`WorkflowRunStepItem`、`WorkflowRunRequest`、`WorkflowRerunRequest`、`WorkflowRunPruneRequest`/`Result`、`RunStatus` 字面量 |
| `backend/app/bootstrap.py` | 启动序列：`reset_stale_jobs`（含 `reset_stale_runs`）→ `prune_workflow_runs` |
| `frontend/src/views/Workflows.vue` | 列表卡片「最近一次运行」列、「清理运行记录」popconfirm（保留天数） |
| `frontend/src/views/WorkflowRunsPage.vue` | 「运行记录」整页（keep-alive 单实例，切走 suspend 断流、切回 resume 重取） |
| `frontend/src/views/WorkflowRunView.vue` | 单次运行完整现场整页，在跑自动 follow，重跑/续跑弹框 |
| `frontend/src/components/WorkflowRunsDrawer.vue` | 业务页抽屉外壳（简历/幻灯片/面试详情使用），内容全在面板 |
| `frontend/src/components/workflow/WorkflowRunsPanel.vue` | 发起表单 + 实时进度 + 历次列表 + 现场折叠 + 自动 follow/停止跟随；抽屉与整页共用 |
| `frontend/src/components/workflow/RunScene.vue` | 每步现场：运行输入、执行过程/输入/输出三组、重跑与续跑按钮、失败步默认展开 |
| `frontend/src/components/workflow/StepOverrideModal.vue`、`runFields.ts`、`runLinks.ts` | 覆盖变量弹框；可编辑上下文变量还原（与 `_rebuild_context` 同口径的纯函数）；业务对象深链 |
| `frontend/src/api/workflows.ts`、`frontend/src/api/sse.ts` | 接口与 SSE handler（`onRunStart/onStepStart/onStepTrace/onStepEnd/onRunEnd/onError`） |

### 运行相关接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /workflows/{workflow_id}/runs/stream` | 发起一次运行，SSE 流；建记录前先过模型门槛，前置失败也以 `error` 事件回 |
| `GET /workflows/runs/{run_id}/stream` | 中途跟随：纯观察流（GET），404 在响应成流之前给出 |
| `GET /workflows/runs` | 运行清单：`workflow_id` 或 `subject_kind`+`subject_ref`（成对）收敛、`status` 筛选、分页 |
| `GET /workflows/runs/{run_id}` | 运行现场快照（输入 + 每步 prompt/output/parsed/trace/模型名） |
| `POST /workflows/runs/{run_id}/steps/{key}/rerun` | 单步重跑，立即返回 `WorkflowRunItem`，进度走 follow |
| `POST /workflows/runs/{run_id}/steps/{key}/resume` | 从该步所在组续跑，立即返回，进度走 follow |
| `POST /workflows/runs/prune` | 手动清理，体 `{keep_days}`（1–3650），返回删除计数 |

## SSE 事件清单

两条流（发起、跟随）事件名与载荷同构；流外每 15 秒一帧注释心跳 `: ping`。

| 事件 | 何时发 | 载荷关键字段 |
| --- | --- | --- |
| `run_start` | 流的第一帧 | `run_id`、`workflow_slug`、`steps[]`（`key`、`group_key`、`agent_slug`、`agent_name`、`output_kind`、`optional`）；跟随流额外带运行 `status`，且每个步带当下 `status` 与已确定的 `model_name`（发起流里步全是 pending、无模型名） |
| `step_start` | 步开始（跟随流里步被重跑打回 pending 也发） | `key`、`group_key`；跟随流补 `status`，客户端据此清掉上一次显示 |
| `step_trace` | 步内每个模型往返/工具起止，**仅发起流** | `key`、`group_key`、`node`（`llm`/`tool_start`/`tool_end` 扁平事件，形状见 unified-trace.md） |
| `step_end` | 步定下终态 | `key`、`status`（succeeded/failed/skipped）、`error`、`output_chars`（正文不进事件，走详情接口取）、`elapsed_ms`、`model_name`；发起流还带 `parsed` |
| `run_end` | 运行进终态，流结束 | `run_id`、`status`、`error`、`elapsed_ms`（续跑为累计值） |
| `error` | 前置拒绝、执行崩溃或跟随失败 | `code`（业务码/`-1`，跟随流记录消失为 404）、`message` |

## 升级行为

- `workflow_run` / `workflow_run_step` 建表语句在 `docs/schema.sql`，既有库走
  bootstrap 的幂等 ALTER（如 `workflow_run_step.agent_version`，见
  agent-versions.md）；`trace` 列的升级口径见 unified-trace.md。
- 老运行没有 trace 列数据时，现场不出现「执行过程」分组；`agent_id/agent_version`
  为 NULL 时不显示人设版本标；步骤引用的模型配置事后被删，`model_name` 为 None，
  列表与现场都留空不报错。
- 流程定义事后被改/删步：详情接口按 key 对当前定义补 `agent_name/output_kind/
  optional/output_var`，对不上的步这几项落空、按步骤行本身的现场展示——现场的主人
  是那次运行，不是当前定义；但这样的步不能再重跑/续跑（模板已不存在）。
- 服务重启只把遗留的 pending/running 收成 failed（运行与在跑步同样处理，原因固定为
  `STALE_RUN_ERROR`），已结束的历史行一个不动。

## 测试

- `backend/tests/test_workflows.py`：执行与落库主链路——`test_execute_run_streams_and_persists_each_step`、
  并行组、可选步失败继续、非可选失败后序标 skipped、JSON 契约解析与缺字段失败、
  `run_model_names` 按步聚合/没跑到的步跳过；流——`test_stream_run_finishes_the_run_when_the_consumer_leaves`
  （断流照样落终态）、执行崩溃落 failed、模型门槛先于首事件、MCP 束并集与节点级
  工具子集；follow——回放现状、只推错过的变化、卡死自愈、步被打回 pending、记录被
  prune 后收流、客户端离开断流；重跑——prepare 清现场并拒绝在跑运行/缺失步、按落库
  产物重建上下文、用当前定义、并行兄弟不可见、后续没跑报 N 步未跑、模型失败结算、
  连接束取并集；`reset_stale_runs` 两个；`prune_runs` 五个（保留业务对象最近一次、
  保留在跑与近期、owner 收敛、窗口、无主运行过窗即删）；`subject_state` 无产物不报
  ready；`last_runs` 与详情组装。
- `backend/tests/test_workflows_api.py`：`/runs` 不被 `/{workflow_id}` 抢匹配、
  subject 成对过滤、分页与状态过滤；发起流的事件与现场、缺模型/未知输入的 error
  事件；跟随流回放与 owner 隔离；rerun/resume 两个接口的 happy path 与在跑 409；
  prune 手动入口保留每对象当前运行、非法窗口 422。
- `backend/tests/test_workflow_resume.py`：续跑专测——只重置目标组及之后、失败组
  修复后续跑、覆盖值生效与非法覆盖拒绝、从第一组续跑等于全跑、prepare 闸门、单步
  重跑带覆盖值。
- `backend/tests/test_workflow_flows.py` / `test_resume_workflow.py`：简历、幻灯片、
  面试题、求职助手通过 `start_builtin`/`finish_builtin_step` 触发运行的端到端口径，
  含业务状态来自最近一次运行、在跑挡住删除与重复触发。
- `backend/tests/test_workflow_evals_api.py`：评测引用的 prune 硬豁免与解除（规则
  见 workflow-eval.md）。
- `backend/tests/test_agent_versions.py`：开跑时绑定当时人设版本的写入（见
  agent-versions.md）。
