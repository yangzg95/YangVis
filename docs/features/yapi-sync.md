# YApi 同步（本地 Java 接口扫描）

开发工具里的「YApi 同步」页：扫描**后端所在机器上**的一个本地 Java 目录，认出 Spring
Controller 暴露的 HTTP 接口，按「一个 Controller 一个分类」写进指定 YApi 项目。扫描只读
源码、不需要工程能编译；同步只新增/更新，不删除 YApi 上已有接口。

## 用户可见行为

- 表单四项：YApi 地址、项目 ID、项目 Token（YApi 项目设置里的 token）、本地源码路径；
  Token 只随本次请求发送、不落库，页面上也明示了这一点
- 「选择目录」在**运行后端的机器**上弹出系统目录框（tkinter），也可以直接粘贴路径；
  填到包的上级目录即可，任意层级的包都能扫到，`target`、`build`、`src/test` 等目录自动跳过
- 扫描后展示 Java 文件 / Controller / 接口 / 分类四个计数，接口按分类折叠，
  点行打开预览抽屉：入参（query/path/header/form）、JSON 请求体、出参 JSON Schema、源码位置
- 不勾选任何行 = 同步全部；勾选后只同步勾选项（键为 `METHOD path`）。同步前有确认弹窗
- 同步结果逐行标 `新增` / `更新` / `失败`（失败悬停看 YApi 原话），顶部汇总
  新增、更新、失败、新建分类数与耗时；有警告时黄色提示条列出
- 扫描与同步都是同步长请求，前端单独放宽超时：选目录 330s、扫描 180s、同步 600s

## 关键取舍

### 为什么本地扫描而不是接代码仓库

- 使用者是在自己工位上把**本机当前工作副本**灌进 YApi，扫的是手头这份代码（可能含未提交
  改动），不是某个分支的远端快照；接 Git 反而和「我现在本地长什么样」对不上
- 浏览器拿不到本机绝对路径，所以选目录由后端代劳：弹的是后端机器的对话框。本机起服务时
  体验就是选自己的盘；部署到服务器时语义是「服务器上的目录」，无桌面环境（容器等）弹框会
  失败，提示改为手填路径
- 解析器是自研的轻量词法/语法分析（`java_parser.py`），不引入 `javalang`：不把 JDK 17 的
  record、sealed、注解数组参数押在一个停更的库上。只解析与接口文档有关的语法，不追求完整 AST

### 判重口径

- 扫描结果内部按 `(HTTP 方法, path)` 去重，同方法同路径只保留先扫到的那个，另一条进警告
- 与 YApi 之间也用同一口径：同步前先 `list_menu` 拉项目全量分类与接口，建
  `(METHOD, path)` 索引。命中即更新、未命中即新增，底层都是 YApi 的 `interface/save`
  （upsert 语义）；YApi 里有而本地没有的接口**不删**
- 分类按 `(包名, Controller 类)` 建，一个 Controller 一个分类。分类名优先 Swagger
  `@Tag(name)`，其次 Swagger 2 的 `@Api(tags/value)`，再退化到去掉 `Controller`/`Api`
  后缀的类名；YApi 分类平铺、同名互相覆盖，所以重名时追加 `（完整包名.类名）` 并告警。
  分类描述里写 Java 包名与 Controller 类名

### path 白名单为何必要

