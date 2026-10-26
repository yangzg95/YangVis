# 统一 Trace 时间线

对话回答、工作流运行、运维排查三处都在展示「模型这一轮干了什么」，但此前各管
各的：对话气泡的思考过程只覆盖流式期间的工具调用，工作流步骤是个打不开的黑盒
（`run_agent` 内部调了哪些工具、每轮模型多久全看不到），运维面板又自己抄了一份
时间线映射。本次把三处统一成**同一套 trace 节点模型与同一组渲染组件**，并补齐了
两个一直缺失的信息：每次模型往返的耗时，以及工作流步骤内部的工具调用现场。

## 用户可见行为

- 对话气泡的「思考过程」里，每一轮没有工具调用的模型往返也会出现一行
  「模型思考 · 1.2 秒」；工具行右侧新增本次调用耗时，悬停可见开始时刻。
  纯问答（不调工具、没有思维链）落库后也保留这次模型往返的耗时。
- 工作流「每步现场」新增**执行过程**分组（失败步默认展开、成功步默认收起）：
  按发生顺序列出该步内部的每次模型往返与每个工具调用，工具可展开看入参与
  返回结果（失败红叉），与对话页的思考过程长得一模一样。老运行没有这组数据，
  分组整块不出现。
- 运行进行中跟随进度时，每一步下面实时累积执行过程，不必等跑完再翻详情。
- 运维排查面板维持原有的命令时间线，不插入「模型思考」行（那里只关心命令）。

## 统一节点模型

三条来源的存储形状不同，渲染前都在 `frontend/src/utils/traceModel.ts` 适配成
同一组 `TraceNode`：

| 节点 | 形态 |
| --- | --- |
| `reason` | 思维链全文（推理模型才有） |
| `llm` | 一次模型往返，只有耗时 |
| `tool` | 一次工具调用：状态（running/done/error）、标题、耗时、开始时刻、入参/返回块 |

- 对话侧（`chat_message.timeline`）：在原有 `text/reason/step/confirm` 条目上
  给 `step` 补 `started_at`（`db_now()` ISO 字符串）与 `elapsed_ms`，并新增
  `{type:'llm', elapsed_ms}` 条目；适配时过滤掉正文 `text` 与确认提议
  `confirm`（它们不属于「过程」）。
- 工作流侧（新列 `workflow_run_step.trace`）：存的是**扁平事件流**
  `{kind:'llm'}` / `{kind:'tool_start'}` / `{kind:'tool_end'}`，适配层按工具名
  把 start/end 配对成一个工具节点（同名并发按 LIFO 配对）；流结束仍未闭合的
  start 保留为 running（步骤被中断时正是这个形态），找不到归属的 end 用自身
  补一个完成节点。

## 关键取舍

### 不新建 trace 总表

统一的是**节点形状与渲染**，不是存储。对话侧复用既有 `chat_message.timeline`
JSON 扩字段，工作流侧只给 `workflow_run_step` 加一个 `trace JSON` 列。没有跨域
全局检索、全局时间线这类需求，建总表只会带来双写与一致性问题。

### 回调而不是重写 run_agent

`run_agent` 新增 `on_event: async (node) => None` 可选回调，在每次
`bound.ainvoke` 前后与每个工具 `ainvoke` 前后发节点；不传时行为零变化
（`_emit` 第一行就 return），所有既有调用方不用改。回调异常只记 warning，
绝不能炸掉这次模型调用——trace 是观测通道。工具入参进事件前先
`_json_safe`（试 `json.dumps`，失败退 `str(value)`），保证能落 JSON 列。

### 工作流失败步也保留 trace

步骤在工具调用之后才失败时，已经发出的轨迹随终态一起写进 `trace` 列
（`row.trace = trace or None`），排查时正要看它挂在了步内的哪个动作上。

### 跟随流不推 step_trace

