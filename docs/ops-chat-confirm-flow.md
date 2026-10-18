# AI 运维助手：流式对话与写命令确认

「自然语言 → 命令/SQL」这条链路的实现说明。覆盖 SSE 事件协议、两阶段落库确认、三层安全边界、前后端状态流转，以及本地怎么验证。

## 方案一句话

**回答走 SSE 流，写操作走落库状态机** —— 模型只能*提议*写命令，提议就是往 `ops_pending_action` 落一行 `pending` 然后把本轮回答正常收尾；用户点确认后由 confirm 请求自己认领（CAS）、在自己的 worker 上执行、基于结果续答。全链路零进程内状态。

## 为什么不是「协程干等用户点按钮」

这是本设计最需要解释的一点，也是旧 WebSocket 方案死掉的原因。

「断点」处没有任何东西在等。`_propose()`（`services/chat_ops.py:276`）落库后直接 `return` 一个字符串给模型，函数就结束了——没有 `await`、没有 `Event`、没有 `Queue.get()`。模型按 prompt 规则收尾，`astream_events` 自然耗尽，`done` 发完，`finally` 里 `stream.aclose()` + `toolbox.aclose()` 关掉 SSH（`services/chat/service.py:925`）。此后这个 worker 上关于本次提议的内存状态**全部销毁**：工具箱对象、那条连接、请求级 session。

唯一的残留是 MySQL 里那一行 `status='pending'`。所以：

- gunicorn 多 worker 天然不是问题——确认请求落在哪个 worker 都行，反正状态在库里。
- 刷新页面、换浏览器、进程重启都能恢复卡片（`GET /api/chat/conversations/{id}/actions`）。
- **挂调试器去看是看不到「卡住的协程」的**，进程完全空闲。整条链路唯一真会阻塞的是 `execute_confirmed` 里的 `run_once`（`services/chat_ops.py:335`，最长 `OPS_AGENT_CMD_TIMEOUT=300s`）。

## 接口

| 接口 | 位置 | 说明 |
| --- | --- | --- |
| `POST /api/chat/completions` | `routers/chat.py:289` | 提问，SSE 流式回答 |
| `POST /api/chat/conversations/{id}/retry` | `routers/chat.py:385` | 重答最后一问（先删悬空的 user 消息，避免历史里两条一样） |
| `POST /api/chat/actions/{id}/confirm` | `routers/chat.py:227` | 同意：认领 → 执行 → 续答，SSE |
| `POST /api/chat/actions/{id}/reject` | `routers/chat.py:211` | 拒绝，普通 JSON |
| `GET /api/chat/conversations/{id}/actions` | `routers/chat.py:143` | 卡片回放 |
| `GET` / `POST /api/chat/ops-conversations` | `routers/chat.py:178,196` | 绑定模式的会话列表 / 新建 |

三个流式端点都**恒返回 HTTP 200**，错误靠一帧 `error` 事件传——报错时 200 和响应头早就发出去了，改不了。

## SSE 事件协议

帧格式 `event: <名>\ndata: <json>\n\n`（`sse.py:24`）。顺序由模型行为决定，不是固定的。

| 事件 | payload | 产出位置 | 说明 |
| --- | --- | --- | --- |
| `meta` | conversation_id / agent_id / 检索范围 / model_config_id | `routers/chat.py:340` | 先于任何 token，首条提问也能立刻改 URL 与侧栏 |
| `step` | `{tool, query, input}` | `chat/service.py:876` | 每次 `on_tool_start`。`input` 是完整入参，前端按工具名挑 command / statement / query |
| `citations` | 全量列表 | `chat/service.py:884` | 每次 `on_tool_end` 后发一次，客户端整体替换。模型一次都不检索就没有这帧 |
| `token` | 字符串 | `chat/service.py:916` | 正文增量 |
| `exec` | `{action_id, command, exit_status, output, elapsed_ms, success}` | `chat/service.py:889`（只读）、`chat/service.py:595`（确认后） | `action_id: null` 表示未经确认的只读直跑 |
| `confirm` | `{action_id, command, reason, target_*, timeout_seconds}` | `chat/service.py:902` | `propose_command` 落库后立刻推，卡片和本轮回答一起出现，不等 `done` |
| `done` | `{message_id, conversation_id}` | `chat/service.py:985` | 用户中途停止时**不发**（`chat/service.py:981`）——客户端已经走了 |
| `error` | `{code, message}` | 各端点兜底 | 403 / 404 / 409 / -1 |

