# 幻灯片（AI 生成结构化页面 · 工作台编辑 · 放映）

智能办公里的幻灯片：给一份材料（PDF / Word / txt / md 文档、粘贴文案或一句内容说明），
AI 产出的是**整份幻灯片的结构化 JSON**（主题 + 一组页面），服务端再把这份结构确定性
渲染成一个自包含 HTML 文档——预览、放映、导出三个入口共用这同一份渲染结果。用户在
工作台里改的永远是结构（表单为主、整组 JSON 源码为辅），不直接改 HTML；改完保存，
预览与放映自然同步。

## 用户可见行为

- 「幻灯片」列表：未配置可用的默认对话模型时顶部黄色告警并禁用「新建」；表格显示
  页数 / 配图数 / 状态（生成中、已生成、失败，点状态标签打开这份 deck 的运行记录抽屉）。
- 新建弹窗一次性给全：主题（必填）、内容说明、材料文档（多文件，单文件 10 MB 内）、
  粘贴文案、生成要求（受众 / 页数 / 风格，可选）。提交后立即返回一行「生成中」，
  慢的模型调用跑在后台，列表每 2.5s 轮询；生成完成弹通知（带「打开工作台」按钮，
  轮询回调不是用户手势，不能直接 `window.open`），失败通知里带模型返回的原因。
- 工作台与放映都是 **bare 路由、新开浏览器标签页**（不带主布局外壳，但仍要登录）。
- 工作台三栏：左侧页面列表（加页 / 删页 / 上下移，顺序即放映顺序）+ 外观区；中间
  表单编辑器（10 种版式各有对应字段）与「源码」模式（整组页面 JSON，应用后仍走服务端
  校验）；右侧是服务端渲染的实时预览 iframe。顶部可上传配图（PNG/JPEG/GIF/WebP/BMP，
  单张 5 MB、每份最多 24 张）、重新生成、导出 HTML / PDF、保存、放映。
- 外观（5 个预设、16:9 / 4:3、自定义强调色 hex）**改动即时单独保存并刷新预览**，
  不牵连未保存的文字；正文改动要点「保存」才落库，关标签页时有未保存提醒。
- 放映页：满屏 iframe，键盘 `←→↑↓` / 空格 / PageUp/Down / Home/End 翻页、数字键跳页、
  滚轮、触摸滑动、点左右边缘；`F` 全屏、`S` 显示讲稿备注；URL hash 记住页码，刷新停在
  同一页。顶部工具条鼠标静止 2.5s 淡出，提供翻页、全屏、「改这一页」抽屉（只改标题 /
  要点 / 配图 / 讲稿，保存重绘后回到原页）、导出。
- 导出 HTML 下载的就是放映那份文档（图片改成绝对地址，双击即可离线放映）；导出 PDF
  走浏览器打印（文档自带 A4 横版 `@page`，一页一纸），不在服务端养无头浏览器。
- 重新生成读的是创建时的材料快照、会覆盖当前编辑（列表上有二次确认），生成中禁止
  重复触发、禁止删除。

## 关键取舍

### slides 是唯一事实源，html 是派生物

`slide_deck` 一行里 `slides`（结构化页面 JSON）是事实源，`html` 列由
`services/slides/html.py` 从它确定性渲染，任何写接口都不收 HTML（`SlideDeckSave`
刻意没有该字段）。每次写 slides / theme / assets 后重渲一次；**读的时候也现渲一遍并
回写**（`deck_html()`）：渲染器一改版，所有历史 deck 立刻拿到新渲染，纯字符串拼装、
上限 80 页，成本相对一次 HTTP 可忽略。预览 / 放映 / 导出因此永远逐字一致，前端不做
本地假渲染。还没生成或没有合法页面时，渲染器兜底一张只有标题的封面页，放映不白屏。

### 生成走内置工作流 + 功能内置人设，不是独立 prompt

生成不是散落在 service 里的一次模型调用：service 只留工作流 slug
`WORKFLOW_GENERATE = "slide-generation"`，人设是内置智能体 `slide-architect`
（`chat_visible=false` 的功能内置人设，不出现在对话选择器里），prompt 骨架与输入变量
声明在 `services/workflows/builtin.py`，界面上可改。`_generate_inputs()` 把标题、说明、
要求、截断到 `SLIDE_MAX_SOURCE_CHARS`（16000 字）的材料全文、可用配图 id 清单整理成
工作流输入——模型看不到图片内容，只能在清单范围内引用 `asset_id`。

