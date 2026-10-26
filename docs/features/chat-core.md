# 智能对话核心（会话、流式回答、重试、附件与绘图）

对话页是 YangVis 最老的一条链路：会话是按用户隔离的 DB 线程，提问先落库再调
模型，回答以 SSE 逐 token 流回；失败可以原地重试，用户主动停止后半截回答照存。
本文覆盖会话 CRUD、`/chat/completions` 与 `/retry` 两条流、SSE 帧与心跳、附件
上传/回放，以及绘图助手（Mermaid 围栏识别与在线编辑）。相邻能力只交叉引用：
思考过程与 `llm`/`step` trace 节点的统一模型见 unified-trace.md；手动压缩与
滚动摘要见 chat-compaction.md；运维写命令的 confirm/exec 见
ops-chat-confirm.md；本地工具与 MCP 挂载见 local-tools.md；长期记忆注入/提取
见 chat-memory.md；智能体与人设开关见 agents.md；模型连通性测试与思考三态见
model-settings.md。

## 用户可见行为

- 左侧「会话」按最近活跃排序，支持标题关键词搜索（纯前端过滤）、铅笔原地重命名
  （Enter/失焦保存、Esc 还原、空标题不提交）、删除（二次确认，消息/附件/待确认
  项随会话一起清掉）。绑定运维目标的问答会话不出现在这个列表里。
- 不必先建会话：在输入框写好直接发送，后端用首问前 40 个字起标题自动建会话，
  侧栏随第一帧 `meta` 即时出现；只发附件不写字时，标题退回第一个附件的文件名。
- 回答逐字流出，上方工具行可切智能体、模型、知识库范围与会话级工具；选择随会话
  记住，重新打开旧会话恢复它当时的那套（已失效的勾选项就地裁剪掉）。流式进行中
  全部下拉禁用；聊到一半换智能体会强制另开一条会话，不让两个人设的发言混史。
- 模型下拉只列测通连通性的对话模型，支持图片理解的模型名字后带「（图片）」标签。
- 生成中可点「停止」：已收到的半截回答立即落定，末尾补「（已停止生成）」。
  ReAct 撞步数上限或单轮 900 秒墙钟时，回答同样存半截并提示「回复『继续』可接着
  排查」。
- 发送失败时用户气泡下出现「发送失败 / 重试」，重试不产生重复提问；网络层就断了
  （连会话都没建出来）时，问题文字与附件自动放回输入区，走一次普通发送。
- 首页「最近对话」以 `/chat?conv=<id>` 深链直达指定会话；id 已删除或不属于本人
  时静默停在空态，进入后立刻清掉 query，刷新不会重复打开。
- 附件：回形针选文件或直接往输入框粘贴截图。文档（PDF / Word / 纯文本 / 常见
  代码文件）上传时服务端抽成文本，图片保留原图并给缩略图；最多 6 个，文档
  10MB、图片 5MB。上传完即可预览/移除，发送时才真正挂到消息上。给非视觉模型配
  图片时前端直接拦在发送前。
