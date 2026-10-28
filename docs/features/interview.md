# 智能办公 · 面试记录

侧栏「AI 应用 → 面试」页（路由 `/office/interviews`；早期版本是「简历」页里的
「面试记录」tab，因实体与接口完全独立而拆出）。一场面试一条记录（公司、岗位、日期、轮次、结果、
整场复盘备注）加一份问题清单；问题由用户手工录入（可附「我当时的回答」与备注），
每题可点「生成参考答案」让 AI 后台产出一段 markdown 答案。问题明细在新开标签页的整页
详情（`/office/interview/:id`，`bare` 路由）里处理，列表只管场次元数据。

## 用户可见行为

- 列表：按公司/岗位前端筛选，展示公司岗位、面试日期、轮次、结果（待定/通过/未通过/
  已 offer 四种彩色标签）、题目数、记录时间；行内可编辑元数据、删除、详情新标签页打开
- 「记录一场面试」弹窗：公司、岗位必填，日期可选，轮次是可自由输入的自动补全
  （一面/二面/三面/HR面/电话面/笔试/终面），可写整场复盘备注
- 详情页顶部 descriptions 展示元数据与「记录时间」，可编辑、可删除（删除有二次确认，
  有题目正在生成答案时后端拒绝）
- 问题区：顶部两个框快速添加「面试官问了什么 + 我当时怎么答的」；每题卡片查看态显示
  题干、我的回答、备注，可内联编辑或删除
- 每题一个「生成参考答案」按钮：未配置可用对话模型时禁用；点击后接口立即返回，按钮变
  「正在生成…」，页面每 2.5 秒轮询详情，没有 analyzing 的题就停表；用户正在内联编辑某题
  时跳过当轮刷新，避免冲掉草稿
- 答案就绪后默认展开一次（用户刚点的生成要直接看到结果），之后可折叠；markdown 渲染，
  内容来自内置工作流 `interview-answer`（人设 `interview-answerer`「面试答题教练」），
  固定「考察点 / 答题思路 / 参考答案 / 可能的追问」结构，提供了当时回答会先点评得失
- 生成失败题卡内联显示错误，可重新生成；只要发起过（状态非 none）就显示「运行记录」，
  打开统一的 WorkflowRunsDrawer（subject 为 `interview-question` + qid）
- 同一题重复触发生成会被拒绝；题目或整场记录在生成中删除也会被拒绝

## 关键取舍

### questions 是记录上的 JSON 子列表，不建子表

一场面试几十条题、没有跨记录检索题目的需求，独立子表只带来 join 和级联，取舍同
`resume_doc`。每题带服务端签发的 `qid`（`secrets.token_hex(6)`），题目级端点与后台回写
都按 qid 寻址，不依赖数组下标——下标会在「删题的同时另一题正在生成答案」时漂移。

### 进度只存在运行记录上，JSON 里只存产物

每题只落 `ref_answer` 一个 AI 字段。「生成到哪了/为什么失败」是认领该题那次运行的事：
发起时建一条 `subject_kind='interview-question'`、`subject_ref=qid` 的内置工作流运行，
序列化详情时用每题最近一次运行翻译成 `ref_status`（none/analyzing/ready/error）与
`ref_error`。旧实现把 `ref_status` 也写在 JSON 里，一次生成两个事实源、重启要两边收尾，
现在只剩运行一份事实。题干在记录 JSON 里、运行按 qid 反查不到它，所以发起时抄一份
`subject_label`（公司 · 岗位 · 题干）用于运行流水展示。

### 三条写路径各管各的字段，不用锁

questions 会被三处并发写：`PUT /interviews/{id}` 只改元数据、根本不接收 questions；
题目级 PUT 只改 question/my_answer/note 且保留 `ref_answer`；后台任务只按 qid 改那一题
的 `ref_answer`，提交前先 `refresh` 把用户期间的改动合并进来，记录已被整条删除就丢弃
结果收工。整体替换 questions 的入口刻意不存在，否则会和后台回写互相覆盖。

### 生成走 BackgroundTasks + 轮询，不走 SSE

一次模型调用远超 SPA 的 30s axios 超时（与简历分析同一理由），所以是「接口先做模型配置
检查 → 建运行立即返回 → FastAPI BackgroundTasks 里跑模型 → 前端轮询」。后台任务函数体内
延迟导入 `SessionLocal` 并开新 session（测试要 monkeypatch，顶层导入替换不掉），任务内
任何异常不外抛，失败写在运行上。

### 面试记录与简历/报告没有数据关联

