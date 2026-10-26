# 多智能体体系

智能体（agent）是一套可复用的「对话人设」：独立的 system prompt、温度偏好与三个能力开关。
内置智能体随代码发布，`owner_id = 0` 全员可见但谁都改不了；用户可以新建自己的，也可以把
内置项「复制」一份进自己的空间再改。对话、工作流、简历/幻灯片等后台功能都按 slug 或 id
引用智能体，人设与模型配置相互独立——模型在会话级选择，智能体只携带温度偏好。

## 用户可见行为

- 「智能体」页卡片展示全员可见的内置项与本人自建项，按 `sort_order, id` 排序；
  内置卡有「内置」标签，动作只有查看 / 版本 / 复制；自建卡才有编辑、删除。
- `chat_visible=false` 的「功能内置人设」（运维专家、简历分析、幻灯片架构师等）折在
  「功能内置人设」折叠区里，不进对话选择器，仅供各功能模块后台按 slug 调用。
- 新建要填标识 slug（创建后不可改）、名称、system prompt；可切换检索知识库、运维只读
  工具、长期记忆、「在对话中可选」四个开关，温度是 0-100 的滑块或勾选「跟随模型配置」。
- 内置项不可直接编辑：点「复制」得到一份本人名下的可编辑副本（名称加「 副本」，
  slug 为 `原 slug-copy`，重名再追加 `-copy2`、`-copy3`）。
- 删除仅对本人自建项开放；已被会话/工作流引用不阻止删除——会话行保留 `agent_id`，
  历史还在，只是再发消息时该智能体按不可见处理，需要重新选一个。
- 对话页工具栏第一个下拉就是智能体选择器：只列启用且 `chat_visible=true` 的项，默认选
  排序最前的一个；选择随会话记住（`chat_conversation.agent_id`），重新打开旧会话恢复
  它当时用的智能体。聊到一半换人设会强制另开一条会话，不让两个人设的发言混在同一段历史里。
- 报告页「发起对话」默认选用内置「求职顾问」（`career-advisor`）；运维嵌入面板的会话
  在创建时由后端按目标类型绑定 `server-ops` / `db-ops`，用户无从手选。
- 工作流的每个步骤按智能体 slug 引用，执行时在「内置 + 本人」范围内解析；细节见工作流文档。

### 三个能力开关的实际效果

| 开关 | 打开后在 `stream_answer` 里发生什么 |
| --- | --- |
| `use_knowledge` | 挂 `search_knowledge_base` 检索工具、追加统一的引用标注规则 `_CITATION_RULES`；进循环前校验向量索引可用，且本轮必须已选项目，否则直接报「请先选择项目」 |
| `use_ops` | 挂 `ChatOpsToolbox`（服务器只读命令 / 数据库只读查询）+ 时间工具，追加 `_OPS_RULES`；只读是硬约束，写操作走 `propose_command` 确认通道。注意内置 `server-ops`/`db-ops` 本身该开关为 false，工具箱是靠会话的运维目标挂上的 |
| `use_memory` | 回答前注入该用户的长期记忆段落，回答后异步从对话提取新记忆；机制详见 chat-memory.md |

工具（本地方法 / MCP）是会话级勾选，不挂在智能体上，详见 local-tools.md。

## 关键取舍

### 内置项为什么只读、只能靠复制定制

内置行属于 `owner_id=0` 的共享空间，任何用户都能读。如果允许就地修改，一个人的改动会
影响全员，而且启动 seed 无法再安全地把改进后的 prompt 推上线（会覆盖用户改动）。所以
定制的唯一路径是复制：副本是 `is_builtin=false` 的独立行、独立版本史，内置行永远归代码管。

### seed 就地刷新人设，但保留用户的「藏起」决定

每次启动都把内置行的名称、描述、prompt、三个开关、温度、排序、`chat_visible` 拉回代码
定义，prompt 改进随重启生效；唯独 `enabled` 不动——把一个智能体停用是用户自己的决定，
必须扛得过一次部署。功能模块的后台调用走 `require_by_slug`，**不看 `enabled`**：停用只
影响对话选择器，不影响简历、运维这类功能自带人设（缺失才报「初始化损坏」错误）。

### 引用与确认规则为什么放在智能体定义之外

`_CITATION_RULES`（怎么标引用、资料不足不许编）和 `_OPS_RULES`（写命令必须确认）由
`stream_answer` 在拼装 system prompt 时统一追加，而不是写进每个内置 prompt。这样用户
自建的智能体只要打开开关，就自动获得同一套硬约束，不依赖他记得抄一段规则；硬约束始终
位于 prompt 最末尾，记忆/摘要等背景信息插在人设与规则之间。

### 温度：模型配置是硬约束，智能体只是偏好

`resolve_temperature(config, agent.temperature)` 的顺序是模型配置温度 → 智能体温度 →
都没有则不传该参数。「Kimi 只接受 1」这类限制是模型属性，只该在模型配置设一次；智能体
温度为 NULL（前端「跟随模型配置」）时不表达任何偏好。存储用 0-100 整数，调用时除以 100。

### slug：owner 内唯一、终身不变