- 绘图助手：助手回答里的 <code>```mermaid</code> 围栏在回答落定后升级成图卡片
  （流式途中仍按普通代码块显示），可在线编辑源码、复制源码、下载 SVG；保存前先
  本地校验语法，保存即改写这条助手消息，之后再提问，模型回放看到的就是改后的图。

## 关键取舍

### 恒 200：错误是一帧事件，不是 HTTP 状态

completions/retry 的响应头在第一个错误可能发生之前就发出去了，流式端点因此整体
豁免 `APIResponse` 信封：HTTP 恒为 200，任何失败都收束成一帧
`error {code, message}`。路由按 `BusinessError`（业务码，如模型未测通）、
`LookupError`（404 文案）、`ValueError`（工具选择失效，文案本身就是操作指引，
不再套「回答生成失败」）、兜底 `Exception`（记 exception 日志 + 「回答生成
失败：…」）分类。前置校验——附件批次、智能体解析、建会话——全部排在第一帧
`meta` 之前：连会话都不该建出来的失败，侧栏不得留下空会话。

### SSE 帧与心跳：裸 `event:/data:` 文本帧，15 秒一帧注释心跳

`sse_event(event, data)` 产出 `event: <名>\ndata: <json>\n\n`，统一带
`SSE_HEADERS`（`X-Accel-Buffering: no` 防 Nginx 整段缓存）。`with_heartbeat`
在下层产出停滞超过 15 秒时插一帧 SSE 注释 `: ping`——EventSource 语义上客户端
必须忽略注释帧，它只负责重置链路上各段的空闲读超时。实现用
`wait_for(shield(anext()))`：超时取消的只是外壳，绝不把取消送进正在等上游的
生成器（那等于自己掐断回答）；生成器被关闭时再收掉挂起的下层任务。前端不用
EventSource（要带鉴权头与 POST 请求体），而是 fetch + ReadableStream 自行按
空行分帧；100 秒一个字节都没收到（约 6 帧心跳余量）才判假死，流读完却没见过
终止帧（`done`/`error`）补一嗓「连接中断，请重试」。

### 先落库再生成；重试是「删掉悬空提问后重发」，不是补发

路由的固定顺序是：校验附件/智能体 → 建会话或取会话（必要时覆盖工具/范围选择）
→ 解析模型配置 → 发 `meta` → 落库 user 消息 → `stream_answer`。user 先落库是
有意的：失败后重试不必让用户重打一遍，停止生成后半截回答也有归属消息；代价是
失败时库里留下一条「user 在、assistant 不在」的悬空提问。重试端点先解析智能体
与模型（模型不可用绝不能先删用户的问题），再 `pop_dangling_user_message` 删掉
那条悬空 user 消息——附件只解绑 `message_id`、不删行——然后走与首发完全相同的
add_message + stream_answer，历史里不出现两条重复提问。会话末尾已是 assistant
（上次其实成功了）时按 409 业务拒绝。前端对「连 `meta` 都没收到」的失败另有
分支：后端大概率没有任何记录，本地删气泡、把文字与附件放回待发区走普通发送。

### 事件顺序由模型决定，不做固定状态机

回答是 LangGraph ReAct 循环：模型每轮自己决定调不调工具、调几次，所以 SSE 帧序
是 `step* → step_result → citations → (reason* | token*)` 的任意交错，最后
`done`，纯问答则一个工具帧都没有。`citations` 每次工具返回后都发**累积全量**
（跨多轮检索编号保持稳定），前端整体替换。工具的开始/结果按 langchain `run_id`
配对（并行调用不串），事件没有 run_id 时退回「最后一个还没结果的步骤」，前端同
口径兜底。终止帧只有 `done` 与 `error`；用户停止后服务端不发 `done`——客户端
已经走了，落库半截内容就是这次请求的全部意义。

### 中断、步数与墙钟：存下来的必须等于用户看到的

每处理一个 astream 事件先查一次 `is_disconnected()`，断开即 break 并在 finally
里 `aclose()` 上游流（释放发往 provider 的 HTTP 请求）、关 SSH 与 MCP 连接。
落库正文就是真正流出过的 token 拼接，停止时补「（已停止生成）」，绝不把模型在
后台跑完的完整文本偷偷存下。普通 ReAct 上限 8 步、挂运维箱或 MCP 时放到 25 步，
另有 900 秒单轮墙钟；两种超限都不是错误：半截回答照存、补一句「回复『继续』可
接着排查」并照常发 `done`。一个正文 token 都没有时兜底「知识库中未找到相关内
容，无法回答这个问题。」

### 绘图助手：只升级闭合围栏，改图就是改历史

识别规则刻意收窄：只有**已闭合**的 <code>```mermaid</code> 围栏才切成图段
（`splitMessageSegments` 按行扫描，开闭围栏大小写不敏感）。流式途中围栏多半没
闭合，整条回答走普通 markdown 渲染成代码块；闭合后下一次分段自然升级，视觉上是
「代码块长成了一张图」，未闭合残围栏连同开围栏行一起退回 markdown。Mermaid 懒
加载（含 dagre 布局，不拖慢普通对话），`securityLevel: 'strict'` 不允许标签里
的 HTML。编辑保存先本地 `mermaid.parse` 试渲染——存一份渲染不出来的源码，模型
下轮看到的也是坏代码；通过后 `PUT /chat/messages/{id}` 整体回写消息正文。该
接口只准改 assistant 消息，改的是历史本身，没有额外版本概念。

### 附件两步走：文档不存原文，图片落随机名路径

上传与发送是两次请求：`POST /chat/attachments` 先落一行附件（`message_id`
为空）拿 id，文档当场抽取成文本存入 `chat_attachment.text`（按
`CHAT_ATTACHMENT_MAX_CHARS` 截断，扫描版 PDF 这种抽不出文本的直接拒绝），图片
按内容魔数判定扩展名、写到 `chat/<owner>/<32 位随机 hex>.<ext>`，用户给的文件
名只用于展示、永不参与路径拼接；读取/删除前一律用白名单正则复验相对路径。发送
时 `describe_attachments` 再挡一道：附件必须属本人且没被别的消息占用（已发过的
批次返回 409），这步排在建会话之前。没挂上消息的附件可删（连图片文件一起），
已发送的不许删。回放时附件不进 `ChatMessage.content`：文档正文按 60k 字符总
预算、图片按最多 8 张分配，优先最近一轮，超预算的旧附件只留一行说明；非视觉模
型下图片降级为「内容未提供」说明。删除会话时附件行与图片文件级联清理。

