# 工作流编排：定义、并行组与结构图

「AI 构建 → 工作流」把简历分析、幻灯片生成、面试参考答案这类「功能里硬编码的一次
模型调用」搬成页面上可改的流水线：一条工作流 = 一组声明的输入变量 + 若干步骤，每步
绑定一个智能体人设、一段 `{{变量}}` 模板和一种输出形态；步骤按**并行组**组织，组间
顺序执行、组内并发。编辑器是「基本信息 + 每组一页」的向导，结构图以只读预览弹窗
呈现，所有「跑起来必然失败」的写法在保存时由后端一次拦下。内置流程随代码 seed、
只读、可复制；功能侧（简历、幻灯片、面试、求职助手）按 slug 调内置流程。运行/SSE/
单步重跑/历史清理见 `workflow-runs.md`，双运行对比与 Rubric 见 `workflow-eval.md`，
步骤绑定智能体版本见 `agent-versions.md`，步骤内部执行 trace 见 `unified-trace.md`。

## 用户可见行为

- 列表页一行一条流程：名称旁挂「内置」/「已停用」标签与 slug；「流水线」列用
  `keyA ∥ keyB → keyC` 一行说清组间顺序（→）与组内并行（∥），「规模」列报
  「N 步 · M 组 · K 输入」，另有最近一次运行的状态/时间/耗时。整行点击进编辑器，
  内置流程进去是只读；行内留「运行」「复制」，自建流程才有「删除」。
- 新建是一个向导：第 1 页基本信息（名称、标识、说明、输入变量、全流程 MCP 挂载），
  之后**一个并行组一页**，步骤条可点跳页（离开当前页要先过本页必填）。新建页顶部有
  「起步于」下拉，选一条内置流程把输入与步骤整份带进来，名字置成「XXX 副本」，
  **标识留空必须自己起**。
- 并行关系只靠两个按钮改：组页「并行步骤」往本组插一个节点（留在本页）；最后一组点
  「下一步」自动开一个新组并翻到新页。卡片头部的上下箭头调先后：跨组移动时该步自动
  并入相邻组。要把两组合一，在「高级」里把分组名改成同名——相邻同名即同组。
- 每步卡片：步骤标识、人设选择（下拉带人设描述，可展开 system prompt）、Prompt
  模板、产出变量常驻；分组名、markdown/json 切换、JSON 期望字段、按步模型覆盖、
  节点级 MCP 挂载、「可选步骤」收在「高级」折叠区（带这些值的步骤回填时自动展开）。
- 模板框敲 `{{` 就地弹补全（上下键选、回车/Tab 补、Esc 收），候选只有这一步真能
  引用的：声明的输入 + **前面并行组**的产物；JSON 步额外列 `{{产物.字段}}`，字段
  来自它声明的期望字段。卡片上常驻「上一步产出」芯片行（仅紧邻的前一组），完整集合
  在「＋插入引用」弹层里按来源分组，点芯片即插入。
- 任一页都可点「预览」打开结构图：自上而下一行一个并行组、行内几步平排，输入变量
  画在最上方；节点上显示编号、标识、人设、模板摘要、引用/产出芯片与 JSON/可选标记。
  节点间的线不用手连，是从每步模板的 `{{根变量}}` 反推出来的，只连「前面组的产物
  → 这一步」；没有引用线的组间间隙补一个 ↓ 表示先后。悬停节点高亮相关连线并显示
  变量名，悬停浮层看全该步详情；点节点关闭弹窗、跳到该步所属组的配置页。
- 列表页与结构图都不展示 `g1/g2` 这种内部组名：组展示名由组内各步标识拼成
  （`大纲、配图`），运行现场例外，原样显示落库的 group_key。
- 必填校验在前端先拦一遍：名称、新建时的标识、每个输入变量名、每步的标识/分组/
  人设/非空模板是缺口（`problemIds`），翻页离开当前组先亮红框拦住，点「完成」按
  向导顺序跳到第一个问题页；开始提交后红框随填随灭。前端只查「填没填」，引用闭合
  与归属问题一律以后端保存校验为准。
- 保存是整份替换：客户端不提交 `sort_order`，后端按数组顺序重新编号。引用同组或
  后面步骤的产物、引用未声明变量、对 markdown 产物点字段、组名同名但被隔开、标识/
  变量名非法或重复、人设不可见、模型/MCP 不属于自己——全部在保存时拒绝，原因原样
  弹回，表单内容不丢。
- 内置流程的查看页是同一套向导的只读态：所有输入禁用、底部提示「需要改动请先在
  列表页复制」，但结构图预览、缩放、人设查看照常用。输入变量的显示名留空时以后端
  兜底成变量名，运行报错与运行记录里不会出现没有名字的变量。
