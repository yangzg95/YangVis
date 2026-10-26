# 手动对话压缩（Compaction）

对话一长，模型上下文装不下，旧有的自动滚动摘要只能无声地把历史压成一段
system 提示，用户既看不见压掉了什么、也控制不了保留什么。现在对话页提供
**手动压缩**：用户选保留最近几轮、可附加压缩要求，先流式预览摘要，确认后才
应用。被压掉的消息原文一条不删，只是不再进入模型上下文，随时可展开摘要、
可取消压缩，也可以在此基础上一次再压（渐进式）。

## 用户可见行为

- 对话工具栏多一个「压缩对话」入口（消息不足两对时禁用）。向导里选「保留最近
  N 轮」（1–20，默认 6），可选填一条压缩要求（如「保留未决错误与文件路径」，
  最多 500 字）。
- 打开向导即流式生成预览：先给「将压缩 N 条 / 保留 M 条」，随后摘要逐字出现，
  可停止、可改参数后「重新生成」。**预览全程不写库**，点「应用压缩」才生效；
  关掉弹窗等于放弃。
- 应用后消息区顶部出现分界卡：「已压缩 N 条更早对话（不再进入上下文，原文
  保留）」，锚点前的消息在当前会话视图里隐藏；展开分界卡可看摘要全文与压缩
  要求，并可「取消压缩」——回放恢复自动窗口口径，消息一条不少。
- 可以再次压缩：切点只前移不后退（锚点前的历史不会被放回来），旧摘要并入
  新一次摘要的输入；上次的压缩要求默认带进向导。
- 自动滚动摘要没有消失：每轮问答后的静默重算仍在，且在有锚点时**保住锚点、
  沿用压缩要求**——锚点前的历史永远算摘要段，锚点之后老化出窗口的消息逐轮
  并入。分界卡对自动摘要也露出一条（无「取消压缩」按钮）。

## 关键取舍

### 锚点而不是删除

`chat_summary.cut_message_id` 是压缩锚点：该 id 及之前的消息永不回放，切点
沿用自动摘要同一条「不拆问答对」的边界（切点必落在一条助手消息上）。消息
原文全部保留，取消压缩只是删掉摘要行；这也让回放、导出、截图对证都不受损。

### 预览与应用是两次请求

预览只流式返回文本与切点，不落任何状态；应用时服务端重新校验切点仍属于本
会话、其后至少还有一轮对话、且没有被一次更新的压缩越过。拿着陈旧切点
（期间又压过一次、或消息被清理）直接 apply 会被拒，前端引导重新生成。

### 自动摘要与手动压缩共用一行

`chat_summary` 一个会话只有一行：`cut_message_id` 为 NULL 是自动滚动摘要，
非空是手动压缩落点。自动重算复用这行（不先删后建），锚点不被后台任务挪动；
压缩要求存在同一行的 `instruction` 上，每次重算都带进提示词。

### 不合成 checkpoint 消息

llm-space 在应用压缩时会造一条 user/assistant 形式的摘要消息插进线程；本项目
早已用 system prompt 注入摘要（「以下是本次会话更早部分的摘要…」），压缩
沿用这条经过验证的通道，不往消息表里写合成内容。线程仍是 DB 实体，压缩原地
生效，也不复制新线程/新标签页。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `ChatSummary` | 新增 `cut_message_id`（压缩锚点，NULL=自动摘要行）、`instruction`（压缩要求，自动重算沿用） |
| `backend/app/services/chat/memory.py` | `split_with_anchor`（锚点头永驻摘要段 + 锚点后再套窗口切分）、`plan_compaction`、`stream_compaction`、`apply_compaction`、`summary_row`/`anchor_id`；`refresh_summary` 改为保锚点、沿用 instruction |
| `backend/app/services/chat/service.py` | `_history` 按锚点截断；门面 `compaction_state` / `compaction_preview`（异步生成器，`compact_meta → token* → compact_done`）/ `apply_compaction` / `clear_compaction` |
| `backend/app/routers/chat.py` | `GET` 状态、`POST .../preview`（恒 200 的 SSE，错误落 error 帧）、`POST .../apply`、`DELETE` 四个端点 |
| `backend/app/models/schemas.py` | `CompactionPreviewRequest`（keep_turns 1–20、instruction ≤500）、`CompactionApplyRequest`（summary 1–2000）、`CompactionStateItem` |
| `backend/app/bootstrap.py` + `docs/schema.sql` | `chat_summary` 两列幂等 ALTER；建表语句与表注释同步 |
| `frontend/src/api/sse.ts` | `onCompactMeta`/`onCompactDone` 回调与派发；终止事件白名单加 `compact_done`（漏了会被看门狗判「连接中断」） |
| `frontend/src/api/chat.ts` | `CompactionState`/`CompactMeta` 类型与四个 API（preview 走 streamSse） |
| `frontend/src/views/Chat.vue` | 工具栏入口、分界卡（展开摘要/取消压缩）、压缩向导（流式预览 + 应用）、`visibleMessages` 按锚点隐藏、切会话顺带加载状态 |

## 升级行为

- 两列走 `migrate_schema` 幂等 ALTER（先查列名再 ADD）；SQLite 测试库由
  `create_all` 直接建出新列。
- 存量会话没有摘要行时 `GET` 返回 `data: null`，界面与升级前完全一致；
  已有自动摘要行的存量会话，两列均为 NULL，语义仍是「自动滚动摘要」。

## 测试

- `backend/tests/test_chat_compaction.py`（17）：`split_with_anchor` 三种
  形态、plan 的三类拒绝、apply 锚点/要求/截断与切点校验、clear 恢复全窗回放、
  `_history` 锚点截断、refresh 保锚点并 fold 新老化且沿用 instruction、
  预览不落库 + 指令透传 + 渐进式二次压缩切点前移、空模型输出与模型未测通
  拒绝、状态计数与属主隔离。
- `backend/tests/test_chat_compaction_api.py`（12）：状态 null/404、预览 SSE
  线格式（头、帧序、计数、不落库）、无可压内容与模型不可用落 error 帧、
  请求体 422、apply 信封/非法切点 404/-1/空摘要 -1、DELETE 幂等且不删消息、
  四端点的属主隔离（SSE 越权也是恒 200 的 error 帧）。
