# 在线简历工作台（整页编辑 + A4 实时预览 + AI 帮写）

「简历」页第一个 tab「在线简历」里新建/编辑一份简历，工作台在新标签页打开
（路由 `/office/resume-builder/:id`，`bare` 整页，不带主布局侧边栏）。左栏模块导航、
中栏表单、右栏 A4 实时预览，改完手动保存；整份简历是 `resume_doc.doc` 里的一个 JSON
文档，与上传 PDF/Word 做分析的「简历列表」是两个域，只通过「保存到简历」单向衔接
（上传分析那条链路见 [resume.md](resume.md)，网盘备份见 [netdisk.md](netdisk.md)）。

## 用户可见行为

- 五个模块：个人信息、工作经历、项目经历、教育经历、专业技能；左栏可拖拽重排模块顺序，
  经历类模块显示条目数，悬停可直接「+ 添加一条」
- 三类经历是条目卡片（上移/下移/删除），字段含公司/学校等抬头、月份区间（YYYY-MM +
  「至今」勾选）与一段富文本描述；教育另有学历（高中/大专/本科/硕士/博士）与
  就读形式（全日制/非全日制）两个白名单下拉
- 富文本工具栏：撤销重做、粗斜下划线、三种对齐、有序/无序列表、缩进、链接、清除格式；
  粘贴内容先消毒，工具栏右侧内嵌该模块的 AI 按钮
- 右栏 A4 预览按 794×1123px 等比缩放，停顿 300ms 后重渲；超过一页时画红色虚线分页符
  （「第 N 页从此处开始」），分页线按各版式实测几何算，不是整页高的倍数
- 顶栏切换 3 套版式（经典 classic / 双栏名片 sidebar / 简约 minimal）与 7 种主题色
  （青碧/藏蓝/酒红/墨绿/黛紫/琥珀/炭灰）；版式与配色正交
- 保存：「保存」按钮或 `Ctrl/Cmd+S`，有未落库改动时标题旁亮「未保存」，关页弹
  beforeunload 提醒；保存回来用服务端归一化后的结果对齐
- 导出 PDF：实际是浏览器打印链路——把渲染 HTML 开成 blob 独立标签页并自动 `print()`，
  用户在打印面板选「另存为 PDF」；服务端不出文件。双栏版式跨页左栏底色不延续，导出前
  有量到分页线就弹警告建议换版式
- 「保存到简历」：把内容排成纯文本同步成简历库一行（首次新建、之后更新同一行），同步前
  若有未保存改动会先保存；空简历拒绝同步
- AI 三类动作：帮写（write）、润色（polish，优先取编辑器选中文字）、智能检查（check，
  仅技能模块入口，针对整份输出 markdown 评分与逐模块修改建议）；右侧抽屉流式出字，
  可停止/重试，结果可「插入光标处 / 追加到末尾 / 替换内容」
- 顶栏「AI 对话」与列表行的「AI 对话」：有未保存改动先落库，再新开对话页并带
  `?resume-doc=<id>`，默认选「求职顾问」，简历全文挂成待发送上下文卡片

## 关键取舍

### 整份 JSON 文档，不拆子表

条目量级是几条到十几条，没有跨文档检索条目的需求，拆子表只带来 join 与级联。
`resume_doc.doc` 一个 JSON 列装下全部模块与 `module_order`，前端整文档加载、整文档
保存，没有条目级接口。条目的 `cid/pid/eid` 只是文档内部寻址键（AI 帮写按它定位），
前端生成、保存时服务端校验形状 `^[A-Za-z0-9_-]{1,64}$`，不认识就补发一个。

### 保存是结构契约的唯一收口

`normalize_doc()` 对任意来源（旧版本、手改库）补齐成完整契约：模块顺序必须恰好是五个
键的一个排列，否则退回默认；版式/主题/学历/就读形式走白名单；月份只认 `YYYY-MM`；
富文本统一消毒；出库（`to_detail`）也过一遍，前端因此不需要兼容老数据。

### 富文本白名单消毒，前后端各做一次

