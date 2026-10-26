# 首页工作台（Home Dashboard）

登录后落地的第一屏。要解决的问题很朴素：各模块分散在十几个路由里，用户进来
不知道自己有什么、从哪下手。首页一次给出三件事——按时段变化的问候与快捷动作、
调用方名下各模块的资源数量、最近在聊的会话；另外把「对话模型还没通」这种会让
后续多个页面一起扑空的配置缺口，提前在首页讲清楚。

## 用户可见行为

- 顶部问候区：按本机小时显示「早上好/中午好/下午好/晚上好/夜深了」+ 显示名，
  副标题是当天日期与星期；右侧「开始对话」「上传资料」两个快捷按钮。
- 「功能模块」卡：八张可点瓦片——对话（个会话）、知识库（份文档）、智能体
  （个智能体）、工作流（条流程）、简历（份简历）、幻灯片（份幻灯片）、
  服务器（台服务器）、数据库（个连接）。数字与名称同一行右对齐，概览没回来
  之前每张卡亮骨架条而不是闪成 0。
- 两张瓦片带副信息：工作流下面是「今日 N 次运行」，简历下面是「面试记录 N」。
  知识库瓦片的主数字是**文档数**（`knowledge_document_count`）；项目数
  （`knowledge_project_count`）接口有返回但首页不单独占一格。
- 管理员在末尾多一张「AI 网关」卡：主数字是**今日调用数**（次调用），副信息
  「今日 N tokens · M 个通道」；普通用户这张卡根本不渲染。
- 右侧「最近对话」卡：按更新时间倒序取最多 10 条（`RECENT_LIMIT`，前端切片），
  每条显示标题、消息数与相对时间（刚刚/N 分钟前，超一周回落日期），底部一行
  「共 N 个会话，显示最近 M 条」；点一条跳到 `/chat?conv=<id>`，由对话页的
  深链处理打开该会话并清掉 query。空列表显示「还没有会话」。
- 连通性门：没有可用的默认对话模型时，问候区下方亮黄色警告条「未配置可用的
  对话模型」，「去配置」按钮直达 `/system/settings`。对话、简历分析、幻灯片
  生成共用这一个前提，所以门放在首页而不是各页各自拦。
- 首页挂在 keep-alive 里：`onMounted` 并发拉三路数据，之后每次 `onActivated`
  （首次激活除外）重新拉一遍，避免从别的页新建完会话回来数字还停在旧值。

## 关键取舍

### 会话口径排除运维页内嵌问答

`conversation_count` 只数 `chat_conversation.ops_target_type IS NULL` 的行，
与主对话侧栏 `ChatService.list_conversations(include_ops=False)` 完全同一条
 WHERE——绑定了 server/database 目标的问答会话属于运维页面的嵌入面板，混进
首页数字和侧栏都只是噪音。首页「最近对话」直接复用 `GET /api/chat/conversations`
 列表，口径天然一致，不另造查询。

### 内置资产算进每人的计数，停用的不算

内置智能体与内置工作流挂在 `owner_id=0` 上、对所有人可用，所以计数条件是
`owner_id IN (0, 我)` 且 `enabled = TRUE`：不把内置算进来，选择器里明明有好几个
可用助手、卡片却显示「智能体 0」；停用的界面上不可见，数进来同样对不上号。
其余模块（知识库、简历、幻灯片、服务器、数据库连接）一律纯 `owner_id` 隔离。

### 网关是平台级资产：只对管理员出数，且只给今日口径

`ai_channel`、`ai_call_log` 没有 `owner_id`，口径本身就是全局的，三项网关字段
（`ai_channel_count` / `ai_call_count_today` / `ai_token_count_today`）只在
`is_admin` 时填充，非管理员保持 `None` 而不是 0——0 会被读成「网关还没接通道」，
`None` 才是「无权查看」，前端按此决定卡片出不出现。今日按**服务器本地零点**
（`datetime.now()` 替换时分秒）切窗，tokens 取 `SUM(total_tokens)`；这与网关概览
卡的累计 `log_count`（全部历史行数）和统计页 `/stats?days=7` 的近 N 天窗口都是
三套口径，首页不复用它们、直接查日志表，刻意不叫「概览」。通道数不筛 `enabled`
（停用通道也是资产），与概览里 `channel_enabled` 分开计数。

### 门只读连通性状态，检查本身失败则放行

门的数据来自 `GET /api/settings/models/status` 的 `chat.ready`：该字段就是当前
用户默认对话模型配置行的 `last_test_ok`（无默认配置时 `PurposeStatus.ready`
默认 False）。连通性测试在设置页手动触发（`POST /settings/models/{id}/test`），
首页只读取结论、不发起任何真实探测。状态接口本身请求失败时前端按「就绪」处理——
家门口的一次网络抖动不该弹一条红字把人拦在门外，对话页内还有自己的重试入口。