confirm 端点的事件序列是固定的：`exec → token* → done|error`。

### 不卡死的三处配合

- 后端每 15s 插一帧 `: ping\n\n` 注释帧心跳（`sse.py:15,31`）。实现要点是 `asyncio.shield` 只包外壳——直接 `wait_for` 会把取消打进正在等 provider 响应的下层 `__anext__`，等于自己掐死回答。
- 响应头带 `X-Accel-Buffering: no`（`sse.py:105`）。漏写一处的表现是：那条流在 Nginx 后面攒完才吐，本地开发看不出问题。
- 前端 100s 没收到任何字节触发看门狗掐流（`api/index.ts:1058`，约 6 帧心跳余量）；流读完却没见过终止事件时补一帧「连接中断，请重试」，否则界面上的转圈永远停不下来。

## 授权：三层边界

**1. 分类**（`services/ops/safety.py`，刻意写成不碰网络不碰 DB 的纯函数）。三态 `readonly` / `needs_confirm` / `forbidden`。白名单不只看首个词——`;` `&&` `||` 重定向 反引号 `$(` `${` 换行一律挡（`ops/safety.py:46`），管道网开一面但要求每段独立合法，危险 flag 单独拦。

**数据库侧还有第二道真保证**：连接建立后立刻 `SET SESSION TRANSACTION READ ONLY`。即使解析被绕过，服务端自己拒写。所以数据库不存在「确认后可写」通道——模型要写只能把 SQL 写给用户，让他到运维页面执行。

**2. 确认**。只读直接执行；`forbidden` 直接挡；`needs_confirm` 落 pending。三道门的位置：只读通道 `_run_readonly`（`chat_ops.py:264`）、提议通道 `_propose`（`chat_ops.py:276`，里面遇到 `readonly` 会直接执行、不打扰用户，遇到 `forbidden` 明确告知「无法提交确认」）。

模型**不能**绕过分类：它手里只有两个工具，只读工具跑到非只读命令时会返回一句话让它改用 `propose_command`（`chat_ops.py:269`）。

**3. 留痕**。`ops_audit_log`，verdict 为 `readonly` / `confirmed` / `rejected` / `forbidden`（人工通道另有 `manual`，见 `docs/schema.sql:230`），确认执行的记录双向带 `pending_action_id`。审计写失败只记日志、不回滚会话（`chat_ops.py:90`）。

### 权限位 `ops_write`

判定是 `is_admin or ops_write`（`models/schemas.py:792`，管理员是天然超集）。三个要点：

- **confirm 端点里闸门必须在 CAS 之前**（`routers/chat.py:243`）：无权限用户不能把 pending 认领成 `approved`。
- **reject 不设限**：拒绝是安全方向，任何时候都该能点。
- 前端置灰只是提前讲清原因（`ConfirmCard.vue:21` + `stores/auth.ts:19`），真正拦截是服务端那帧 403。人工通道是双闸门叠加：连接 `writable` × 用户 `ops_write`（`routers/ops.py:313`）。

## 状态机

```
pending ──claim_for_confirm()──► approved ──执行成功──► executed   [终态]
   │                                 └────执行失败────► failed     [终态]
   │
   ├──claim_for_reject()─────────► rejected                        [终态]
   │
   └──180s 无人应答──────────────► expired                         [终态]
       （CAS 的 created_at 条件不成立；或进程重启时 reset_stale 物化）
```

实现在 `services/ops/actions.py`，全部是单语句 UPDATE：