contenteditable + `execCommand` 零新依赖，但粘贴和模型输出都不可信。白名单只放排版所需
标签（`p/br/div/span/strong/b/em/i/u/ul/ol/li/a/blockquote`），`script/style` 连内容
一起丢，链接只放行 http(s)/mailto/tel，`style/class` 不透传，唯一例外是块级 text-align
提取成受控 class（`rb-ta-center` 等），blockquote 只保留一层。服务端 `sanitize_html()`
落库前消毒，前端 `sanitizeRichHtml()` 在粘贴、AI 结果应用、预览/打印前再消毒，规则两边
同步维护。编辑器自身靠「输入产生的 innerHTML 原样 emit、同值回写不重设 DOM」保证光标
不跳，拼音组词期间不向上抛变更。

### AI 帮写走 SSE 直发，不走工作流/运行记录

用户就坐在编辑器前等出字，所以与对话流同一套 SSE 约定（token/done/error 事件、心跳、
断流感知），不经工作流引擎。每个 action 对应一个 `chat_visible=false` 的功能内置人设
（`resume-writer` / `resume-polisher` / `resume-checker`），模型仍取用户自己的默认对话
模型，未配置时流内回 BusinessError，前端按「去配置模型」引导。帮写/润色被要求只输出
`<p>/<ul>/<li>/<strong>` 的 HTML 片段，体检输出 markdown；prompt 里整份简历是「事实
边界」，只许基于已有信息、缺数字用「X」占位。

### 导出用浏览器打印，不在服务端养无头浏览器

版面本来就是前端渲染器算出来的，三套 CSS 里写好了 `@page { size: A4; margin: ... }`，
开独立标签页走 `tab.print()`，「另存为 PDF」即成品。不走「导出 PDF 再上传简历库」：
浏览器打印出的 PDF（如 Microsoft Print to PDF）常常没有文本层，无法再做分析，所以另做
「保存到简历」内部同步纯文本。

### 与简历分析、网盘的关系

同步在简历库建的是 `mime=text/plain`、`filename=<标题>.txt` 的纯文本行，没有原件，
因此 `netdisk_fs_id/netdisk_path` 恒为 NULL，不会同步到百度网盘（上传 PDF/Word 那条线
才会把原件同步网盘，见 netdisk.md）。两边弱关联无外键：`resume_id` 记录同步目标，简历
库那行被删后下次同步按缺失重建；再次同步若正文变化，旧正文的分析报告与改进意见一并
清空（`report/suggestions/analyzed_at` 置空）。

### bare 整页 + 新标签页，避开 keep-alive 多实例

