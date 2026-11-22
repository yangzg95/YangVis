# 「创作」模块设计（写小说 / 写文章）

> 状态：已评审，决策已锁定（见文末「锁定决策」）。M1 骨架实现按本文档执行。
> 代码约定参照：幻灯片（`slide_deck` + 后台生成 + 轮询）、求职笔记（CRUD/owner 隔离）、
> 短视频工作台（in-shell 动态标签 + keep-alive）。

## 1. 总体形态

一个菜单叶子 + 页内两个 tab（与 `Tools.vue` 两页签同模式），不拆两个菜单：

| 项 | 方案 |
|---|---|
| 菜单位置 | 「AI 应用」组内、素材库之后，标题「创作」，图标 `EditOutlined` |
| 列表路由 | `/studio`，页内 `a-tabs`，tab 同步 query（`/studio?tab=novel`），刷新/分享不丢态 |
| 工作台路由 | `/studio/works/:id(\d+)/workbench`，不挂 bare，仿 `VideoWorkbench`：顶栏按作品 id 占动态标签，keep-alive 保草稿 |
| 动态标签 | `utils/appTabs.ts` 加 `STUDIO_WORKBENCH_RE`，标题「创作 #id」，配套补 appTabs 单测 |
| 涉及壳文件 | `router/index.ts`、`App.vue`（MENU 叶子）、`utils/appTabs.ts`、`main.py`（路由注册） |

两个 tab 共用一套「作品列表 → 工作台」骨架，差异只在参数表单、大纲形状和生成流程；
后端一张作品表 + `kind` 区分，避免两套 CRUD。

## 2. 两个 tab 的功能差异

| 维度 | 写小说（novel） | 写文章（article） |
|---|---|---|
| 作品含义 | 一本书 | 一篇文 |
| 创作参数 `params` | 题材、风格、基调、视角、主角、世界观、目标字数 | 文章类型（公众号/知乎/小红书/技术博客/公文/软文）、平台、受众、语气、核心观点、目标字数 |
| 大纲形状 | 两级树：卷 → 章，章节点带 synopsis、目标字数 | 一级列表：段落 section，带观点 point、目标字数 |
| 正文载体 | 每章一个 part，独立生成/续写/重写 | 可整篇一次生成（按 `##` 标题切回各 section），也可逐段生成 |
| AI 操作 | 生成大纲、章节起草、续写、重写、润色 | 生成大纲（含标题候选）、全文起草、段落重写、润色/扩写/缩写/换语气 |
| 导出 | 拼接整本 Markdown/TXT（卷章标题自动分级） | 整篇 Markdown/TXT |

## 3. 数据模型（两张新表）

实体约定：`owner_id` 隔离、JSON 列存结构、**行上不存 status**（状态从 `workflow_run`
翻译，与 `slide_deck` 同一取舍）、`docs/schema.sql` 与 `entities.py` 双写。

### 3.1 `writing_work`（作品）

| 列 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK AUTO_INCREMENT | |
| owner_id | BIGINT NOT NULL, index | |
| kind | VARCHAR(8) NOT NULL | `novel` / `article`，服务端白名单 |
| title | VARCHAR(128) NOT NULL | |
| brief | LONGTEXT NOT NULL | 灵感/主题原文，生成输入的快照 |
| requirement | VARCHAR(512) NULL | 补充要求，重生成时仍生效（同 deck.requirement） |
| params | JSON NOT NULL | 创作参数，服务端按 kind 白名单清洗，未知键丢弃 |
| outline | JSON NOT NULL | 大纲树（形状见 3.3），cid 服务端发放 |
| meta | JSON NOT NULL | 副产物：`{title_candidates, tags}` |
| created_at / updated_at | DATETIME | 同款 server_default / onupdate |

索引：`idx_writing_work_owner_kind (owner_id, kind)`。

### 3.2 `writing_part`（章节/段落正文）