### 一次请求出齐，而不是挨个调列表接口数长度

`GET /api/dashboard/summary` 一次返回全部数量：首页不必发十余次列表请求，单个
模块慢或失败也不会拖垮整页（概览请求整体失败时瓦片留在骨架态）。工作流光数定义
条数说明不了有没有在用，所以同一次响应里附带按发起时刻（`WorkflowRun.created_at`）
统计的 `run_count_today`；跨天跑完的运行不重复计入完成那天。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/routers/dashboard.py` | `GET /api/dashboard/summary`：`require_user` 鉴权，把 `user_id`/`is_admin` 交给服务层 |
| `backend/app/services/dashboard.py` | `DashboardService.summary()`：`owned_count` 通用计数 + 智能体/工作流/会话/今日运行四条专用查询；管理员补三项网关全局数 |
| `backend/app/models/schemas.py` `DashboardSummary` | 11 个必填整数字段 + 三个 `Optional[int]`（默认 `None`）的网关字段 |
| `backend/app/models/entities.py` | 计数表：`ChatConversation.ops_target_type`（会话口径）、`Agent`、`Workflow`/`WorkflowRun`、`KbProject`/`KbDocument`、`Resume`/`InterviewRecord`、`SlideDeck`、`OpsServer`/`OpsDatabase`、`AiChannel`/`AiCallLog` |
| `backend/app/routers/settings.py` `readiness_status` | `GET /api/settings/models/status`：`ReadinessStatus.chat.ready` 取默认 chat 配置的 `last_test_ok` |
| `backend/app/services/model_config.py` | `default_model_config()`（按 owner+purpose+is_default 查行）、`record_test_result()`（测试通过/失败落 `last_test_ok`）；改密钥等会作废旧测试结论 |
| `backend/app/services/ai_gateway/service.py` | 首页**不经过**它；网关概览的累计口径见 `overview()`（`log_count` 全量）与 `stats(days=7)`，对照用 |
| `frontend/src/views/Home.vue` | 问候区、门警告条、模块瓦片网格、最近对话列表、相对时间、深链跳转、keep-alive 重拉 |
| `frontend/src/api/dashboard.ts` | `dashboardApi.summary()` 与 `DashboardSummary` 类型 |
| `frontend/src/api/settings.ts` | `settingsApi.status()` → `/settings/models/status` |
| `frontend/src/api/chat.ts` | `chatApi.listConversations()` → `GET /api/chat/conversations` |
| `frontend/src/views/Chat.vue` | `handleConversationDeepLink()`：只接受纯数字 `conv`，打开会话后 `router.replace` 清 query，并 watch query 变化兼容 keep-alive |

涉及接口（均为真实方法+路径，统一 `/api` 前缀由前端 `request` 补上）：

- `GET /api/dashboard/summary` — 首页全部数量，响应信封 `APIResponse[DashboardSummary]`
- `GET /api/settings/models/status` — 连通性门的 `chat.ready`
- `GET /api/chat/conversations` — 最近对话列表（已排除运维内嵌会话）
- 对照口径（不在首页调用）：`GET /api/ai-gateway/overview`、`GET /api/ai-gateway/stats?days=7`

模型配置与连通性测试本身见 model-settings.md；AI 网关的通道、日志与统计页见
ai-gateway.md。

## 测试

- `backend/tests/test_dashboard.py`（7 个，SQLite 内存库直测 `DashboardService`）：
  属主隔离下各模块计数（`test_counts_are_scoped_to_owner`）、内置/停用工作流与
  智能体的计入规则（`test_builtin_workflows_counted_but_disabled_excluded`、
  `test_builtin_agents_counted_but_disabled_excluded`）、今日运行排除昨天与他人
  （`test_run_count_today_excludes_yesterday`）、运维绑定会话不计入
  （`test_ops_bound_conversations_excluded`）、非管理员三项网关字段为 `None`
  （`test_gateway_fields_hidden_from_non_admin`）、管理员口径只算今天且停用通道
  仍计入通道数（`test_admin_gateway_counts_are_today_only`）。
- 连通性门的读状态端点 `/settings/models/status` 没有独立测试；其依据的
  `last_test_ok` 语义（设默认必须先测通、改凭证作废结论）覆盖在
  `backend/tests/test_model_config.py`（如 `test_set_default_requires_a_verified_config`、
  `test_a_new_key_rotates_the_credential_and_clears_the_verified_state`）。
- 前端没有 Home.vue 的组件测试（`frontend/src` 下现有测试只覆盖 utils）；
  深链打开行为随对话页测试面手工验证，无自动化用例。