- 内置流程（简历分析、简历对比、幻灯片生成、面试参考答案、求职助手六条等共 10 条）
  随代码升级：prompt 与步骤结构改进重启即生效，停用状态不被覆盖；内置流程不可直接
  改，「复制」得到 `slug-copy`（撞名追加 2、3……）的自建副本后随便改。功能侧按
  slug 调内置流程，用户在界面停用它不影响功能本身。

## 关键取舍

### 为什么只有「相邻同名」算一组

分组没有单独的实体，只有每步行上的 `group_key` 字符串。组的唯一口径是**相邻且
group_key 相同**：后端 `template._group_order`、执行器、编辑器 `groupSteps`、
列表页 `chainOf` 是同一套切法，「画出来几行就是跑起来几段」。允许 `g1,g2,g1`
按名字归并成两组，编辑器里的顺序就会和真实执行顺序脱节，所以 `_normalize` 直接
拒绝「并行分组 X 的步骤不连续」。前端据此把「并组」做成显式动作：高级里改名、
移动按钮并入相邻组，而不是让用户猜命名规则。

### 为什么线是反推的、不让手连

引用关系的唯一事实源是模板文本：`{{name}}` 能不能取到值，保存时后端
`_check_template_vars` 已按「声明输入 + 前面组产物」判过。手连一条线就会多出一份
必须与模板保持一致的数据，连错时图上有线、运行时取不到值。`stepLinks` 只解析模板
里的根变量，且只连后端认的那类引用（生产者在严格更早的组、且 output_var 同名）；
输入变量每个步骤都可能用，全连是一片糊，只在输入行与芯片里呈现。

### 为什么整份替换、不存 sort_order

步骤之间是相互约束的（只能引用前面组的产物、同组不能互引、分组必须连续），逐行
补丁判不出「改完这一行整份还合不合法」；而编辑器本来就是一次提交整份。所以
`PUT /workflows/{id}` 先删全部步骤行再按数组重插，`sort_order = 数组下标 × 10`，
客户端 schema（`WorkflowStepInput`、`WorkflowPayload`）里压根没有 sort_order。
先删后插也避开了 `(workflow_id, key)` 唯一索引与改名冲突。删除/重插只动定义表，
历史运行行自带头 prompt 快照与冗余 slug，定义改了甚至删了，旧运行仍看得懂。

### 为什么保存校验宁可严苛

挡下来的全是「跑起来必然失败」：未声明变量、同组兄弟产物（并行执行时那步还没有
输出）、对整段文本点字段、看不见的人设、不属于自己的模型配置。保存报错只花一次
请求；放过去就是一次运行的时间和 token，外加上运行历史里一行难定位原因的失败。
MCP 挂载是例外：保存只查「存在且属于本人」，停用状态放行（允许先挂后启用），
停用项到运行时再挡。

### 为什么芯片集合必须和后端校验同一份

能点进模板的引用与保存时被认可的引用，是同一条规则在两端各跑一遍：
`chipGroupsFor` 按「声明输入 + 严格更早的并行组产物」列候选，JSON 步的
`{{产物.字段}}` 候选只来自它声明的期望字段，与 `_check_template_vars` 的判定逐项
对应。候选里出现一个存不下的引用，比没有候选更糟——用户照着补全写、保存才被拒。
「上一步产出」常驻行刻意只回退紧邻的一组：手边常接的就是这一级，更早的产物仍在
「插入引用」弹层里完整列着；第一步前面没有组，不显示这行。

### 为什么内置只读、复制才是定制入口