| 列 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK AUTO_INCREMENT | |
| owner_id | BIGINT NOT NULL, index | |
| work_id | BIGINT NOT NULL, index | |
| cid | VARCHAR(32) NOT NULL | `p` + uuid4().hex[:16]，与 slides 的 sid 同形同约定；数组下标永不参与寻址 |
| sort_order | INT NOT NULL | 拖拽排序落这里 |
| title | VARCHAR(128) NOT NULL | 章节名/段落名 |
| content | LONGTEXT NOT NULL | markdown 纯文本 |
| word_count | INT NOT NULL DEFAULT 0 | 服务端统计（CJK 字符 + 英文词，剥离 markdown 符号），不信前端报数 |
| created_at / updated_at | DATETIME | |

约束：`UNIQUE(work_id, cid)`，索引 `(owner_id, work_id, sort_order)`。

### 3.3 大纲节点统一形状

小说卷节点不建 part，叶子节点与 part 按 cid 1:1 对齐：

```json
[
  {"cid": "p3f9…", "kind": "volume", "title": "第一卷", "synopsis": "", "children": [
    {"cid": "pa12…", "kind": "chapter", "title": "第一章 雨夜", "synopsis": "…", "target_words": 3000}
  ]}
]
```

文章是 `kind: "section"` 的扁平一级（字段同义，`point` 复用 synopsis 字段承载）。
所有 cid 在服务端 sanitize 时补齐（仿 `sanitize_pages`），模型与前端发的非法 cid 一律重发。

## 4. 后端设计

### 4.1 目录与接口

新增 `services/writing/`：

- `service.py`：CRUD、序列化、导出拼装
- `outline.py`：大纲白名单清洗、cid 发放、part 对账
- `words.py`：字数统计纯函数
- `generate.py`：M2/M3 的后台任务体（M1 不建）

新增 `routers/writing.py`（prefix `/studio`，`main.py` 注册，tags=["studio"]）。

| 方法/路径 | 作用 | 里程碑 |
|---|---|---|
| GET `/studio/works?kind=&keyword=` | 列表，不含正文（item/detail 分离，同 note） | M1 |
| POST `/studio/works` | 只建行，不自动触发生成；参数先落库，工作台里手动点生成 | M1 |
| GET `/studio/works/{id}` | 详情：作品 + 大纲 + parts | M1 |
| PUT `/studio/works/{id}` | 改标题/要求/params | M1 |
| DELETE `/studio/works/{id}` | 有运行中任务时拒绝（`running_subjects` 闸门，同 deck 删除） | M1 |
| PUT `/studio/works/{id}/outline` | 保存大纲树；清洗后对账 part 行（新节点建空 part，删节点连带删 part，前端二次确认） | M1 |
| POST `/studio/works/{id}/parts` | 手工加章：同一事务补大纲叶子 + 空 part | M1 |
| PUT `/studio/works/{id}/parts/{cid}` | 自动保存（title/content），服务端重算 word_count | M1 |
| DELETE `/studio/works/{id}/parts/{cid}` | 同时摘掉大纲节点 | M1 |
| GET `/studio/works/{id}/export?format=md\|txt` | 导出，仿 slides export 的 no-store/attachment 头 | M1 |
| POST `/studio/works/{id}/outline/generate` | 起大纲运行，body 可带 requirement 覆盖 | M2 |
| POST `/studio/works/{id}/parts/{cid}/generate` | 正文生成，body `{action, instruction?, tone?}` | M3 |
| POST `/studio/works/{id}/draft/generate` | 文章整篇生成（小说批量章节生成留 backlog） | M3 |

### 4.2 两个新 subject 常量（`services/workflows/__init__.py`，M2 启用）

- `SUBJECT_WRITING_WORK = "writing-work"`：作品级运行（大纲 / 文章整篇起草），ref=作品 id
- `SUBJECT_WRITING_PART = "writing-part"`：单块正文运行，ref=part 的 cid 字符串
  （不用数字 id，避免在接口里暴露内部主键）

列表/工作台状态全部经 `subject_state(latest_run(...))` 翻译；失败原因点 tag 进
`WorkflowRunsDrawer`，与幻灯片列表完全一致。

### 4.3 五个内置智能体 + 五条内置工作流（M2/M3）

随启动 seed（`chat_visible=False`，不进对话页人设选择）：

