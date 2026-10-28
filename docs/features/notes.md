# 智能办公 · 求职笔记

「简历」页的「笔记」tab，一个只属于本人的 markdown 记录本：JD 片段、谈薪策略、公司
调研、待办提醒之类不适合塞进简历或面试记录的散点信息。与面试记录同挂在求职办公域，
但和简历/面试记录/报告没有任何数据关联，没有外键或引用。

## 用户可见行为

- 列表：表格按更新时间倒序（同秒时新建者在前），每行标题 + 正文两行摘要（折叠空白后
  截取 140 字）+ 更新时间；行内「编辑」「删除」（删除二次确认）
- 顶部「搜索标题或正文」：回车/点搜索按钮后走后端关键词搜索（标题或正文包含即命中，
  大小写不敏感）；输入框清空叉立即回到全量列表；空态文案区分「还没有笔记」与
  「没有匹配的笔记」
- 「新建笔记」打开右侧抽屉（移动端全屏）：标题（必填，200 字内）+ 正文 textarea
  （10 万字内），「编辑 / 预览」切换：预览用全站共用的 markdown-it 基座渲染
  （`html:false`，外链新标签页打开），正文是 markdown 纯文本
- 编辑既有笔记时先拉详情取完整正文（列表行只有摘要）；保存后关闭抽屉并刷新列表。
  正文留空保存即清空为无正文

## 关键取舍

### 单表 CRUD，不做标签/分本子

个人求职期的笔记量级是几十到几百条，标题 + 正文关键词搜索已经覆盖找回场景，标签、
文件夹一类结构属于提前建复杂度。表只有 `title` / `content` 两个业务列，后续要加维度
是纯增量。

### 列表不带正文，摘要读侧现算

列表行只给标题、摘要与时间，正文走 `GET /notes/{id}`：避免一次请求把全部长文拖过网络。
摘要（连续空白折叠成空格、截 140 字加省略号）在序列化时由正文算出、不落库——正文改了
摘要自然跟着变，没有两份事实要同步。

### 关键词是单个 LIKE，通配符按字面量处理

搜索词用 `ilike %kw%` 同时匹配标题与正文（MySQL 上编译成 `LOWER(col) LIKE LOWER(:p)`，
SQLite 同样可用）。用户输入的 `%` / `_` / `/` 先按 `ESCAPE '/'` 转义，搜 `100%` 只命中
真的包含字面量 `100%` 的笔记，不会变成匹配所有行。

### 纯空格标题在 schema 层拒绝

`min_length=1` 只数字符数、挡不住纯空格，所以 `NoteCreate/NoteUpdate` 共用一个
`field_validator`：strip 后为空直接 422，不把 NOT NULL 列写进空串这种灰色状态。
正文则允许显式传 `null` 清空（`exclude_unset` 的部分更新语义，与面试记录一致）。

### owner 隔离在 service 层收口

所有查询经 `NoteService._scope()` 加 `owner_id` 条件，handler 拿不到裸 session；
他人的笔记 id 一律读作 404，搜索同样只在本人空间内匹配。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/routers/note.py` | `/office/notes` CRUD 五个端点，`keyword` 查询参数搜索 |
| `backend/app/services/note.py` | `NoteService`（作用域隔离、CRUD、LIKE 转义、摘要计算与序列化） |
| `backend/app/models/entities.py` `Note` | 表 `job_note`：`title` VARCHAR(200) + `content` MEDIUMTEXT，新表由 `create_all` 自动建 |
| `backend/app/models/schemas.py` | `NoteCreate/NoteUpdate/NoteItem/NoteDetail` 与标题非空校验器 |
| `frontend/src/components/NotesPanel.vue` | 列表 tab：搜索、新建/编辑抽屉（编辑/预览切换）、删除 |
| `frontend/src/api/notes.ts` | `noteApi`：list/create/detail/update/remove |

## 接口

所有接口在 `/api` 前缀下，需登录，按 owner 隔离。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/notes?keyword=` | 笔记列表（更新时间倒序），只给摘要；keyword 非空时按标题/正文模糊匹配 |
| POST | `/office/notes` | 建笔记，返回详情；标题 strip 后为空走 422 |
| GET | `/office/notes/{id}` | 详情，含完整正文；他人/不存在为 404 |
| PUT | `/office/notes/{id}` | 部分更新（`exclude_unset`），显式 `content: null` 清空正文 |
| DELETE | `/office/notes/{id}` | 删除，他人/不存在为 404 |

## 测试

- `backend/tests/test_note_api.py`（service + HTTP 两层，16 个用例）：
  - service：标题/正文 strip、空白正文落 None、更新时间 + id 双键倒序、摘要折叠与截断、
    标题/正文大小写不敏感匹配、`%`/`_` LIKE 通配符按字面量转义、部分更新与 null 清空、
    owner 隔离（他人读不到也搜不到）；
  - HTTP：空列表信封、建后列表只带摘要不带正文、详情/更新、keyword 过滤、他人 id 404、
    纯空格标题 422、删除后再取 404。
- 前端无自动化测试（与 InterviewRecords 等列表组件一致）。