- `claim_for_confirm`（:83）—— `WHERE id AND owner_id AND status='pending' AND created_at >= now-180s`。InnoDB 行锁保证并发/重复点击只有一个请求 `rowcount=1`。**过期视同未批准**——没人应答就执行写命令，正是这套设计要防的事。
- `claim_for_reject`（:106）—— 不卡 `created_at`：用户点的「拒绝」就是答案，只是来晚了，允许标记。
- `finish_execution`（:125）—— `approved` 是唯一合法前态，别的方法来路直接 `RuntimeError`，宁可报错也不覆盖。
- `display_status`（:47）—— 过期判定用惰性换算，不每次回写库。
- `reset_stale_ops_actions`（:144，`bootstrap.py:340` 启动时调）—— 把上个进程遗留的 pending 物化成 expired，让它们呈现为「等待超时」而不是永远可点。

拒绝时会补一条 assistant 消息进历史（`chat/service.py:554`）：否则历史里「提议 → 没有下文」会让模型以为命令还在等。

## 前端状态流转

`utils/useOpsConfirm.ts` 是两个使用方（主对话 `Chat.vue`、嵌入面板 `OpsChat.vue`）共用的状态机。

| 时机 | 动作 |
| --- | --- |
| 收到 `confirm` 事件 | `actionFromConfirm()` 造本地对象，status=pending，`created_at` 取当下（事件不带时间戳） |
| 点「同意执行」 | 乐观置 `approved`（按钮立刻消失），`actingIds` 挡重复点击 |
| 收到 `exec` | 写回真实终态 + `result` + `exit_status`，卡片展开输出块 |
| 收到 `error` | 409/404（出现在 exec 之前，说明没在这次请求里执行）钉成 `expired`；其他错误恢复 `pending` 让用户能再点——若服务端其实已执行，再点会被 CAS 挡下走 expired 分支 |
| 倒计时归零 | **只改展示层** `displayStatus`（`ConfirmCard.vue:80`），不碰 `action.status`——裁决权在服务端 |

续答接进一个**新气泡**，与普通回答共用同一条流式渲染管线（`Chat.vue:1050`）。回答进行中点了确认会被挡下（「请等当前回答结束后再确认」，`Chat.vue:1051`）——一次只跑一条流。卡片按 `message_id` 挂到触发它的那条气泡下面，`message_id` 为 NULL 时挂当前流式气泡（`Chat.vue:412`）。

`exec` 事件在服务器问答页还会往外 emit，命令与输出同时打进终端窗口（`OpsChat.vue:561` → `OpsServerTerminal.vue:297`），让用户看到 AI 实际干了什么。AI 回答里的 shell 块带「在终端中打开」、SQL 块带「在查询框中打开」。

## 两种挂载形态，同一份工具箱

| | 自由模式 | 绑定模式 |
| --- | --- | --- |
| 入口 | 智能体开 `use_ops`（内置 `ops-assistant`，`agents.py:467`） | 会话带 `ops_target_type/id`（内置 `server-ops` / `db-ops` 人设，二者 `use_ops=False`，工具箱是靠会话目标挂上的，`chat/service.py:815`） |
| 工具签名 | 带 `server_id` / `database_id`，模型从台账里挑 | **根本没有 id 参数**，命令永远落在绑定的目标上 |
| 归属校验 | 在闭包里做 `self._servers.get(server_id)`——模型能决定查哪台，够不到别人的机器 | 建会话时已校验 |
| prompt | 无目标说明 | `prompt_suffix()` 追加「当前已连接的服务器/数据库…」（`chat_ops.py:192`） |
| 会话列表归属 | 出现在主对话侧栏 | 不进主对话列表（`list_conversations` 默认过滤 `ops_target_type IS NULL`），只在嵌入面板的历史下拉里翻 |

`_OPS_RULES`（`chat/service.py:104`）追加在 system prompt **末尾**，不依赖用户自己写的智能体——「写命令必须经用户确认」这条硬约束不该因为用户改了人设就消失。

## 规避掉的坑