### system prompt 的拼装顺序

`stream_answer` 里 system prompt 按固定层次叠加：智能体人设 →（开长期记忆时）
记忆规则与记忆段 → 会话滚动摘要 →（挂运维箱时）运维硬规则与目标提示后缀 →
（开知识库时）引用标注规则。硬约束永远在 prompt 最末尾，背景信息插在人设与规则
之间，这样用户自建智能体只要打开开关就自动获得同一套约束。人设、三个能力开关与
温度合并的细节分别归 agents.md、chat-memory.md；检索工具与运维/MCP 工具如何挂
载归 local-tools.md、ops-chat-confirm.md，本文不重复。

### 会话记住选择：不传 = 沿用，[] = 清空

会话行持久化 `agent_id`、`project_id/type_ids`、`model_config_id`、
`local_tool_keys`、`mcp_server_ids`。追问时这些字段一律「缺省沿用会话已记住的
那份，显式传值（含空数组）才覆盖，空数组就是清空」；模型在会话落库之后才解析：
点名的配置必须是本人的、对话用途且通过连通性测试，否则退回设置里的默认对话模型。
`meta` 帧携带本轮真正生效的全套选择，前端以它为准回写选择器——首问自动建会话时，
前端那套临时选择由此被服务端确认。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/routers/chat.py` | 会话 GET/POST/PUT/DELETE、消息列表与 PUT 改写、附件 POST/DELETE、`POST /chat/completions` 与 `POST /chat/conversations/{id}/retry` 两条 SSE（含建会话、meta、错误分类）；confirm/reject 仅经此路由，语义见 ops-chat-confirm.md |
| `backend/app/services/chat/service.py` | `ChatService`：会话/消息/附件落库与属主收敛、`stream_answer`（astream_events → SSE 二元组、prompt 装配、ReAct 步数/墙钟/中断、引用编号、附件回放预算、时间线累积）、`pop_dangling_user_message`、`require_chat_config`/`resolve_agent`、`derive_title`、落库后的 `after_answer_task` |
| `backend/app/sse.py` | `sse_event` 帧格式、`SSE_HEADERS`、`with_heartbeat`（15s `: ping`，shield 不杀下层）与字节透传版 `with_byte_heartbeat` |
| `backend/app/services/chat/attachments.py` | 文档抽文本（pypdf / python-docx / 编码嗅探，截断）、图片魔数嗅探与随机名落盘、相对路径白名单复验、回放 data URL、删除文件 |
| `backend/app/models/entities.py` | `ChatConversation`（选择字段 + `ops_target_*`）、`ChatMessage`（content/citations/timeline）、`ChatAttachment`（kind/rel_path/text/message_id） |
| `backend/app/models/schemas.py` | `CompletionRequest`（问题与附件不能同时为空）、`ConversationCreate/Update/Item`、`MessageItem/MessageUpdate`（正文 1–20000）、`ChatAttachmentItem` |
| `frontend/src/api/sse.ts` | `streamSse`：fetch + ReadableStream 分帧、100s 停顿看门狗、终止帧白名单、`StreamHandlers` 回调契约与 `dispatchSse` 派发 |
| `frontend/src/api/chat.ts` | `chatApi`（会话/消息/附件 REST、streamCompletion/streamRetry）、`TimelineEntry`/`ChatMessage`/`ChatAttachment` 等类型 |
| `frontend/src/views/Chat.vue` | 侧栏搜索/重命名/删除、选择器与 gate 提示、发送/停止/重试、流式时间线与气泡、待发附件区、`?conv=`/`?report=`/`?resume-doc=` 深链、改图回写 |
| `frontend/src/components/MermaidCard.vue` | mermaid 懒加载与渲染、语法错误展示源码、在线编辑（保存前 parse）、复制源码、下载 SVG |
| `frontend/src/utils/messageSegments.ts` | <code>```mermaid</code> 围栏切分（未闭合退回 markdown）与 `replaceMermaidSegment` 回写重建 |
| `frontend/src/utils/streamRender.ts` | 流式 markdown「缓存 + 100ms 节拍」渲染，避免每 token 重解析全部历史消息 |
| `frontend/src/components/ThinkingTrace.vue` | 回答气泡的思考/工具过程外壳；节点模型与渲染见 unified-trace.md |

### SSE 事件类型（completions / retry）

