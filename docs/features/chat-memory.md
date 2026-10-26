# 滚动摘要与长期记忆

对话系统最初只有「固定窗口、窗口外历史直接消失」。现在有两条互补的记忆通道：

- **会话摘要**（`chat_summary`）：窗口外的本会话历史压成一段滚动摘要，属于这一条会话；
- **长期记忆**（`user_memory`）：跨会话沉淀的「关于用户本人的持久事实」，属于用户。

两者都在回答时拼进 system prompt，并由同一个请求外后台任务在每轮回答落库后重算。
会话摘要后来又承载了手动压缩（锚点、压缩要求），那部分的用户界面与实现见
[chat-compaction.md](chat-compaction.md)，本文只讲滚动摘要与长期记忆这条基线。

## 用户可见行为

- 对话超过上下文窗口后，更早的历史不会突然消失，而是以「本次会话更早部分的摘要」
  继续参与每轮回答，用户无需做任何操作。
- 开了「长期记忆」开关的智能体会在对话中**自动**沉淀关于用户的持久事实
  （职业背景、偏好、长期目标），下一次新会话也记得；没开的智能体既不消费也不积累。
  内置人设里只有通用助手与求职顾问默认打开。
- 「系统设置 → 长期记忆」Tab：记忆全量可见，可手动新增、编辑、删除任意一条，
  也可一键清空（不可恢复）。AI 记住了什么，用户永远看得见、管得了。
- 删除会话**不删记忆**：摘要随会话走（连带删除），记忆随用户走（保留）。
- 模型未配/失效时摘要与记忆静默跳过，对话本身照常——增强功能永远不拖垮主链路。

## 关键取舍

### 两张表，而不是一条会话线程

摘要的作用域是会话、记忆的作用域是用户，生命周期本来就不同：会话删掉时摘要应
连带消失，而用户在另一条会话里留下的事实不该受影响。拆成 `chat_summary`
（一会话一行）与 `user_memory`（一条一句话）两张表，删除语义自然成立，不用在
一张大表里打作用域标记。

### 全量重算摘要，不存 checkpoint

增量摘要需要存「已压到第几条」，用户手动改历史、后台任务中途死掉都会让 checkpoint
永久过期。做法是每次全量重算、无状态、幂等自愈：只有历史超过 `SUMMARY_INPUT_LIMIT`
（12000 字符）时才退化为「旧摘要 + 尾部新消息」合并。每次多烧一点 token，
换正确性与零进程内状态（多 worker 部署安全）。

### 记忆靠后台自动提取，不靠工具调用

曾考虑让模型自觉调 `save_memory` 工具——模型会忘，覆盖率不可控。现在每轮回答
落库后 `after_answer_task` 在请求外跑一次提取，模型输出一份 JSON 增删改清单，
没有值得记的内容就返回空清单（prompt 明确「空是大多数情况」），只记关于用户
本人的持久事实，不记任务内容与闲聊，避免清单很快被垃圾淹没。

### 后台任务挂在流内部，而不是 BackgroundTasks

SSE 流式响应里 FastAPI 的 `BackgroundTasks` 语义别扭，请求级 session 也早已关闭。
`fire_and_forget`（`app/background.py`）直接在 `stream_answer` 内部挂协程，
任务自开 DB session；提问、失败重试、确认后续答三个入口自动全覆盖。任务对象放
模块级强引用集合（事件循环只弱引用 task，不 hold 住会被 GC 提前回收），
done 回调回收。客户端中途断开导致某轮没跑也不欠账：下一轮成功回答时全量重算
会把缺口补上。

### 字符预算，不引 tokenizer

全系统统一用字符数做预算，记忆域也不引入 tokenizer 依赖：摘要输入 12000、
摘要正文 1500、提取时单条消息截 2000（报告讨论首条可能长达 2 万字符，原样喂
既烧钱又稀释信号）。摘要 prompt 还明确要求剥掉正文里的 `[1][2]` 引用编号——
编号对应的 citations 不在后续上下文里，留着只会误导模型。

### 注入有顺序、条数有硬顶

system prompt 拼装顺序固定：**人设 → 记忆 → 摘要 → 引用规则 → 运维规则**，
硬约束一律追加在末尾。记忆注入取最近更新的 30 条（`MEMORY_PROMPT_LIMIT`），
全表 50 条硬顶（`MEMORY_MAX_ITEMS`，满了丢弃新条并记日志），防止长期使用后
记忆淹没 system prompt。`MEMORY_RULES` 同时规定「当下说的为准」，避免旧记忆
压过用户刚说的新情况；模型不主动复述记忆清单。

### 失败静默

摘要、提取、增强注入是观测/增强通道：任一步失败只记日志、下一步照常；
坏 JSON 宽松解析失败就跳过本轮，绝不把错误抛进回答链路。

### 明确不做的

- 记忆不做结构化分类——分类体系的维护成本远超它对注入效果的贡献，一条记忆就是
  一句话。
- 会话 RAG（历史会话语义检索）暂缓——信噪比低，还引入 embedding 指纹漂移问题。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `ChatSummary` / `UserMemory` | 摘要表（一会话一行）与记忆表（owner 作用域）；`Agent.use_memory` 开关 |
| `backend/app/services/chat/memory.py` | 窗口切分、摘要全量重算（full/merge 两套 prompt）、记忆 JSON 增删改提取、注入拼装、常量预算；同批操作与存量记忆一起规范化查重 |
| `backend/app/services/chat/service.py` | system prompt 按「人设→记忆→摘要→引用→运维」注入；三个回答入口落库后 `fire_and_forget(after_answer_task)`；任务体内延迟导入解循环依赖 |
| `backend/app/background.py` | `fire_and_forget`：模块级强引用集合 + done 回调回收 |
| `backend/app/routers/memory.py` | 记忆 CRUD 与一键清空（`GET/POST/PUT/DELETE /api/memory`，全走 owner 作用域，越权 404） |
| `backend/app/services/agents.py` | `use_memory` 开关随智能体 CRUD 贯通；与 `use_knowledge`/`use_ops` 同构 |
| `backend/app/bootstrap.py` + `docs/schema.sql` | 新表 `create_all`、新列幂等 ALTER；seed 刷内置开关不动用户已改的 `enabled` |
| `frontend/src/views/Settings.vue` | 「长期记忆」Tab：列表、新增/编辑弹窗、单删、清空 |
| `frontend/src/views/Agents.vue` | 智能体卡片上的「长期记忆」开关 |

## 升级行为

- 新表由 `create_all` 建立，`use_memory` 等新列走 `migrate_schema` 的幂等 ALTER。
- seed 内置智能体时只补开关默认值，不覆盖已存在智能体的 `enabled` 状态。
- 上线前的老会话没有摘要行时按全窗口回放，行为与升级前一致。

## 测试

- `backend/tests/test_chat_memory.py`（38）：窗口切分、摘要全量重算与超限合并、
  记忆提取的增删改与去重（同批重复 + 与存量重复）、空清单、坏 JSON、门控
  （未开开关不消费不积累）、注入条数与顺序、50 条硬顶、清空、属主隔离。
- `backend/tests/test_background.py`（1）：`fire_and_forget` 的强引用保活。