1. **等待跨不过进程边界。** 见上节。这是整篇的由来。
2. **`exec` 必须早于模型配置解析。** `chat/service.py:593`：命令已经执行完了，这时才发现「对话模型没通过连通性测试」绝不能把结果吞掉。
3. **续答不落 user 消息。** 合成的是 `[系统] 用户已批准…` 旁白，`question_persisted=False`（`chat/service.py:629`）。落 user 消息的话，用户的提问流里会出现一条他没说过的话；不落的话历史里已有的问答仍能给模型来龙去脉。
4. **`message_id` 只能回填。** 提议发生在流式中途，那条 assistant 消息还没落库，所以列可空，回答落库后再补（`chat/service.py:962`）。
5. **运维排查会把步数上限撞穿。** 沿用检索的 `MAX_REACT_STEPS=8` 会在排查中途抛 `GraphRecursionError`，用户看到的就是「AI 说到一半突然停了」。挂工具箱时提到 25（`chat/service.py:67`），且真撞上限时不报错，把已流出的内容存下来 + 补一句「回复『继续』可接着排查」（`chat/service.py:88`）。
6. **AI 通道要更宽的命令超时。** `du`/`find` 在大磁盘上远超交互通道的 20s，用交互超时会误杀。AI 通道单独 `OPS_AGENT_CMD_TIMEOUT=300s`（`chat_ops.py:231`），交互终端不受影响。
7. **命令输出撑爆上下文。** 两层截断：传输层 `OPS_OUTPUT_LIMIT=8192`（`ops/server.py:355`），回灌模型前再截到 `OUTPUT_LIMIT=4000`（`chat_ops.py:48`）。模型要细看某一段可以自己加 `tail`/`grep` 再查。
8. **日志不带命令全文。** 写命令常带敏感参数，全文已在审计表，日志只留首词（`chat_ops.py:307`）。
9. **SSH 连接别常驻。** 一次回答里复用一条（每条命令重连既慢又刷 auth 日志），回答结束即关——一次回答可能只跑一两条命令，长连接占着不如随用随建。
10. **删会话要连带删 pending，但不能动审计。** `chat/service.py:341`：`ops_pending_action` 跟着会话走（挂起的卡片不该留在任何地方），`ops_audit_log` 是留痕、不是会话内容。

## 明确的取舍

- **确认与执行绑在同一个请求里**，代价是 confirm 这条流要承担「执行 + 续答」两件事，中断它只会丢续答，命令已经跑完了（这也是前端对「流断了」恢复 `pending` 而不是 `failed` 的原因）。
- **超时按未批准处理，而不是按批准处理**，且过期不许 confirm、仍然许 reject。
- **数据库不做「确认后可写」**，只读是硬约束。写需求交回人工页面——少一条攻击面，也少一套状态机。
- **前端一切预设都只是展示层**，裁决全在服务端 CAS。倒计时归零不改 status、乐观置 approved 会被 exec/error 覆盖。

## 本地验证

```bash
./dev.sh              # 后端 18099（uvicorn --reload，单进程），前端 5173 代理 /api
```

**触发一条待确认命令**：运维终端页右侧 AI 面板问「帮我把 xxx 服务重启一下」，或主对话选「运维助手」智能体。

**看状态机**（比看协程有效得多）：

```sql
SELECT id, conversation_id, message_id, command, status, exit_status, created_at, resolved_at
  FROM ops_pending_action ORDER BY id DESC LIMIT 5;
SELECT id, actor, command, verdict, success, pending_action_id
  FROM ops_audit_log ORDER BY id DESC LIMIT 10;
```

能看到 `pending → approved → executed/failed` 三段迁移，以及 `message_id` 先是 NULL、回答落库后才有值。

**下断点**，四个位置够看全貌：

| 位置 | 看到什么 |
| --- | --- |
| `chat_ops.py:305` | 提议落库瞬间 |
| `chat/service.py:902` | `confirm` 帧推给前端 |
| `ops/actions.py:89` | CAS——两个标签页同时点确认，第二次 `rowcount=0` |
| `chat_ops.py:335` | **唯一真正挂起的地方**；命令跑 120s 期间前端只收到 `: ping`，Network 里 Response 一片空白 |

别在断点上停超过 100s——前端看门狗会掐流，会误判成 bug。

**验证超时分支**：`backend/.env` 里 `OPS_CONFIRM_TIMEOUT=10`（配置字段名即环境变量名，无 prefix），提议后等 15 秒点同意 → 必然 409 → 卡片「等待超时，请让 AI 重新提议」。重启后端再进该会话，卡片会是 expired 而不是永远可点（`reset_stale_ops_actions`）。