它是求职办公域里与简历平级的独立记录本，没有任何指向 Resume/报告的外键或引用；
参考答案的背景只来自该场记录的公司、岗位、轮次与本题的「我的回答」。详情页做成
bare 整页新标签页，与报告页同一外壳模式（无侧边栏、max-width 960px）。

### owner 隔离在 service 层收口

所有查询经 `InterviewService._scope()` 加 `owner_id` 条件，handler 拿不到裸 session；
他人的记录 id 一律读作 404，运行查询（`latest_run(s)`、`running_subjects`）同样带
owner，删除闸门读的是运行状态而不是 JSON 标记。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/routers/interview.py` | `/office/interviews` 场次 CRUD、题目级增删改、`/answer` 触发生成并挂 BackgroundTasks |
| `backend/app/services/interview.py` | `InterviewService`（作用域隔离、CRUD、`start_answer`/`run_answer`、运行→状态翻译）与 questions JSON 纯函数 |
| `backend/app/models/entities.py` `InterviewRecord` | 表 `interview_record`：场次元数据 + `questions` JSON（`[{qid,question,my_answer,note,ref_answer}]`） |
| `backend/app/models/schemas.py` | `InterviewCreate/Update/Item/Detail`、`InterviewQuestionIn/InterviewQuestion`（ref_status/ref_error 是接口翻译值） |
| `backend/app/services/workflows/builtin.py` | 内置工作流 `interview-answer`：单步骤，agent `interview-answerer`，输入 company/position/round_note/question/answer_note |
| `backend/app/services/agents.py` | 内置人设 `interview-answerer`「面试答题教练」，`chat_visible=false` |
| `backend/app/services/workflows/__init__.py` | `SUBJECT_INTERVIEW_QUESTION = "interview-question"` |
| `frontend/src/views/Interviews.vue` | 面试页外壳（page-card + 标题），渲染 InterviewRecords |
| `frontend/src/components/InterviewRecords.vue` | 场次列表：筛选、新增/编辑/删除弹窗、详情新开标签页 |
| `frontend/src/views/InterviewDetailView.vue` | bare 整页详情：元数据展示/编辑、题目添加与内联编辑、生成与 2.5s 轮询、运行记录抽屉 |
| `frontend/src/components/WorkflowRunsDrawer.vue` | 按 subject（interview-question + qid）看该题运行流水 |
| `frontend/src/api/interview.ts` | `interviewApi`：list/create/detail/update/remove、add/update/removeQuestion、generateAnswer |
| `frontend/src/router/index.ts` | `/office/interviews`（列表，name `Interviews`）与 `/office/interview/:id(\\d+)`（name `InterviewDetail`，`bare: true`） |

## 接口

所有接口在 `/api` 前缀下，需登录，按 owner 隔离；题目级写操作统一返回整条 InterviewDetail，
前端直接整换。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/interviews` | 场次列表（按 id 倒序），题目只给计数 |
| POST | `/office/interviews` | 建场次，可在 `questions` 里顺手带第一批问题 |
| GET | `/office/interviews/{record_id}` | 详情，含每题由最近运行翻译出的 ref_status/ref_error |
| PUT | `/office/interviews/{record_id}` | 只改元数据（`exclude_unset`，显式 null 清空字段；纯空格的公司/岗位跳过） |
| DELETE | `/office/interviews/{record_id}` | 删除；有题生成中返回 `code=-1` 业务拒绝 |
| POST | `/office/interviews/{record_id}/questions` | 加一题，服务端发 qid |
| PUT | `/office/interviews/{record_id}/questions/{qid}` | 改 question/my_answer/note，ref_answer 原样保留 |
| DELETE | `/office/interviews/{record_id}/questions/{qid}` | 删题；该题生成中业务拒绝 |
| POST | `/office/interviews/{record_id}/questions/{qid}/answer` | 先校验对话模型配置，再建运行并挂后台任务；重复触发生成返回 `code=-1` |

## 测试

- `backend/tests/test_interview.py`（仅纯函数层，164 行）：
  - `_new_qid` 唯一性；`_new_question` 只接收手填三件套并裁空白；
  - `_merge_ref_answer` 只改目标题的 ref_answer、qid 已删时 noop；
  - `_question_of` 把最近一次运行翻译成 none/analyzing/ready/error（参数化）、
    历史遗留行（有答案无运行）视为 ready、用户字段原样返回；
  - `_answer_inputs` 携带公司/岗位/题目、有轮次才加轮次行、有「我的回答」才包成标注段。
- 暂无 service/HTTP 层测试：场次与题目的 CRUD、owner 隔离（他人 id 404）、删除/重复触发
  闸门、BackgroundTasks 端到端回写目前没有自动化用例覆盖。
- 前端（列表、内联编辑、轮询停表、折叠状态）没有自动化测试。