内置行只有 `owner_id = 0` 一条，seed 每次启动按代码目录就地刷新（步骤按 key
对齐覆盖），直接改内置会在下一次部署被抹掉。因此 `_owned` 对内置统一抛
PermissionError，定制路径只有复制；副本是普通自建行，slug 自动派生、功能侧也
不会按副本的 slug 去调它——功能永远认内置那一条。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/workflows/__init__.py` | 词表：`RUN_*`/`STEP_*` 状态、`SUBJECT_*` 业务对象 kinds，及包内模块分工说明 |
| `backend/app/services/workflows/defs.py` | `WorkflowService`：定义 CRUD/复制、可见性（内置 owner_id=0 + 本人）、`_normalize` 整份校验与重编号、`_check_template_vars` 引用校验、`require_by_slug` |
| `backend/app/services/workflows/template.py` | `render_prompt` 模板渲染（点号取值、数组下标、取不到即报错）、`_group_order` 相邻同名分组、`_PLACEHOLDER` 正则 |
| `backend/app/services/workflows/builtin.py` | `BUILTIN_WORKFLOWS` 目录（dataclass 声明）、`seed_builtin_workflows` 幂等刷新、`start_builtin`/`finish_builtin_step`/`builtin_output` 功能侧入口 |
| `backend/app/services/workflows/exec.py` | 执行器：`_group_order` 切组、组间顺序、组内 `asyncio.gather`（运行语义详见 `workflow-runs.md`） |
| `backend/app/routers/workflows.py` | 定义端点：`GET /workflows`、`GET/POST/PUT/DELETE /workflows/{id}`、`POST /workflows/{id}/duplicate` |
| `backend/app/models/entities.py` | `Workflow`（`uk_workflow_owner_slug (owner_id, slug)`）、`WorkflowStep`（`uk_workflow_step_key (workflow_id, key)`） |
| `backend/app/models/schemas.py` | `WorkflowDefinition`/`WorkflowCreate`(+slug)/`WorkflowUpdate`(+enabled)、`WorkflowStepInput`（无 sort_order）、`WorkflowItem`/`WorkflowStepItem`/`WorkflowInputField` |
| `backend/app/bootstrap.py` | 启动 `seed_workflows()` 调 `seed_builtin_workflows`；`workflow`/`workflow_step` 挂载列等走幂等 ALTER |
| `frontend/src/views/Workflows.vue` | 列表页：`chainOf` 相邻同名分组渲染步骤链、规模/最近运行、复制/删除/清理入口 |
| `frontend/src/views/WorkflowEditor.vue` | 向导弹窗壳：基本信息页 + 每组一页、起步预设、加组/加并行/调序、必填缺口 `problemIds`、整份提交、结构图预览弹窗 |
| `frontend/src/components/workflow/WorkflowStepForm.vue` | 步骤卡片：标识/人设/模板/产出常驻，高级区收纳分组名与契约；`{{` 补全、芯片插入、选区保持 |
| `frontend/src/components/workflow/WorkflowCanvas.vue` | 只读结构图：elkjs layered（DOWN）布局、引用线/顺序边/↓ 箭头、悬停高亮、缩放/折叠/全屏、`locate` 事件与 `active` 光环 |
| `frontend/src/components/workflow/workflowLayout.ts` | 前端单一事实源：`groupSteps`/`groupSequence`/`groupLabel`、`referencedVars`、`stepLinks`、`chipGroupsFor`/`chipsFor`/`upstreamGroupsFor`、`uniqueGroupName`、`toStepForm` |
| `frontend/src/components/workflow/runFields.ts` / `runLinks.ts` / `StepOverrideModal.vue` | 运行现场的上下文还原、业务对象深链、重跑覆盖弹框（属 `workflow-runs.md`） |
| `frontend/src/stores/workflowEditor.ts` | `requestEditor(id, readonly)` 跨页传「打开谁」，编辑器按 nonce 灌表单 |
| `frontend/src/api/workflows.ts` | 接口类型；`WorkflowPayload.steps = Omit<WorkflowStep, 'sort_order'|'agent_name'>[]` |

### 定义契约与后端校验

- 标识字符集：步骤 key、group_key、产出变量、输入变量统一用
  `^[A-Za-z_][A-Za-z0-9_]*$`（不含点号与短横线，保证模板里永远引用得到）；
  工作流 slug 是另一套：`^[a-z0-9][a-z0-9-]*$`，2–64 字符，仅创建时可填、
  创建后禁用输入，重复 slug 报「标识已存在」。
- `_normalize` 按数组顺序产出步骤行（`sort_order=index*10`，空产出变量归 NULL），
  依次查：输入名合法且不重复 → MCP 归属 → 每步 key/group 名合法、key 不重复 →
  输出形态仅 markdown/json、非 json 不得带期望字段 → 产出变量名合法且不重复 →
  人设 slug 可见 → 按步模型配置归属（BusinessError 4004 照转）→ 节点级 MCP 归属
  → 分组连续性 → 模板引用。
- `_check_template_vars` 按组推进：known 集合初始为声明输入；同组产物先放进
  in_group 仅用于给出「引用了同组步骤 X 的产物」这条专门报错，组处理完才并入
  known；`{{a.b.c}}` 的根必须是某个 **json 步**的 output_var，每段路径须匹配
  `^[A-Za-z_][A-Za-z0-9_]*$` 或纯数字下标，否则分别报「整段文本没有字段可取」
  「字段名不合法」。
- 运行时 `render_prompt` 是同一套形状的兜底：占位正则
  `\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}`，取不到值抛
  「模板取值失败」（对象/数组按 JSON 序列化，null 补空串），不静默替空。
- 接口错误形状：`ValueError`/`PermissionError` 回 `APIResponse(code=-1, message=...)`
  信封（前端拦截器直接弹 message）；`LookupError` 回 404；查别人的流程也是 404
  而不是 403。

### 结构图布局

- 布局用 elkjs（`elkjs/lib/elk.bundled.js`，一兆多，弹窗真打开时才动态 import），
  `layered` + `DOWN` + 正交走线：一组一行、行内保持步骤原序
  （`forceNodeModelOrder`）、`NETWORK_SIMPLEX` 排横向位置。卡片宽固定 260px，
  先从 DOM 量自然高度喂给 ELK，坐标与走线回写绝对定位；布局发版本号，迟到的旧
  结果直接丢弃。
- 相邻两组之间一条引用都没有时补一条不可见的顺序边，否则 layered 会把两组并到
  同一行，把「先后」画成「并行」；顺序边不进 link 表，回来自然不画，界面改在组间
  间隙画 ↓。
- 交互：悬停节点时相关线 edge-hot、其余 edge-dim，变量名只在高亮线上显示；
  `active` 属性给节点描光环并 `revealActive` 把它滚进视口，点节点 emit
  `locate(下标)`，编辑器接到后关弹窗、跳到该步所属组页。组件保留了「外部指定
  active 节点 → 图上光环 + 自动滚入」的跟随能力，但当前向导版以预览弹窗使用它，
  不传 active（弹窗里没有「正在编辑哪一步」的概念）。缩放 0.3–2（手动档）与
  「适应宽度」（不放大超 100%），折叠偏好存 localStorage；ResizeObserver 跟随
  容器与内容尺寸变化重算。弹窗宽 1280px、窄屏留 16px 安全边并可全屏；代码中没有
  按屏宽隐藏结构图的开关，窄屏仍可开预览（早期左右分栏版「≤768px 不画图」的口径
  随向导改版已不存在，列表/向导本身就是窄屏兜底）。

## 升级行为

- 内置流程 seed 幂等：不存在则插入（`enabled=True`）；存在则按 slug 对账，
  刷新 name/description/input_schema/sort_order，步骤按 key 对齐——改了的就地
  覆盖（行 id 不变）、新增的插入、代码里删掉的步骤删除；**`enabled` 不动**，
  用户把内置流程藏起来扛得过部署。
- seed 只管 `owner_id=0` 那一行；用户复制出的同名（`xxx-copy`）流程不在对账范围，
  prompt 更新不会动用户副本。
- 功能侧调内置走 `require_by_slug`：停用照样可用（那是功能自己的流水线），查不到
  视为初始化损坏，抛 BusinessError（`CODE_CHAT_NOT_READY`，提示重启重新初始化）。
- 定义侧列（如流程级/节点级 `mcp_server_ids`）由 `bootstrap.migrate_schema`
  幂等 ALTER 补出，SQLite 测试库 `create_all` 直接建新列；老定义无需数据迁移，
  保存整份替换后自动落成新形状。整份重建步骤不触碰 `workflow_run_step`，历史运行
  的 prompt 快照、group_key、slug 冗余均自足。

## 测试

定义、模板与 seed 的测试集中在 `backend/tests/test_workflows.py`：

- 模板与分组：`render_prompt` 基础替换、JSON 产物整体 JSON 序列化、点号/嵌套/
  数组下标、null 补空串、字段不存在与未定义变量报错；`test_group_order_splits_repeated_group_name`
  验证 g1,g2,g1 切成三组。
- 定义校验（create/update 路径，约 20 个）：步骤按数组编号落库；slug 重复；
  未声明模板变量；引用同组并行兄弟被拒；**同名组被隔开（不连续）被拒**；引用
  前组产物放行；对 markdown 产物点字段被拒、对 json 产物放行；畸形字段路径
  （含同组互引组合）被拒；未知智能体与非法名；输出契约不符（非 json 带字段）；
  按步模型配置归属；流程级与节点级 MCP 的落库、外来 id 拒绝、停用挂载保存放行、
  复制保留挂载；整份替换步骤；更新定义不影响旧运行现场；内置拒编；复制可改；
  删除定义保留运行记录。
- seed：`test_seed_creates_builtin_workflows_and_steps`（且内置模板变量必在声明
  输入内）、幂等且按 key 就地刷新、停用选择保留、代码删步骤随之删除；
  `require_by_slug` 忽略停用、缺失报初始化错误。
- 接口层 `backend/tests/test_workflows_api.py`：列表带内置与各自最近运行、
  单取与 404、`/runs` 不被 `/{workflow_id}` 抢匹配、自建流程增改删闭环、
  校验失败回信封错误、内置只读但可复制。
- 前端目前没有 workflowLayout 的单测；分组/引用/芯片逻辑集中在
  `workflowLayout.ts` 纯函数，后端同名行为由上述测试覆盖。