工作台和面试详情、报告页一样从列表 `window.open` 新开标签页、路由 `meta.bare` 跳过主
布局，避免 App.vue keep-alive 按组件缓存造成的多实例问题。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/routers/resume_builder.py` | `/office/resume-docs` CRUD、`save-to-resume`、`/ai/assist` SSE |
| `backend/app/services/resume_builder.py` | 结构契约（`default_doc`/`normalize_doc`）、HTML 消毒、prompt 拼装、纯文本导出、流式模型调用 |
| `backend/app/models/entities.py` `ResumeDoc` | 表 `resume_doc`：`title`、`doc`(JSON)、`resume_id` 弱关联 |
| `backend/app/models/schemas.py` | `ResumeDocCreate/Update/Item/Detail`、`ResumeDocAiRequest` |
| `backend/app/services/agents.py` | 内置人设 `resume-writer`/`resume-polisher`/`resume-checker`（均 `chat_visible=false`） |
| `frontend/src/views/ResumeBuilder.vue` | 整页外壳：顶栏、三栏布局、载入/保存、同步、预览量测与分页线、快捷键/离开提醒 |
| `frontend/src/views/Resume.vue` | 「在线简历」tab：列表、新建（建完即开工作台）、重命名、删除、AI 对话入口 |
| `frontend/src/components/resume/ModuleNav.vue` | 模块导航、HTML5 拖拽重排、条目计数与添加入口 |
| `frontend/src/components/resume/ProfileForm.vue` | 个人信息字段网格 + 个人优势富文本 |
| `frontend/src/components/resume/WorkForm.vue` / `ProjectForm.vue` / `EducationForm.vue` | 三类经历条目表单与条目内 AI 入口 |
| `frontend/src/components/resume/SkillsForm.vue` | 技能富文本 + 帮写/润色/智能检查三入口 |
| `frontend/src/components/resume/EntryList.vue` | 经历条目卡片容器（头部/默认插槽、上移下移删除） |
| `frontend/src/components/resume/MonthRangeFields.vue` | 月份区间 + 「至今」 |
| `frontend/src/components/resume/RichTextEditor.vue` | contenteditable 富文本、工具栏、粘贴消毒、选区/插入 API |
| `frontend/src/components/resume/AiAssistPanel.vue` | 共用流式 AI 抽屉，三种应用方式 |
| `frontend/src/utils/resumeDoc.ts` | 前端结构契约（类型、白名单常量、`createEntry`/`createEmptyDoc`） |
| `frontend/src/utils/resumeTemplate.ts` | 三套版式渲染、`sanitizeRichHtml`、`pageBreakTops` 分页几何、打印 CSS |
| `frontend/src/utils/slidePrint.ts` `printSlideDeck()` | blob 开独立标签页并触发打印（与幻灯片导出共用） |
| `frontend/src/api/resumeBuilder.ts` | CRUD 信封请求 + `aiAssist` 走 `streamSse`（不经 axios） |
| `frontend/src/router/index.ts` | `/office/resume-builder/:id(\\d+)`，name `ResumeBuilder`，`bare: true` |

## 接口

所有接口在 `/api` 前缀下，需登录，全部按 owner 隔离（他人文档一律 404）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/resume-docs` | 列表，按 `updated_at desc, id desc`，不带 doc 大字段 |
| POST | `/office/resume-docs` | 新建，body 仅 `title`，服务端给默认骨架 |
| GET | `/office/resume-docs/{doc_id}` | 详情，出库也过归一化 |
| PUT | `/office/resume-docs/{doc_id}` | `title`/`doc` 均可单独更新；doc 入库前归一化+消毒 |
| DELETE | `/office/resume-docs/{doc_id}` | 删除 |
| POST | `/office/resume-docs/{doc_id}/save-to-resume` | 同步纯文本到简历库；空简历返回 `code=-1` 业务拒绝 |
| POST | `/office/resume-docs/ai/assist` | SSE：body `{doc_id?, module, action, item_id?, selection?}`，流 `token`/`done`/`error{code,message}` |

注：`/ai/assist` 注册在 `/{doc_id}` 之前不依赖顺序——路径参数声明为 int，字面段不会匹配。

## 测试

- `backend/tests/test_resume_builder.py`（service + 纯函数，约 40 个用例）：
  - CRUD 与 owner 隔离：默认骨架、空标题回退、按时间倒序、他人文档读作不存在、
    删除、空标题更新被忽略；
  - 契约：老数据缺键读时修复、缺字段补齐、非法 module_order/版式/主题回退默认、
    条目字段（月份正则、学历白名单、id 补发）校验、合法文档往返不变；
  - 消毒：script 连内容丢弃、事件处理器/style/class 不透传、危险协议链接变纯文本、
    漏闭合标签修复、text-align 转受控 class、blockquote 只留一层、非字符串得空串；
  - prompt 与导出：帮写含条目事实与全文上下文、未知 item_id 报错、润色需有原文或选区、
    check 拒绝空简历、`html_to_text`/`html_to_lines` 保列表边界、导出文本按 module_order
    且空文档为空；
  - 同步简历库：空文档拒绝、首次新建并回填 `resume_id`、二次更新同一行、仅标题变化保留
    报告、目标行被删后重建、他人的 resume_id 不认；
  - AI 流：未配置对话模型报 not-ready、他人文档 404、fake model 验证文本与结构化内容块
    转发、客户端断流即停。
- `backend/tests/test_resume_builder_api.py`：HTTP 层只覆盖 save-to-resume 三条
  （成功建库行、空文档业务拒绝、未知 id 404）。
- 前端（渲染/分页几何/消毒镜像规则、三栏交互、打印链路）没有自动化测试。