| slug | 人设 | 温度 | 输出 | 里程碑 |
|---|---|---|---|---|
| novel-architect | 小说架构师：灵感+设定 → 卷章大纲 JSON | 45 | JSON | M2 |
| article-architect | 文章架构师：主题+类型+受众 → 段落大纲 + 标题候选 | 35 | JSON | M2 |
| novel-writer | 小说写手：设定+前情+本章 synopsis → 章节 markdown | 55 | markdown | M3 |
| article-writer | 文章写手：大纲 → 整篇 markdown | 40 | markdown | M3 |
| writing-editor | 文字编辑：润色/扩写/缩写/换语气/重写 | 25 | markdown | M3 |

工作流 slug：`novel-outline`、`article-outline`、`novel-chapter`、`article-draft`、
`writing-polish`（两个 kind 共用，action 段落由调用方拼好传变量，与 builtin.py
现有条件段落拼装法一致）。JSON 解析复用 `strip_json_fence` / `json_span`，
错误文案复用 `model_error_text`，模型门槛复用 `require_chat_model`
（前端复用 `settingsApi.status().chat.ready` 告警条）。

### 4.4 正文生成的上下文策略（M3，长篇关键）

`novel-chapter` 输入拼装：

1. 作品卡（params 关键字段）+ 完整大纲（只含 synopsis，很短）
2. 前情：最近一章结尾 1500 字；更早章节只给「标题 + synopsis」清单
3. 本章 synopsis、目标字数、用户 instruction
4. 总量过 `WRITING_CONTEXT_MAX_CHARS` 时同 slides `_truncate` 截断并标注

七个 action：`draft`（首写）/ `continue`（带本章已有内容尾部续写）/
`rewrite`（带全文重写，前端 popconfirm 覆盖）/ `polish` / `expand` /
`condense` / `retone`（带 tone 参数）。

**生成中该 part 锁定编辑**（textarea disabled），完成后服务端写回、前端拉新详情——
天然避开自动保存与 AI 回写的冲突，不做三方合并。

文章整篇生成后按 `/^## /m` 切分，与大纲叶子数量对得上就 1:1 写回；对不上则整篇落第一个
section 并记 warning（不静默丢内容）。空输出等后处理失败统一 `fail_run` 降级，
不允许「succeeded 但没产物」。

后台任务体照抄 slides `_generate_task`：函数内延迟 import `SessionLocal`，
新开会话、异常只落 run 不抛出，保证测试可 monkeypatch。

### 4.5 config.py 限制项

```python
WRITING_MAX_WORKS_PER_OWNER       = 100
WRITING_MAX_PARTS_PER_WORK        = 200
WRITING_OUTLINE_MAX_NODES         = 120
WRITING_BRIEF_MAX_CHARS           = 20000
WRITING_PART_TARGET_MAX_WORDS     = 6000   # 单章目标上限，防 token 爆炸
WRITING_PART_CONTENT_MAX_CHARS    = 30000  # 入库闸门
WRITING_CONTEXT_MAX_CHARS         = 12000  # 喂模型的上下文窗口闸门（M3）
```

## 5. 前端设计

### 5.1 新增文件

- `api/writing.ts`（信封协议同 notes.ts）+ `api/index.ts` barrel 导出
- `views/Writing.vue`：两 tab 列表页
- `views/WritingWorkbench.vue`：三栏工作台
- `components/writing/`：`WorkCreateModal.vue`（novel/article 两套参数表单）、
  `OutlineTree.vue`（hover 上移/下移 + M4 起 a-tree 拖拽，触屏保留按钮）、`PartEditor.vue`、
  `OutlineGenerateModal.vue`（M2）、`PartGenerateModal.vue` / `DraftGenerateModal.vue`（M3）
- `utils/writingWords.ts`、`utils/writingOutline.ts`（各带 `.test.ts`）

### 5.2 列表页