| 事件 | 时机 | 载荷要点 |
| --- | --- | --- |
| `meta` | 建/取会话（含本轮选择覆盖）之后、提问落库与任何 token 之前 | `conversation_id`、`agent_id`、`project_id`、`type_ids`、`model_config_id`、本轮生效的 `local_tool_keys`/`mcp_server_ids` |
| `step` | 一次工具开始（`on_tool_start`） | `tool`、`query`、完整 `input`、`run_id`、`started_at` |
| `step_result` | 该工具返回（`on_tool_end`） | `run_id`、截断到 2000 字的 `output`、`is_error`、`elapsed_ms` |
| `citations` | 每次工具返回后 | 截至当前的**累积全量**引用列表，前端整体替换 |
| `reason` | 推理模型的思维链增量 | 字符串增量；普通模型不发，三态配置见 model-settings.md |
| `token` | 正文增量 | 字符串增量；步数/时间上限的提示语也走它追加 |
| `llm` | 一次模型往返结束（含无工具轮次） | `elapsed_ms`；trace 语义见 unified-trace.md |
| `confirm` | 模型提议一条待执行写命令 | `action_id`/`command`/`target_*`/`timeout_seconds`；见 ops-chat-confirm.md |
| `exec` | 只读命令直接执行，或确认后续答流里的已确认命令 | `action_id`（只读为 null）、`exit_status`、`output`、`elapsed_ms`、`success` |
| `done` | 回答落库完成（停止生成时不发） | `message_id`、`conversation_id` |
| `error` | 任意失败的统一收束 | `code`、`message`；恒 200 响应，不靠 HTTP 状态表达错误 |

帧间停滞超过 15 秒另有一帧 `: ping` 注释心跳（不是事件，客户端分帧后直接
忽略）。压缩预览流的 `compact_meta`/`compact_done` 与工作流的
`run_start`/`step_*`/`run_end` 复用同一套传输与派发，帧名互不重叠，分别见
chat-compaction.md 与工作流文档。

## 升级行为

- 老消息 `timeline` 为 NULL 时前端退回只渲染 `content`，与升级前的对话页完全
  一致；附件能力上线前的消息在消息接口里返回空附件数组，无需数据回填。
- 本文覆盖的会话/消息/附件行为不依赖新的强制数据迁移；`timeline` 列本身的
  ALTER 历史记录在 unified-trace.md。

## 测试

- `backend/tests/test_chat_service.py`（service 层主战场）：会话创建/更新的
  选择项校验、空数组归一为 NULL、部分更新与「不传不动」、属主隔离（他人读作
  404）、列表排序与消息计数、运维会话默认隐藏；删除会话级联消息/待确认项/摘要；
  消息落库（citations 序列化、`updated_at` 触碰）、消息列表顺序与引用重建、
  `update_message_content` 只准 assistant；`pop_dangling_user_message` 的真悬空/
  末尾已回答/他人行三种形态；`_history` 窗口、不拆问答对与属主隔离；`_replay`
  的角色映射、`question_persisted` 两种语义；`require_chat_config` 的显式/默认/
  未测通/用途不符；`resolve_agent` 默认只挑 chat_visible 启用项；引用编号注册器
  的跨轮稳定编号；检索工具的闭包 scope、空结果与异常重抛；`stream_answer` 的
  帧序（step_result → citations → token → done）、trace 计时与纯问答 llm 帧、
  落库内容等于所见、无检索时 citations 为 NULL、空回答兜底、步数预算、abort 后
  半截落库且不发 done、收尾任务在正常/中断两轮都调度；`after_answer_task` 的
  独立 session、会话/配置消失时静默、摘要与记忆提取失败互不连坐。附件部分另含：
  文本/代码抽取与编码嗅探、截断预算、docx 段落加表格、扫描版 PDF 拒绝、图片随机
  路径往返、BMP 与超限拒绝、附件绑定/复用 409、属主与顺序、视觉闸门、回放多模态
  与非视觉降级、retry 解绑附件、未绑定附件删文件、已发不许删、删会话清附件。
- `backend/tests/test_chat_attachments_api.py`：HTTP 层上传文本（服务端抽正文）
  与图片（魔数嗅探 + URL）、不支持格式走业务拒绝、图片超限、删除未绑定附件成功、
  删除不存在 id 返回 404。
- `backend/tests/test_chat_tools.py`：会话级工具选择的落库去重/清空/替换/保持
  语义，本地工具与 MCP 的真实挂载、输出截断、连接束关闭，MCP 缺失/停用/连不上
  时面向用户的指引性错误，本地与 MCP 工具重名拒绝，运维会话默认挂时间工具。
- `backend/tests/test_ops_confirm_api.py` 中两个 SSE 用例顺带覆盖 `app/sse.py`：
  空档插入心跳帧且不吞掉下层事件、`aclose()` 时挂起的下层任务被收掉。