流程那一步声明的产物是 markdown：模型实际可能回 `{"theme":…,"slides":…}` 对象，也
可能只回一个页面数组，还常裹 ```json 栅栏或前后寒暄。`_json_candidate()` 看**最先出现**
的开括号决定按对象还是数组截取（数组里也有花括号，对象优先会掐错），解析不出可解析
结构就判失败。后处理用 `sanitize_theme` / `sanitize_pages` 收敛，合法页面为零这类后处理
失败用 `fail_run` 把已成功的运行降级成失败。

### 进度只有 workflow_run 一份事实源

接口同步做的事只是：文档解析（PDF/DOCX 在线程里本地抽文本）、落 deck 行、建一条
`subject=("slide-deck", deck.id)` 的运行；真正的模型调用由 FastAPI `BackgroundTasks`
执行，后台任务体自己开新 `SessionLocal`（延迟导入，测试可替换）。deck 行不存状态，
列表的 `status` / `error_msg` 由 `subject_state()` 从最近一次运行翻译（analyzing /
ready / error）；没有运行又没有页面的历史行按 error 呈现。模型门槛
（`require_chat_model`，要有一份验证过的默认对话模型）在创建与重新生成最前面拦，避免
留下永远「生成中」、删不掉的行。

### 编辑只改结构：sid 寻址、整包保存、降级而不是报错

页面有服务端发放的 `sid`（`p` + 16 位 hex），页面级增删改不依赖会漂移的数组下标
（与面试题 qid 同约定）；新页 sid 为空时由 `sanitize_pages` 补发，保存响应回传规范化
后的整组页面，前端拿它对齐自己那份。10 种版式（cover / section / bullets / image /
quote / closing / table / steps / flow / arch）中结构没填全会被**降级**：image 引用了
不存在的图、表格只有表头、步骤为空、链路不足 2 格、分层不足 2 层都退回 bullets 页，
链路 / 分层格子里的文字会搬进要点，不在屏幕上留空壳。`drop_empty_content` 只在生成侧
开（模型交来的空页直接丢），工作台保存不开——用户把一页清空是编辑途中的常态。

### 放映文档是沙箱里的自包含 HTML

文本全部来自模型与用户，不可信：所有文本走 `html.escape`，版式 / 预设 / 比例 / 强调色 /
图片路径全部过白名单（accent 只收 `#rgb` / `#rrggbb`，其余回落预设色），文档里唯一的
脚本是渲染器内置的固定导航脚本、零插值。因此前端敢给 iframe 开
`sandbox="allow-scripts"`。父子页面只能互发固定类型的 postMessage（文档上报
`{__slide:'page'}`，父页面下发 `{__slide:'command', action: goto/next/prev}`）。全屏有
两个入口：文档内 `F` 键请求舞台自己全屏；父页面工具条请求的是外层容器——sandbox 文档
收不到父页面点击带来的用户激活，替它调 `requestFullscreen` 会被静默拒掉。

### 配图：本地磁盘 + 随机文件名 + 静态直连