tab 由 route query 驱动；表格列 = 标题（副标题带题材/类型）、状态 tag
（M2 起 analyzing/ready/error，点击开运行抽屉；块级在跑时其下另挂橙色
「章节/段落生成中」标签）、写作进度（`已写 x/y 章` + mini 进度条 + 总字数，
零块大纲显示「待建大纲」）、更新时间（下附创建时间）、操作
（工作台/删除/导出）；`createPollLoop` 在有 analyzing 行或任一行
`part_running` 时轮询，`onDeactivated` 停表；完成跃迁发带「打开工作台」
按钮的 notification。全部复用 Slides.vue 已验证写法。

### 5.3 工作台三栏

- 左栏（260px）：大纲树，节点显示生成状态点（M3：analyzing 转圈 / error 红点，
  点红点开该块运行记录）+ 字数，hover 出增删/上下移，底部「+ 章节」；窄屏收成抽屉。
  M4 起 a-tree 支持拖拽排序（含小说跨卷移动，落点规则见 M4 实现备注），触屏无
  HTML5 拖拽，上移/下移按钮在窄屏常显作为触摸排序手段
- 中栏：part 标题输入（M3 起标题行右侧带「AI 写作」入口）+ 等宽大字框
  （IBM Plex Mono 已是依赖），状态条放块级生成状态、本 part 字数、保存状态
- 右栏：`utils/markdown.ts` 基座实时预览（`html:false` 安全底线现成）；
  < lg 断点改成「编辑/预览」分段切换
- 顶栏：作品名、kind 标签、全书总字数、导出下拉、空大纲时的「生成大纲」主按钮（M2）；
  文章在大纲生成后出现「标题候选」下拉（M4，读 `meta.title_candidates`，点选即改名）

### 5.4 自动保存

停笔 2.5s debounce PUT，Ctrl+S 立即 flush，`onDeactivated` 兜底 flush；
状态文案「未保存 / 保存中 / 已保存 HH:mm:ss」。M3 起生成中的 part 走锁定态，
失败保留旧内容 + 错误 tag + 重试入口。

## 6. 测试与里程碑

后端 `tests/test_writing.py` + `tests/test_writing_api.py`（仿 test_slides.py）：
owner 隔离 404、cid 发放与大纲清洗、字数统计、运行中拦截删除/重复触发（M2）、
任务成功写回/失败 `fail_run`/空产物降级（M3）、文章切分（M3）、导出拼接。
前端：appTabs 新正则单测、字数与大纲清洗纯函数单测。

| 里程碑 | 范围 | 交付后状态 |
|---|---|---|
| M1 骨架 | 两表+DDL+CRUD+列表两 tab+工作台手工写作+自动保存+导出 | 不接 AI 已可当纯写作工具用 |
| M2 大纲 | 两个 architect + 两条 outline 工作流 + 后台生成 + 轮询 + 运行抽屉 | AI 出大纲 |
| M3 正文 | 三个写手/编辑工作流 + 七个 action + 生成锁定 + 整篇切分 | 全链路可用 |
| M4 打磨 | 标题候选、a-tree 拖拽排序、窄屏实测收口、测试全绿、交接文档收口 | 收口 |