`follow_run`（中途接上的轮询流）仍只按步粒度回放状态，不补推 trace：接上时
步骤往往已在跑，推不全反而是误导；跑完后现场随详情接口重取，trace 一行不少。
只有发起那条实时流推 `step_trace`。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/agent_run.py` | `TraceEventCallback`、`_emit`/`_json_safe`/`_elapsed`；`run_agent(..., on_event=None)` 在无工具分支与工具循环每轮发 `llm`，每个工具发 `tool_start`/`tool_end`（含 `is_error`/`elapsed_ms`） |
| `backend/app/services/chat/service.py` | astream_events 新增 `on_chat_model_start/end` 处理（按 run_id 计时，发 `llm` 帧并落 timeline）；`on_tool_start` 带 `started_at`，`_attach_step_result` 回填 `elapsed_ms` |
| `backend/app/services/workflows/exec.py` | 每步收集 `trace_nodes`，`on_trace` 既累积又透传 `("step_trace", {key, group_key, node})`；`_finish_step` 终态写 `row.trace`（失败也写） |
| `backend/app/models/entities.py` / `schemas.py` / `bootstrap.py` / `docs/schema.sql` | `WorkflowRunStep.trace` JSON 列、`WorkflowRunStepItem.trace`、幂等 ALTER 与建表语句 |
| `frontend/src/utils/traceModel.ts` | `TraceNode` 模型与 `chatTraceNodes` / `workflowTraceNodes`（含 start/end 配对） |
| `frontend/src/components/trace/TraceNodeList.vue` | 统一节点行（状态图标/标题/耗时/可展开入参返回/复制） |
| `frontend/src/components/trace/TraceTimeline.vue` | 对话气泡用的可折叠外壳（「N 次工具调用 / M 次失败」摘要） |
| `frontend/src/components/ThinkingTrace.vue` | 改成薄壳：`chatTraceNodes` + `TraceTimeline` |
| `frontend/src/components/workflow/RunScene.vue` | 每步现场新增「执行过程」分组（紫色 tag，失败步默认展开） |
| `frontend/src/components/workflow/WorkflowRunsPanel.vue` | 实时进度每步下挂 `TraceNodeList`，`onStepTrace` 累积进 `live.steps[].trace` |
| `frontend/src/components/OpsChat.vue` | 历史 timeline 映射时显式过滤 `llm`（运维命令时间线不收模型往返） |
| `frontend/src/api/sse.ts` / `chat.ts` / `workflows.ts` | `onLlm`/`onStepTrace` 回调与派发；`TimelineEntry` 新字段与 `'llm'` 类型；`WorkflowTraceEvent` 与 `WorkflowRunStep.trace` |

## 升级行为

- `workflow_run_step.trace` 走 `migrate_schema` 幂等 ALTER；SQLite 测试库由
  `create_all` 直接建出。
- 老运行该列为 NULL，界面不出现「执行过程」分组；老消息没有 `llm` 条目与
  耗时字段，思考过程与升级前完全一致（字段缺失按「无耗时」渲染）。

## 测试

- `backend/tests/test_mcp_tools.py` 新增 5 个：无工具单发 `llm`；工具轮
  `llm → tool_start → tool_end → llm` 全字段；工具失败 `is_error`；不可序列化
  入参退字符串；回调抛错不影响主流程。
- `backend/tests/test_workflows.py` 新增 2 个：`step_trace` 事件夹在步的
  start/end 之间且节点列表落 `row.trace`、run detail 带 trace；失败步保留
  已发出的 trace、未跑的步为 NULL。
- `backend/tests/test_chat_service.py` 新增 2 个：工具轮的 llm 帧序、step 的
  started_at/elapsed_ms 与落库 timeline 同构；纯问答也落 `llm` 节点。
- 六个测试文件里替换 `run_agent` 的桩统一补 `on_event=None` 形参。
- `frontend/src/utils/traceModel.test.ts`（9）：两类适配的过滤/耗时/状态、
  同名 start 的 LIFO 配对、未闭合 start 保留 running、孤立 end 补节点。