唯一约束是 `(owner_id, slug)`，不是全局唯一——不同用户可以各有一个 `my-helper`，内置
slug 也不占用用户的命名空间（冲突检查只在「内置 + 本人」可见范围内）。slug 是工作流步骤、
后台功能代码引用智能体的稳定句柄，所以 `AgentUpdate` 根本不含 slug；删后 slug 可被新建
复用，因此工作流运行绑版本时连 agent id 一起存（见 agent-versions.md）。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `Agent` | 表 `agent`，唯一约束 `uk_owner_slug (owner_id, slug)`；`temperature` 可空，`enabled`/`chat_visible`/`sort_order` 等展示字段 |
| `backend/app/models/schemas.py` | `AgentCreate`（slug 正则 `^[a-z0-9][a-z0-9-]*$`、2-64 字符）、`AgentUpdate`（无 slug、全可选）、`AgentItem` |
| `backend/app/services/agents.py` | `BUILTIN_AGENTS`/`BuiltinAgent` 定义、`seed_builtin_agents()`、`AgentService`（`list`/`get`/`get_by_slug`/`require_by_slug`/`create`/`update`/`delete`/`duplicate`） |
| `backend/app/routers/agents.py` | `/agents` 一组接口；可见性收敛在 service，越权读一律 404 |
| `backend/app/bootstrap.py` `seed_agents()` | 启动 seed + 存量版本补 v1；`migrate_schema` 里对 `agent` 表的幂等 ALTER |
| `backend/app/services/chat/service.py` | `resolve_agent()`（默认只挑 chat_visible 的启用项）、`create_conversation()` 绑定 `agent_id`、`stream_answer()` 按三个开关拼装工具与 prompt、`create_ops_conversation()` 绑定运维人设 |
| `backend/app/services/providers.py` `resolve_temperature()` | 模型配置优先、智能体其次的温度合并 |
| `backend/app/services/workflows/exec.py` | 步骤执行时 `get_by_slug(step.agent_slug)` 解析智能体 |
| `frontend/src/views/Agents.vue` | 卡片网格、新建/编辑/查看弹窗、对话人设与后台人设分区、版本史弹窗 |
| `frontend/src/components/AgentCard.vue` | 单卡标签与动作（内置卡不渲染编辑/删除） |
| `frontend/src/views/Chat.vue` | 选择器 `agentOptions`、`loadAgents()`（`enabled_only=true`）、`onAgentChange()` 换人设重开会话、`selectConversation` 恢复 `agent_id` |
| `frontend/src/api/agents.ts` | `agentsApi`：list/create/update/remove/duplicate 与版本接口 |

人设的不可变版本快照（保存即追加、工作流运行绑版本、删除保留版本行）整套机制见
agent-versions.md，本文不重复。

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/agents?enabled_only=false` | 内置 + 本人的智能体，按 `sort_order, id`；`enabled_only=true` 只返回启用项（对话选择器用） |
| POST | `/agents` | 在本人空间新建；slug 冲突返回 `code=-1` 业务拒绝（非 HTTP 错误），请求体不合法走 422 |
| PUT | `/agents/{agent_id}` | 全字段可选、部分更新；内置项与他人项拒绝修改（业务码提示「复制后再编辑」，他人 id 为 404）；`temperature` 支持显式设回 null |
| DELETE | `/agents/{agent_id}` | 删除本人自建项；内置项拒绝，他人/不存在为 404 |
| POST | `/agents/{agent_id}/duplicate` | 复制到本人名下，返回可编辑副本（内置项与本人项都可复制） |

版本相关的两个 GET（`/agents/{id}/versions`、`/agents/{id}/versions/{version}`）见
agent-versions.md。对话侧的绑定不经过 /agents：首条消息 POST `/chat/completions` 带
`agent_id`，服务端自动建会话并落 `agent_id`；运维嵌入面板走 POST `/chat/ops-conversations`。

## 升级行为

- `migrate_schema` 对已存在的 `agent` 表幂等补列：`use_ops`、`chat_visible`、`use_memory`
  （缺哪列补哪列）；`temperature` 检测到仍是 NOT NULL 时执行一次 `MODIFY ... NULL`
  改可空，已可空则跳过（SQLite 测试库由 `create_all` 直接建成新结构）。
- 启动 `seed_builtin_agents()`：新内置项插入、已有项就地刷新人设字段而保留 `enabled`，
  全程幂等，重复执行不产生重复行；随后与人设快照表对账（全新安装补 v1、代码改过内置
  prompt 的升级启动追加新版本），并为版本功能上线前的存量智能体补 v1——详见
  agent-versions.md 的「升级行为」。

## 测试

- `backend/tests/test_agents.py`（service + HTTP 两层）：
  - 内置定义自检：slug 与 sort_order 全局不重复、所有内置行满足用户侧字段限额、
    开知识库的人设恰好是带 grounded 规则的那批、文档列出的 slug 与代码一致、
    功能人设都 `chat_visible=false`、每个工作流步骤引用的 slug 都存在；
  - seed：只建一次且重复运行幂等、prompt 改动被拉回但 `enabled`（藏起）保留、
    seed 只动共享空间不碰用户行；
  - 可见性与隔离：list 只见内置 + 本人、`enabled_only` 过滤、他人智能体读作不存在、
    `require_by_slug` 无视停用且缺失时报初始化损坏、内置项 service 层只读；
  - 增删改复制：slug 冲突拒绝、新建默认落本人空间、更新只写传入字段、删除只删本人行、
    复制内置项/他人项得到独立可编辑副本、复制不存在的 id 报 404；
  - HTTP：接口需登录、`enabled_only` 是查询参数、slug 冲突是业务拒绝而非 HTTP 错误、
    非法 body 422、改内置项在响应体里拒绝、他人 id 404、复制内置项走通整条 HTTP；
  - 温度：新建可为 null、更新可显式设回 null。
- `backend/tests/test_agent_versions.py`、`test_agent_versions_api.py`：版本追加、
  seed 对账与运行绑定，见 agent-versions.md。
- 能力开关在对话链路的消费分别由 `test_chat_service.py`、`test_ops_confirm.py`、
  `test_chat_memory.py` 覆盖（知识库工具与引用规则、运维工具箱与确认、记忆注入与提取）。