> M1 实现备注（2026-11）：大纲树的排序用 hover 上移/下移按钮落地（同层相邻交换，
> 卷内章不跨容器），a-tree 拖拽排序按上表移到 M4——拖拽的跨卷落点规则需要单独的
> 单测与实测，按钮已覆盖 M1 全部排序场景。窄屏已做基础适配（大纲抽屉 + 编辑/预览分段），
> 实测留 M4。M1 测试：后端 33 个（test_writing.py + test_writing_api.py），
> 前端新增 writingWords/writingOutline/appTabs 共 19 个用例。
>
> M2 实现备注（2026-11）：
> - 两个 architect 内置智能体（novel-architect / article-architect，chat_visible=False）
>   与两条单步内置工作流（novel-outline / article-outline）随启动 seed，prompt 走
>   条件段落拼装（brief/params/requirement 空则整段不出现），max_nodes 传 120。
> - 生成链路与幻灯片同构：请求里 `start_builtin` 建认领作品的 `workflow_run`
>   （subject=`writing-work`），`BackgroundTasks` 跑模型；本表不存状态，序列化由
>   `subject_state(latest_run(...), has_product=大纲有叶子, idle=uploaded)` 翻译；
>   解析不出结构 / 一个合法叶子都没有 → `fail_run` 降级，不允许「succeeded 但没产物」。
>   模型产物不带 cid，cid 发放、层级收窄、节点上限、part 对账全部复用手工 `save_outline`
>   同一道闸门；文章标题候选（去重、≤5 条、每条 ≤128 字）落 `meta.title_candidates`。
> - **按次选模型（评审后追加）**：请求体 `model_config_id` 可指定本次运行的对话模型，
>   不传 = 默认对话模型。请求时 `require_chat_model` 先验归属/用途/测通，再把 id 盖到
>   该运行预建的 pending 步骤行上；执行器（exec.py）的解析优先级改为
>   「运行行预盖 > 流程定义按步覆盖 > 默认模型」，行上最终落实际使用的配置 id。
> - 前端：列表加状态列（analyzing 转圈 / ready / error，点 tag 开 WorkflowRunsDrawer）、
>   就绪告警条、仅在有 analyzing 行时跑 createPollLoop、完成跃迁发带「打开工作台」的
>   通知、生成中禁删；工作台顶栏加「生成大纲 / 重新生成」（后者 popconfirm 警示结构
>   重建会删孤儿正文）与运行记录入口，生成弹窗含模型下拉（只列本人测通的 chat 配置，
>   allow-clear，占位「默认对话模型」）+ 本次补充要求。
> - **生成中编辑冻结（走查后补强）**：大纲落库是整树覆盖 + part 对账（模型产物不带
>   cid，旧叶子按孤儿删除），运行中允许手改等于让用户白写。后端在 save_outline /
>   add_part / update_part / delete_part 四个写入口前统一加 `_assert_not_generating`
>   闸门（running_subjects 判定，路由翻译成 code=-1 业务信封）；后台任务自己的
>   save_outline 发生在运行 succeeded 之后，不会被挡。前端 OutlineTree 与 PartEditor
>   新增 `locked` 入参，analyzing 时按钮/输入框全部禁用并给出锁定提示，与幻灯片
>   工作台的 generating 禁用同模式。
> - **状态翻译特判**：通用 `subject_state` 把「succeeded 但无产物」判失败（为步骤
>   重跑兜底）；创作侧的空产物在任务里已被 fail_run 降级成 failed 运行，所以序列化时
>   对「succeeded 运行 + 空大纲」特判回 uploaded——生成成功后手工清空大纲不再亮红色
>   「失败」标签，而是回到「待生成」。
> - M2 测试：后端 65 个——test_writing.py 33（generate.py 纯函数 9 个，含栅栏
>   JSON / 裸数组 / 非法 JSON / 候选去重截断）、test_writing_api.py 18（HTTP 链路
>   9 个，含 4004 引导信封、跨账号 404、运行闸门 code=-1、模型盖戳与越权 404）、
>   test_writing_flows.py 14（工作流集成：成功/失败/空产物降级/重复触发/删除闸门/
>   模型选择/要求快照/运行中编辑冻结/手工清空的状态翻译）；test_agents.py 的内置
>   人设两份硬编码清单同步两个 architect（33 个全绿）。全量回归 1951 passed，
>   仅清单测试在同步前失败过 2 个。前端 151 个用例全绿（新增 runLinks 深链 4 个，
>   其余 147 个既有），vue-tsc 与生产构建通过。
> - 实现中修掉的真实缺口：执行器原先只读流程定义步骤的 model_config_id，运行行
>   预盖值会被默认模型覆盖；`_json_candidate` 先找对象跨度导致裸数组输出被内层对象
>   截胡（改为按开括号先后取最外层跨度）；走查还清掉了两个弹窗里不产生提示的
>   `void errorText(...)` 死代码、未挂出的 `WritingService.snippet` 与 popconfirm
>   上无效的 disabled 属性。
>
> M3 实现备注（2026-11）：
> - 三个内置智能体（novel-writer 55 / article-writer 40 / writing-editor 25，
>   均 chat_visible=False）与三条单步 markdown 工作流（novel-chapter / article-draft /
>   writing-polish）随启动 seed，test_agents.py 两份硬编码清单同步到 33 个全绿。
>   七个 action 中 draft/continue 走 novel-chapter（小说专属），五个编辑动作两个
>   kind 共用 writing-polish，动作指令由 `action_note` 拼好以变量传入；文章没有
>   单块起草，整篇走作品级 article-draft。
> - **两级生成锁定**：作品级运行（大纲/整篇起草，subject=writing-work）冻结全部
>   手工入口；块级运行（subject=writing-part，ref=cid）只锁自己那一行的编辑/删除与
>   重复触发，别的章节照写、不同 cid 允许并行起运行；任一单块在跑时结构类操作
>   （save_outline/add_part/delete_work/重发大纲）统一拒绝。M2 的单闸门拆成
>   `_assert_work_idle` / `_assert_parts_idle` / `_assert_part_idle` 三道，后台任务
>   自身的写库发生在运行 succeeded 之后，不会被闸门误伤。
> - **块状态翻译**：`to_detail` 批量取 `latest_runs(writing-part, cids)`，每块经
>   `subject_state(has_product=正文非空, idle="idle")` 翻成 idle/analyzing/ready/error；
>   与 M2 作品级同口径，「succeeded 运行 + 手工清空正文」特判回 idle，不亮红色失败。
> - **整篇切分**：`split_article_draft` 先剥 markdown 栅栏（共享的 strip_json_fence
>   只认裸栅栏/json，创作侧本地放宽到任意语言标识，如 ```md），再按 `^## ` 切；
>   块数与大纲叶子 1:1 才逐段落写回，对不上不静默丢内容——整篇落第一个段落，
>   warning 落 `meta.draft_warning` 由工作台告警条展示（本机可关，下次成功起草由
>   服务端清除），运行仍记 succeeded。
> - 前端：PartGenerateModal（动作下拉按 kind/是否有正文置空块编辑动作，retone 显
>   语气必填项，可选额外要求 + 按次选模型）、DraftGenerateModal（模型选择 + 切分
>   规则说明）；PartEditor 标题行加「AI 写作」入口与块级状态（转圈/红色失败点，
>   失败点开该块运行记录抽屉），analyzing 结束的跃迁把成稿装回本机草稿；
>   OutlineTree 每行状态点与 cid 级禁用，失败点点击开 writing-part 运行抽屉；
>   轮询条件扩成「作品 analyzing 或任一块 analyzing」，跃迁提示按 cid 对账，
>   切换选中章节不会误报。设计稿里 rewrite 的二次确认未单独做——动作弹窗本身就是
>   显式手势，且五个编辑动作都是整文替换，逐个 popconfirm 只会添堵。
> - M3 测试：后端写作三套共 99 个——test_writing.py 44（新增 11 个 generate.py
>   纯函数：动作路由/action_note/小说输入拼装/前情 1500 字闸门/整篇切分含栅栏与
>   对不上回退）、test_writing_flows.py 28（集成：起草写回/失败保原稿/空产物降级/
>   续写带原文/动作闸门/两级锁定/整篇 1:1 切分与对不上 warning/按次选模型盖戳/
>   手工清空回 idle）、test_writing_api.py 27（新增 9 个 HTTP 用例：422/业务信封/
>   4004/跨账号 404）。前端 156 个全绿（新增 writingActions 5 个、runLinks 补
>   writing-part 无深链断言），vue-tsc 与生产构建通过。
>
> M4 实现备注（2026-11，收口）：
> - **标题候选**：数据在 M2 已落 `meta.title_candidates`（文章专属，去重 ≤5 条、
>   每条 ≤128 字），M4 只补 UI——工作台顶栏文章且有候选时出现「标题候选」下拉，
>   点选即走既有 `writingApi.update` 改名，当前标题置灰标「当前」；小说不产生候选，
>   入口不渲染。候选只在重新生成大纲时由后端刷新，改名不删候选、不写 meta。
> - **a-tree 拖拽排序**：落点规则全部收在 `utils/writingOutline.ts` 的纯函数里
>   （`planDrop` 出「摘除容器+目标容器+下标」计划，`canDropNode` 给 allowDrop、
>   `applyNodeDrop` 落地，no-op/非法返回 false 不发请求），规则与后端
>   sanitize_outline 的两层树形状对齐：文章只接受根层缝隙（inside 非法）；卷只能
>   根层重排、禁止嵌套（嵌套卷会被后端清洗成章，前置拦掉）；章可同卷/跨卷挪、
>   落卷上=入卷、落根层缝隙=提为顶层章、章对章 inside 非法。组件侧 allowDrop 收
>   -1/0/1 三态，drop 事件用 rc-tree 的 `dropPosition - pos 末段下标` 反推方向
>   （Tree.js 抛出值就是相对三态加兄弟下标，与 antd 官方拖拽示例同款算法）。
>   hover 上移/下移按钮保留：触屏没有 HTML5 拖拽，窄屏按钮常显作为触摸排序手段。
>   拖拽保存复用既有 PUT outline，后端按 cid 对账只改 sort_order，跨卷移动正文
>   随行，无后端改动。
> - **窄屏收口**：大纲抽屉宽度改 `min(300, 100vw-24px)`，320px 屏不再贴边；
>   ≤768px 树行操作按钮常显（触屏无 hover）、PartEditor 状态条允许换行、顶栏操作
>   组换行且长标题 46vw 省略；弹窗靠 antd 自带 ≤575px `max-width:calc(100vw-16px)`
>   响应式，不另写。真机手势/视觉实测仍需人工过一遍。
> - M4 测试：前端 170 个全绿（writingOutline 新增 14 个拖拽用例：文章缝隙/inside
>   拦截、小说同卷/跨卷/入卷/提顶层/卷重排/卷嵌套与卷入卷拦截/章 inside 拦截/
>   no-op 与未知 cid），vue-tsc 与生产构建通过；后端无代码改动，全量回归
>   pytest-xdist `-n auto`：1989 passed in 206s（81 个既有 warning，零失败）。
>
> 走查后补强（2026-11，M4 收口后的全链路交互走查，七项）：
> - **修掉一个真实数据丢失缺口**：`run_outline_generation` 原先先 `save_outline`
>   （内部提交并按 cid 对账、删孤儿正文块）再检查叶子，模型回一个合法但为空的
>   `{"outline": []}` 就会把既有大纲和全部正文清空、只把运行标失败。改为先
>   `sanitize_outline` + `flatten_leaves` 非空校验，空结果直接抛错、完全不触库，
>   旧大纲与正文原样保留；新增回归测试钉死（运行 failed、块数/cid/标题/正文全等）。
> - **列表页补「有块在跑」事实源**：作品级 `status` 只翻译 writing-work 运行，
>   章节正文生成（writing-part）在跑时列表行没有任何信号，删除按钮可点（被后端
>   结构闸门拒回，体验是一次无谓报错）。`WritingWorkItem` 新增 `part_running`：
>   `to_item` 直查该作品块级运行；`to_items` 批量路径两条查询取回（work_id→cid
>   映射 + running_subjects），不是每行一次；`to_detail` 复用已翻译的块状态、零额外
>   查询。前端列表删除按钮在 `status==='analyzing' || part_running` 时置灰并给
>   tooltip（disabled 按钮不发鼠标事件，外套 span 才挂得上提示），后端删除闸门
>   （`_assert_parts_idle`，code=-1 信封）保持不动并由 HTTP 测试钉死。
> - **整篇起草覆盖告警**：DraftGenerateModal 在任一段落已有正文时显示 warning
>   a-alert——整篇起草逐段替换全部现有内容、不保留历史版本，需要保留请先导出；
>   弹窗同时改为每次打开重拉模型列表。
> - **摘要框的结构锁**：摘要保存走整树 PUT outline，任一正文块在跑就会被结构闸门
>   拒；PartEditor 新增 `structureLocked`（接工作台 treeLocked），只禁用摘要框并
>   提示原因，标题/正文仍可写——块级锁只挡自己那个 cid，兄弟章节生成时本块照常写作。
> - **三个生成弹窗每次打开重置表单并重拉模型列表**：去掉 modelsLoaded 缓存，
>   「去配置」配好模型回来立即可见，语气/要求/模型选择不跨次记忆。
> - **删除文案解绑运行类型**：`delete_work` 拦截文案从「正在生成大纲」改为
>   「作品正在生成中，请稍后再删」（块级/整篇起草在跑时同走这道闸门）。
> - **窄屏补漏**：PartEditor 状态条允许换行（四项不再挤爆），与 M4 窄屏收口同断点。
> - 测试：后端写作三套共 103 个——test_writing.py 44、test_writing_flows.py 30
>   （新增空响应保数据、part_running 三序列化路径跟随 2 个）、test_writing_api.py
>   29（新增排队快照 part_running 与阻塞式在途用例 2 个：独立 inspector 连接轮询
>   RUN_RUNNING、第二 TestClient 验列表/详情/删除闸门）；全量回归 1993 passed
>   in 331s（81 个既有 warning，零失败）。前端保持 170 个用例全绿，vue-tsc 与
>   生产构建通过。
>
> 列表信息密度增强与工作台滚动修复（2026-11）：
> - **后端新增 `written_count`**：成稿块数随 `_counts_map` 一次分组查询取回
>   （块数/总字数/成稿数同一条 SQL），判定口径是 `word_count > 0` 而不是
>   `trim(content) != ''`——sqlite 的 trim 只去 0x20、不去 `\n\t`，与 Python
>   strip 不同口径；word_count 本就在每次写入时由 count_words 重算（纯空白/
>   标点为 0），与列表展示的字数同源，跨 sqlite/mysql 行为一致。`to_detail`
>   的 Python 侧计数严格同口径，避免双份真理。
> - **列表行丰富化**：进度列从「x 章 · y 字」升级为「已写 x/y 章 + a-progress
>   mini 进度条 + 千分位总字数」，零块显示「待建大纲」；状态列在作品级 tag 下
>   挂橙色「章节/段落生成中」标签（part_running 独立于作品级 status）；时间列
>   补创建时间；轮询条件从只认作品级 analyzing 扩为 `analyzing || part_running`，
>   块级生成结束后标签与进度条自动收口。
> - **修工作台双滚动条/页面空白**：根因有三处——`.studio-workbench` 用
>   `min-height: calc(100vh-120px)` 自然高度被长正文撑破外层 `.dashboard-main`
>   （它才是设计中的页面滚动容器）；`.wb-body` 外包的 a-spin 两层容器是 block，
>   截断了 flex 高度链，`wb-body` 的 flex:1 没有着力点；`.wb-main`/`.pane` 缺
>   `min-height:0`，flex 子项默认 min-height:auto 被内容撑到自然高度，栏内
>   `overflow:auto` 永不触发。修法：工作台改 `height:100%; overflow:hidden`
>   填满 dashboard-main 内容盒，`:deep()` 把 a-spin 两层容器补成 flex 列，
>   高度链补齐 min-height:0，滚动只发生在大纲树/编辑框/预览三栏内部。
> - 测试：后端写作三套 104 个（flows 新增 written_count 全生命周期 1 个、
>   api 在既有用例上补字段断言），全量回归 1994 passed in 192s（零失败）；
>   前端 170 个用例全绿，vue-tsc 与生产构建通过。

v1 backlog（明确不做）：SSE 逐字流式、知识库 RAG 喂设定、小说批量章节生成、
作品回收站（v1 同 slides 硬删除）、与短视频人物库联动。

## 7. 锁定决策

1. **生成形态 = 后台运行 + 轮询**：与幻灯片/短视频/简历同构，`workflow_run` 单一状态源、
   运行抽屉可回看、断网切页不影响任务、测试模式现成；不引入 SSE 逐字流式。
2. **一个菜单叶子 + 页内两 tab**：路由 `/studio`，不拆两个菜单。
3. **正文统一落 `writing_part`**：小说章节与文章段落同表，靠 work.kind 区分语义，
   编辑器与导出只有一条代码路径。
4. **创建只建行不自动生成**：参数先落库，生成动作在工作台由用户显式触发。
5. **生成中锁定 part 编辑**：不做自动保存与 AI 回写的三方合并。
6. **v1 硬删除**：不做回收站，与幻灯片/笔记一致。