图片落 `UPLOAD_MEDIA_DIR/slides/<deck_id>/<16 位随机 id>.<扩展名>`，由 main.py 挂成
`UPLOAD_MEDIA_URL`（默认 `/uploads`）下的 StaticFiles，`<img>` 直接访问、不带鉴权
（img 请求带不上 Authorization 头），靠随机 id 不可猜。三道防线：扩展名按内容魔数判定
（改名成 .png 的 HTML/SVG 一律不认，SVG 本身不支持）、用户文件名只存库展示不参与路径
拼接、删除时按库里 rel_path 再校验一遍必须是自己那种三段形状。删 deck 时 service 只
返回路径，磁盘清理在路由里 best-effort 完成（服务层不碰文件系统）。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `SlideDeck` | 表 `slide_deck`：title / description / requirement / source_text（MEDIUMTEXT 材料快照）/ assets / theme / slides（JSON）/ html（派生物） |
| `backend/app/models/schemas.py` | `SlideTheme`、`SlidePageIn`/`SlidePage`/`SlidePageSave`、`SlideTable`/`SlideStep`/`SlideFlowNode`/`SlideArchLayer`、`SlideAsset`、`SlideDeckItem`/`Detail`/`Save`/`Regenerate` |
| `backend/app/services/slides/service.py` | `SlideService(db, owner_id)`：owner 作用域 CRUD、配图登记、生成运行的创建与后台执行、模型输出解析、整包保存、序列化 |
| `backend/app/services/slides/html.py` | 唯一注入面：白名单清洗（`sanitize_theme`/`sanitize_pages` 等）、10 种版式渲染、字号降档预算、固定 CSS 与导航脚本、打印样式 |
| `backend/app/services/slides/store.py` | 图片魔数嗅探、随机文件名落盘、rel_path 安全校验、best-effort 删除与 URL 解析 |
| `backend/app/services/workflows/builtin.py` | 内置工作流 `slide-generation`（单步 compose，agent_slug=`slide-architect`，6 个输入变量） |
| `backend/app/routers/slides.py` | `/office/slides` 一组接口；材料解析、上传限额、BackgroundTasks 调度、磁盘清理、html/export 的 CSP 与 no-store 头 |
| `backend/app/main.py` | `UPLOAD_MEDIA_URL` 挂 StaticFiles；include slides router（前缀 `/api`） |
| `backend/app/bootstrap.py` | 退役列清理：`slide_deck` 的旧 `status` / `error_msg` 列由 `migrate_schema` 删除 |
| `frontend/src/views/Slides.vue` | 列表、新建弹窗（multipart 一次性提交）、2.5s 轮询、完成通知、运行记录抽屉入口 |
| `frontend/src/views/SlideWorkbench.vue` | bare 工作台：页面列表 / 表单与源码双模式 / 主题即时保存 / 配图管理 / srcdoc 实时预览 |
| `frontend/src/views/SlideShow.vue` | bare 放映页：srcdoc iframe、postMessage 传话、工具条淡出、外层全屏、就地改页抽屉、导出 |
| `frontend/src/api/slides.ts` | `slidesApi`：list / create（FormData）/ detail / save / remove / regenerate / html / exportHtml / uploadAsset / removeAsset |
| `frontend/src/utils/slidePages.ts` | 前端侧页面骨架与上下限（加页、切版式补骨架、各类 append/remove、normalizePage） |
| `frontend/src/utils/slidePrint.ts` | 导出 PDF：同一份 HTML 补 `<base>` 后开 blob 标签页触发打印 |
| `frontend/src/router/index.ts` | `/office/slides`、`/office/slides/:id(\\d+)/workbench`、`/office/slides/:id(\\d+)/show`（后两者 `meta.bare`） |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPLOAD_MEDIA_DIR` | 空（`<backend>/uploads`） | 配图落盘根目录，按代码位置而非 cwd 解析；Docker 必须挂卷 |
| `UPLOAD_MEDIA_URL` | `/uploads` | 静态访问前缀，URL 不带鉴权，可置于鉴权反代之后 |
| `SLIDE_MAX_IMAGE_BYTES` | 5 MB | 单张配图大小上限 |
| `SLIDE_MAX_IMAGES_PER_DECK` | 24 | 每份幻灯片配图张数上限 |
| `SLIDE_MAX_SOURCE_CHARS` | 16000 | 喂模型的材料截断上限（幻灯片只要骨架，比简历短） |
| `KB_MAX_UPLOAD_BYTES` | 10 MB | 新建时单个材料文档的上传上限（路由另有 200000 字的入库总量闸门） |

## 接口

所有接口挂在 `/api` 前缀下，均需登录，且全部经过 owner 作用域（他人的 deck 读作 404）。
业务拒绝（材料为空、生成中、图片超限等）返回信封 `code=-1` + 中文 message，不是 HTTP 错误。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/office/slides` | 列表（Item，不含 slides / source_text / html） |
| POST | `/api/office/slides` | multipart 新建：`title` / `description` / `requirement` / `text` + 多 `files`；同步落库建运行，后台开跑 |
| GET | `/api/office/slides/{id}` | 工作台完整数据（Detail：theme / slides / assets，仍不含 html） |
| PUT | `/api/office/slides/{id}` | 整包保存，字段全可选（`None` 保持原样）；返回规范化后的 Detail（sid 以响应为准） |
| DELETE | `/api/office/slides/{id}` | 删除；生成中拒绝，成功后 best-effort 清理配图文件与目录 |
| POST | `/api/office/slides/{id}/generate` | 按材料快照重新生成，body 可选 `requirement`（覆盖当次要求）；生成中拒绝 |
| GET | `/api/office/slides/{id}/html` | 自包含 HTML 文档（读取即重渲），`Cache-Control: no-store` + CSP，供 iframe srcdoc |
| GET | `/api/office/slides/{id}/export` | 同一份 HTML 下载（`Content-Disposition: attachment`，图片 `/uploads/...` 改写为绝对地址） |
| POST | `/api/office/slides/{id}/assets` | 上传一张配图（multipart `file`，魔数判定），返回更新后的 Detail；落库失败回删孤儿文件 |
| DELETE | `/api/office/slides/{id}/assets/{asset_id}` | 删配图并解掉所有引用页（image 页由渲染器降级为 bullets） |