**curl 拿原始帧**（`Authorization` 直接放 token，不带 `Bearer` 前缀）：

```bash
curl -N -X POST http://localhost:18099/api/chat/actions/18/confirm \
  -H "Authorization: $TOKEN" -H 'Content-Type: application/json'
```

## 现状与缺口

- **测试覆盖**：分三层钉住，共 60 个用例。
  - `backend/tests/test_ops_confirm.py`（44 个）打服务层：CAS 只赢一次、超时视同未批准但仍可拒绝、`approved` 是 `finish_execution` 唯一合法前态、进程重启遗留的 pending 物化成 expired、owner 越权一律 `LookupError`；提议阶段落库即返回且一次 SSH 都不发起；`execute_confirmed` 的失败（非零退出、连不上、目标已删）都要落成能续答的结果；确认流的 `exec` 必须早于模型配置解析；续答上下文是合成旁白、`question_persisted=False`，不许在历史里冒出用户没说过的话；`stream_answer` 的 `step → exec → confirm → done` 序列与 `message_id` 回填；停止/撞步数上限时落库的就是屏幕上出现过的那半截。
  - `backend/tests/test_ops_confirm_api.py`（16 个）打 HTTP 层：`ops_write` 闸门排在 CAS **之前**（无权限用户点确认之后那一行仍是 pending、SSH 一次没跑）；恒 200 的 SSE，失败落成 `error` 帧（403 / 409 已处理 / 409 超时 / 404 不存在 / -1 意外）；线格式 `event: X\ndata: {json}\n\n` 与 `X-Accel-Buffering: no`；reject 走统一信封，二次拒绝是 HTTP 200 + `code 409`；`GET /conversations/{id}/actions` 的回放顺序与过期口径；`with_heartbeat` 补 ping 但不吞下层慢出来的那一帧、消费方断开时挂起的读取被取消。
  - 第 1 层（分类与 SQL/shell 绕过手法）仍由 `backend/tests/test_ops_safety.py` 的 19 个用例负责。
  - 两处都没测到的：真 SSH、真服务商。测试里全部打桩，机器上的实际行为要靠 `## 本地验证` 那一段。
- **已知措辞冲突**：内置 `ops-assistant` 人设正文第 4 条（`agents.py:479`）让模型「把命令写清楚交给他到运维页面执行」，而末尾 `_OPS_RULES` 第 3 条要求走 `propose_command`。两条指令对同一个场景给出相反做法。
  **未实测**：自由模式下确认卡片究竟稳不稳出得来，我只从注入顺序推断（`chat/service.py:829` 把 `_OPS_RULES` 拼在 prompt 最末尾，靠位置压过人设正文），没有实跑统计过成功率。要坐实就多问几次「重启 xxx 服务」看卡片出不出现。改的话改人设正文，别改 `_OPS_RULES`——后者是所有挂了运维工具箱的智能体共用的硬约束。
- 改这条链路时顺带看一眼 `docs/schema.sql:239` 的 `ops_pending_action` DDL 注释，状态枚举与 verdict 枚举都以它为准。

## 相关文件

- **后端**：`routers/chat.py`（completions / retry / confirm / reject / actions 回放 / ops-conversations）· `services/chat/service.py`（ReAct 循环、`_OPS_RULES`、`confirm_action_stream`）· `services/chat_ops.py`（工具箱与两阶段上半场）· `services/ops/actions.py`（状态机）· `services/ops/safety.py`（分类）· `services/ops/server.py` / `ops/db_exec.py`（执行）· `sse.py`（帧格式、心跳、响应头）· `models/entities.py`（`OpsPendingAction`）· `bootstrap.py`（启动收尾）
- **前端**：`api/index.ts`（`streamSse` / `dispatchSse` / `chatApi.confirmAction`）· `utils/useOpsConfirm.ts`（状态流转）· `components/ConfirmCard.vue`（卡片与倒计时）· `components/OpsChat.vue`（嵌入面板）· `views/Chat.vue` · `views/OpsServerTerminal.vue`（exec 回显进终端）