YApi 的 `save` 对 path 有字符白名单，中文、空格、`%` 等会让整条保存直接报错。同步侧用
正则 `^/[a-zA-Z0-9\-/_:!\.\{\}=]*$` 先在本地判，不合法的接口直接标失败、根本不发请求，
错误信息明确指向 path，省得把 YApi 的含混报错抛给用户。扫描侧会先做路径归一化：截掉
`?`/`#` 之后部分、反斜杠转正斜杠、合并重复斜杠、`{id:[0-9]+}` 这类带正则的变量简化成
`{id}`、补齐前导 `/`、去掉结尾 `/`。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/yapi/java_parser.py` | 自研 tokenizer + 解析器：包/import/类/字段/方法/注解/Javadoc，record 组件与枚举值 |
| `backend/app/services/yapi/scanner.py` | `JavaSourceScanner`、`TypeIndex`（按 FQN/import/同包/通配符 import 解析类型）、`SchemaBuilder`（泛型绑定、继承字段、枚举/包装类展开）、分类归并、警告收集 |
| `backend/app/services/yapi/client.py` | `YapiClient`（list_menu / add_cat / save 三个接口）、`normalize_base_url`、`errcode` 业务错误判定、httpx 日志 token 脱敏过滤器 |
| `backend/app/services/yapi/sync.py` | `sync_to_yapi` 编排：重扫→按勾选过滤→分类映射→判重→白名单→逐条 save，产出逐行结果 |
| `backend/app/routers/devtools.py` | 三个 HTTP 路由，登录态注入，业务错误收进统一信封 `code=-1` |
| `backend/app/services/directory_dialog.py` | 独立子进程跑 tkinter 选目录；模块级锁保证同时只有一个框，300s 超时 |
| `frontend/src/views/YapiSync.vue` | 表单、扫描结果折叠表、行勾选、同步确认、结果标记、预览抽屉 |
| `frontend/src/api/devtools.ts` | 三个接口的 TS 封装与长超时配置 |

识别的 Spring / 文档注解以代码为准：类级 `@RestController`、`@Controller`；映射
`@RequestMapping`（类级拼前缀；方法级未写 `method` 时按 GET 并告警）与
`@GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping`；参数
`@RequestBody/@RequestParam/@PathVariable/@RequestHeader/@CookieValue/@ModelAttribute/@RequestPart`
及 `MultipartFile`；文档 `@Tag/@Api/@Operation/@ApiOperation/@Schema/@ApiModelProperty/@Parameter/@ApiParam`，
缺注解时用 Javadoc 首行兜底。`HttpServletRequest` 等框架参数类型直接跳过。

## 接口

后端路由（均需登录，统一信封 `{code, message, data}`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/devtools/pick-directory` | 无请求体；`data` 为选中目录，取消为 `null`，弹不出框 `code=-1` |
| POST | `/api/devtools/yapi/scan` | body `{root_path}`；返回文件/Controller/接口/分类清单、schema 与 warnings |
| POST | `/api/devtools/yapi/sync` | body `{root_path, base_url, project_id, token, endpoint_keys?}`；`endpoint_keys` 不传=全部，传空数组=一个都不同步 |

后端访问 YApi 的三个出站接口：`GET /api/interface/list_menu`、
`POST /api/interface/add_cat`（表单）、`POST /api/interface/save`（JSON），
token/project_id 由客户端逐请求带上。YApi 业务错误同样回 HTTP 200，以 `errcode != 0` 判定。

## 已知边界

- 路径语义绑定后端机器：服务部署在服务器上时，选择框弹在服务器桌面（无显示服务会失败），
  不能浏览操作者本机目录，只能手填服务器上已有的路径
- DTO 类型必须在扫描路径下有源码才能展开字段；找不到的类型按空对象处理并汇总告警。
  常见统一包装（`Result`/`R`/`PageResult`/`PageInfo` 等）在源码缺失时按约定结构补全
- 保护上限：Java 文件 20000 个、Schema 嵌套 6 层、警告保留 60 条（超出记「另有 N 条」）；
  `@ModelAttribute` 对象拍平最多 2 层
- 单文件读取/解析失败只告警并跳过，不拖垮整次扫描；同时只允许一个目录选择框
- 更新依赖 YApi `save` 的 upsert 行为，不记录也不更新接口的人工改动以外的字段合并策略

## 测试

- `backend/tests/test_yapi_scanner.py`：基于 `tests/fixtures/yapi_demo`（12 个 Java 文件、
  2 个 Controller、9 个接口）断言分类与 `@Tag` 命名、泛型 `Result<T>` 出参展开、
  `PageResult` 分页、multipart 入 form、record 组件校验注解、`@ModelAttribute` 拍平、
  源码缺失时 `PageResult`/`Result<Void>` 约定补全、同方法同路径保留前者、重名 Tag 追加类名
- `backend/tests/test_yapi_sync.py`：用 `FakeYapi` 测编排（新增/更新计数、勾选集合、
  空勾选不连 YApi、payload 各字段、单条失败不中断、非法 path 不发请求）；用
  `httpx.MockTransport` 测客户端协议（三个出站接口、HTTP 200 下 `errcode` 报错、502、
  base_url 规范化、错误文本 token 脱敏）
- `backend/tests/test_yapi_router.py`：三个路由的信封、坏路径与 `YapiError` 收进
  `code=-1`、pick-directory 的选中/取消/不可用三种返回
- 未覆盖：tkinter 真实弹框（子进程 + 桌面环境，无法在 CI 验证）与前端页面