前端路由：`/office/slides`（列表，主布局内）、`/office/slides/:id/workbench` 与
`/office/slides/:id/show` 为 bare 整页（App.vue 据此跳过主布局外壳，未登录仍被路由守卫
拦下）。配图文件经 `GET /uploads/slides/<deck_id>/<id>.<ext>` 静态直读。

## 升级行为

- 进度状态曾在 `slide_deck` 行上有 `status` / `error_msg` 两列；状态只在 `workflow_run`
  上之后，这两列成为孤儿，且 `create_all` 建的是无 DB 默认值的 NOT NULL 列，新插入会
  报错，`migrate_schema()` 启动时幂等删除（与简历三表同一处理，见
  `RETIRED_STATUS_COLUMNS`）。新库由 `create_all` 直接建成新结构，`docs/schema.sql`
  同步当前 DDL。
- 升级前的存量 deck 没有运行行：列表翻译状态时 `idle` 按 error 呈现（没有运行又没有
  页面只可能是被旧逻辑遗留的半成品），点状态标签可看到运行记录。

## 测试

`backend/tests/test_slides.py`（83 个用例，分五组）：

- 图片存储（约 12）：五种真魔数通过、改名 HTML / 非 WebP 的 RIFF 被拒、落盘用随机名
  与真实扩展名、超 5MB 拒绝、`is_safe_rel_path` 只收 `slides/<数字>/<16hex>.<合法后缀>`
  （`..` / 绝对路径 / 不认识文件全拒）、删除忽略非法路径与缺失文件、删目录只清自己
  命名规则的文件。
- 渲染器注入面与白名单（约 12）：所有文本位 HTML 转义、theme 塞 CSS 被丢弃、合法
  accent 与比例生效、主题块可按 `;` 解析、省略 `#` 的 hex 归一、危险 rel_path 的图片
  不渲染而安全路径保留。
- 版面与降级（约 35）：内容页标题钉上沿正文居中、schema 的版式集合与渲染器严格一致、
  要点按折行估算三档降档且档位数字与 CSS 常量互相对拍、降档按 16:9 / 4:3 画幅、
  标题换行与结论条挤占预算、配图页限高、A4 横版打印尺寸、全屏舞台补主题底色、
  表格补齐短行 / 步骤编号 / 空结构降级 bullets、生成侧可丢弃空页、链路与分层的箭头
  条件 / 转义 / 不足两格降级且保留文字 / 末格 via 清空 / 层只留有名或有块的、链路与
  分层档位数字与 CSS 对拍、各版式内容顶满上限不溢出、未知版式与空 deck 兜底封面、
  sid 合法沿用其余重发、页数（80）与字段长度上限。
- 模型输出解析（约 6）：栅栏与寒暄截取、对象 / 数组两种形状、垃圾输出报错、
  `_generate_inputs` 的条件段落与配图清单、材料超长截断标注、超时错误文案。
- 服务层（SQLite 内存库，约 18）：无材料 / 无标题拒绝、创建即 analyzing 且有兜底封面
  HTML、owner 隔离、新字段经 Detail 序列化存活、新建默认浅色 amber 主题、保存发 sid
  并重渲染、无 sid 页面可保存、只存 theme 不动 slides、空标题拒绝、HTML 不能借字段
  落库、analyzing 中禁删、删除返回图片路径、配图增删改 HTML 并解除页面引用、配图
  24 张上限、未知配图 404、无默认对话模型被门槛拦下、禁止并发生成、读取时回填缺失
  html 并重渲陈旧 html、`to_detail` 输出的是清洗后的形状。
