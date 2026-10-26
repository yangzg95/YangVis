# 数据库工作台（MySQL）

运维「数据库」页：左侧连接树、中间 Navicat 式多页签工作区、右侧可收起的 AI 问答。
在管理员登记的 MySQL / MariaDB 连接上跑查询、看结构、导数据、设计表；整页默认
只读，而且这句保证不靠 SQL 解析——每条会话开在 `SET SESSION TRANSACTION READ
ONLY` 里，解析被绕过时服务端自己拒绝写入。连接台账的权限闸门与审计口径见
[ops-servers.md](ops-servers.md)，右侧 AI 通道见 [ops-chat-confirm.md](ops-chat-confirm.md)，
同一台账下的 Redis 连接见 [ops-redis.md](ops-redis.md)。

## 用户可见行为

- **连接树**：连接 → 库（带表数）→ 表/视图，逐级懒加载；连接节点有测试状态色点、
  可配展示色（树行与页签头一起染色，方便认出生产库）。双击表打开数据页签；
  连接/库节点双击是打开/关闭，关闭要二次确认并连带关掉它的页签。
- **多页签工作台**：数据、结构、设计表、ER 图、查询各是一种页签，同一张表去重激活，
  查询页签按连接各自编号（`查询 1 @ 订单库`），可同时开多个。关查询页签时编辑器里
  还有没执行/没收藏的 SQL 会先确认。
- **多页签查询**：CodeMirror 6 编辑器（MySQL 方言，关键字/表名/字段补全，补全元数据
  按库缓存），Enter 执行、Shift+Enter 换行；有选区只执行选中段；可一键美化 SQL
  （可 Ctrl+Z 撤销）；「停止」只断开前端等待。工具栏有默认库选择器。
- **Navicat 式批量执行**：整段脚本拆成多条语句在同一条会话里逐句跑，一句失败不中断。
  底部面板：每条有结果集的语句一个「结果 N」页签，外加「消息」（逐句 OK/报错/耗时，
  点行跳到对应结果页签）和「摘要」（总数/成功/失败/起止时间/总耗时）。单句成功直落
  结果页，单句失败把原因显示在错误条上。
- **SQL 收藏**：编辑器内容一键收藏，可选「仅当前连接」或「所有连接通用」；收藏夹弹层
  里点一条只回填编辑器、不直接执行。
- **查询历史**：复用审计记录，按当前连接取最近 100 条真 SQL（导出/改值等服务端动作
  记录被过滤掉），成功/失败色点、被网关拒绝/写操作有标记；点一条同样只回填编辑器。
- **结果网格**：固定表头、列宽可拖（按查询签名持久化）、Null 占位、长内容展开弹窗、
  双击复制、右键复制整行 CSV/JSON。右键「保存数据为…」支持 CSV / JSON / Markdown /
  INSERT 四种格式；查询结果集导的是手上这份已截断的结果（没有可靠表名，不给 INSERT）。
- **数据浏览**：分页（50/100/200/500）、列头排序（多列带序号）、一个 WHERE 条件栏
  （手输或右键「筛选 = 值」追加，回车生效），底部栏展示实际执行的 SELECT。
- **行内改值与删除记录**：连接开了写开关且账号有写权限时，单击单元格直接行内编辑
  （Enter/失焦提交、Esc 取消），右键可设 NULL / 空白字符串、粘贴、在大文本弹窗里编辑、
  删除记录（先弹确认，摘要前四列值防点错行）。前端不拼任何 SQL。
- **导出分两条路**：网格菜单「导出当前页」是浏览器把手上这一页直接序列化下载；
  「导出全部行」带当前筛选/排序条件走服务端分批流式，受 `OPS_EXPORT_MAX_ROWS` 约束，
  超上限拿到明确报错而不是半截文件。
- **表结构**：字段 / 索引 / 外键三个分段（联合索引、联合外键按列序归拢），抽屉里看
  `SHOW CREATE TABLE` 原文（可复制、可格式化）。外键只读。
- **设计表**：表单编辑列（增删改、改名、排序、类型、可空、默认值、自增、ON UPDATE、
  注释）、主键勾选、BTREE 索引与表注释；点「预览变更」先算出一组带动作标签和风险提示
  的 ALTER，确认后才执行，逐句成败回填在同一个弹窗里。视图、生成列、外键不进表单。
- **ER 图**：一个库的外键关系画成 Mermaid erDiagram，有关系的表优先、可多选指定表、
  缩放、下载 SVG；表太多时只画前 40 张并明确提示。
- **一键备份**：库节点右键，可选「结构+数据 / 仅结构」、整库或勾选若干表，产物是一个
  `.sql`（建库切库 → 逐表 DDL → 分批多行 INSERT），可下载到本地或存百度网盘。
- 手机端整个工作台不挂载（编辑器与网格都是桌面交互），显示桌面端提示。

## 关键取舍

### 只读保证为什么放在连接层，而不是 SQL 解析

SQL 解析（`app/services/ops/safety.py` 的 `classify_sql`）只是第一道门：注释/字符串/
多语句/WITH 后接写操作这类形态靠词法扫描拦住。真正的保证在 `_connect_mysql`：只读通道
执行 `SET SESSION TRANSACTION READ ONLY`，再设 `MAX_EXECUTION_TIME`（MariaDB 退化为
`max_statement_time`），连接的 `read_timeout/write_timeout` 也一起设上。台账上的
`writable` 开关只对人手敲的控制台生效，且要和用户的 `ops_write` 通行证同时成立
（`allow_write AND item.writable`）；元数据浏览、数据浏览、导出、备份、AI 通道永远走
只读路径，与开关无关。通行证与审计留痕的统一口径在 [ops-servers.md](ops-servers.md)。

### 批量执行的结果摘要口径

- 脚本先经 `split_sql_script` 拆分（字符串/反引号/注释里的分号不切，纯注释片段丢弃，
  不支持 DELIMITER），一次最多 100 句；
- **先整体过一遍网关**：被拒的语句不占数据库连接，直接落成 error 占位；放行的语句统一
  补 `ensure_limit`（裸 SELECT/WITH 补 `LIMIT 200`）后才进同一条会话；
- 同会话逐句执行、**continue-on-error**：可写会话是 autocommit 的，前半句成功落盘不受
  后半句失败影响（与 Navicat 一致）；只读会话收尾 rollback；
- 消息口径：结果集显示「N 行」，无结果集显示「执行完成，影响 N 行」（DML 取
  `cursor.rowcount`）；SQL 在消息里压成单行、截到 200 字，完整脚本只在审计记录里；
- 审计**每批一条**（verdict：任一句被拒记 `forbidden`，否则 `readonly`/`write`；
  成功与否看 `failed == 0`），因为历史弹层按「次」展示，每句一条会把历史刷成噪音。

### 为什么导出要分当前页 / 全量两条路

「当前页」导的是用户正在看的这批行——数据已经在浏览器里，`grid.ts` 的 `exportText`
直接序列化成 CSV（带 BOM、CRLF）/ JSON / Markdown / INSERT，一个 Blob 下载，零额外
请求；INSERT 的字面量转义与服务端 `_display_literal` 对齐。「全部行」可能是几十万行，
不能让前端翻页拼文件：服务端在同一把只读连接上按 500 行一批 LIMIT/OFFSET 取、边取边
流式吐，峰值内存是一批而不是整表；用户没给排序时补确定排序键（主键，没主键退第一列），
否则同一行会跨批重复或漏掉。两条路共用一套格式名、后缀和菜单，口径不分叉。导出与浏览
还共用 `_browse_clauses` 拼同一套 WHERE/ORDER BY，「导出全部」命中的必须是屏幕上这套
条件筛出的全集。

### 设计表为什么先预览 ALTER

表单里的「期望定义」和 `information_schema` 的现状之间是一次 diff（纯函数
`build_alter_preview`，不碰数据库），产出按动作分组的语句（新增/改名 CHANGE/修改/
删列、主键、索引、注释）和风险提示（类型收窄可能截断、删列不可恢复、索引重建锁表、
主键变更要重建表）。用户核对的是**即将真正发出去的 SQL**，确认后前端把这批语句原样回
传执行——执行侧不重算 diff，但逐句再过一次可写网关，并校验语句必须以
`ALTER TABLE \`schema\`.`table\` ` 开头，设计表页签改不动别的表。预览与执行是两次请求，
中间表被别人改过防不住（也没有事务能防 DDL），失败的那句带着 MySQL 原始错误返回，和手
敲批量执行同一套 continue-on-error 语义。

注入面只可能来自类型串和 DDL 文本：类型走正则白名单（类型名+括号参数+unsigned 等），
列名/注释/默认值里的引号、反斜杠、分号、注释符一律拒绝，不赌会话的 `NO_BACKSLASH_ESCAPES`。

### 写操作怎么过闸门

三条写路径（控制台 DML/DDL、数据页行内改值/删行、设计表）是同一套双闸门：**连接台账的
`writable` 开关 × 用户 `ops_write` 通行证**，缺一即拒，拒绝也落审计（`forbidden`）。
行级写与设计表的 SQL 全由服务端生成：列名先对 `information_schema.COLUMNS` 核真实存在，
值全部参数化（行定位有主键用主键，没主键退化为全列 NULL 安全的 `<=>` 匹配），语句一律
带 `LIMIT 1`，空 WHERE 的整表写在协议上就不存在；另生成一份字面量版语句仅供审计展示和
网关兜底判定，永不直接执行。影响行数为 0 报「数据可能已被他人修改，请刷新后重试」。
AI 通道不享受写开关：即使连接可写，AI 发起的执行仍是只读判定加只读会话；它要落写操作
只能走确认卡片，见 [ops-chat-confirm.md](ops-chat-confirm.md)。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` | `OpsDatabase` 台账、`OpsSqlFavorite` 收藏、`OpsAudit` 审计（历史复用） |
| `backend/app/services/ops/database.py` | 台账 CRUD（名称全局唯一、密码 Fernet 加解密、出参 `mask()`）、收藏 CRUD（通用/绑定连接）、`OpsDbError`/`OpsDbForbidden` |
| `backend/app/services/ops/db_exec.py` | 连接原语 `_connect_mysql`（READ ONLY + 超时）、单句/批量执行、`execute`/`execute_batch`、连通性探测、`MAX_PAGE_SIZE=500` |
| `backend/app/services/ops/db_schema.py` | 库/表/列/索引/外键/DDL/ER 图/补全元数据，全走 information_schema 参数化查询；`_quote_ident` 反引号转义 |
| `backend/app/services/ops/db_data.py` | 数据浏览（COUNT+分页同连接）、四种格式分批流式导出、行内改值/删行的语句生成与执行 |
| `backend/app/services/ops/db_design.py` | `table_def` 读现状、纯函数 `build_alter_preview` 做 diff、`apply_alter` 逐句网关+目标表校验执行 |
| `backend/app/services/ops/db_backup.py` | 整库/选表备份的准备（逐表 DDL+行数核算）、`.sql` 文本流、网盘 bytes 归集 |
| `backend/app/services/ops/safety.py` | `split_sql_script`、`classify_sql`/`classify_sql_writable`、`ensure_limit` |
| `backend/app/routers/ops.py` | `/ops/databases*`、`/ops/sql-favorites*` 全部路由；双闸门判定、审计写入、流式响应 |
| `frontend/src/views/OpsDatabase.vue` | 三栏外壳、连接台账弹窗、页签去重/编号/关闭确认、树右键菜单、一键备份弹窗 |
| `frontend/src/components/ops/tabs.ts` | 树节点与 `TabRequest` 类型（key 里编码类型与坐标） |
| `frontend/src/components/ops/DbConnectionTree.vue` | 懒加载连接树、双击开关、状态色点、整行染色 |
| `frontend/src/components/ops/DbQueryTab.vue` | 查询页：工具栏、编辑器调高、底部结果/消息/摘要面板、收藏、历史、询问 AI |
| `frontend/src/components/ops/SqlEditor.vue` | CodeMirror 6 封装：MySQL 方言补全（Compartment 热更表结构）、Enter 执行/选区 |
| `frontend/src/components/ops/DbResultGrid.vue` | 查询结果网格：量高、右键菜单（只读复制类）、结果集导出、CellViewer |
| `frontend/src/components/ops/DbDataTab.vue` | 数据浏览：分页/排序/WHERE 栏、行内编辑与删除、当前页/全部导出 |
| `frontend/src/components/ops/DbStructureTab.vue` | 字段/索引/外键分段 + DDL 抽屉 |
| `frontend/src/components/ops/DbDesignTab.vue` | 设计表表单、预览 ALTER 弹窗（动作标签/风险提示/逐句结果） |
| `frontend/src/components/ops/DbErdTab.vue` | Mermaid erDiagram（懒加载 chunk、strict 模式）、缩放/SVG 下载 |
| `frontend/src/components/ops/grid.ts` | 二维数组↔表格记录、四种格式的前端序列化、字面量转义、列宽估算 |
| `frontend/src/components/ops/GridContextMenu.vue` / `CellEditor.vue` / `CellViewer.vue` | 两级右键菜单、长文本编辑弹窗（可设 NULL）、只读查看弹窗 |

主要路由（前缀 `/ops`）：

| 方法与路径 | 说明 |
| --- | --- |
| `GET/POST/PUT/DELETE /databases[/{id}]` | 台账；增删改仅管理员，密码出参脱敏 |
| `POST /databases/{id}/test` | `SELECT VERSION()` 探活，结果回写台账 |
| `POST /databases/{id}/execute`、`/execute/batch` | 单句（MySQL/Redis 共用）与批量（仅 MySQL） |
| `GET/POST/PUT/DELETE /sql-favorites[/{id}]` | 按用户隔离的收藏；列表 = 绑定连接 + 通用 |
| `GET /databases/{id}/schemas`、`.../tables`、`.../tables/{t}/columns` | 连接树数据源 |
| `.../tables/{t}/ddl`、`/indexes`、`/foreign-keys`、`/table-def` | 结构页与设计表初值 |
| `GET .../schemas/{s}/erd`、`/completion` | ER 图数据、编辑器补全元数据 |
| `GET .../tables/{t}/rows`、`/rows/export` | 数据浏览（JSON 数组传 filters/order_by）、全量流式导出 |
| `PUT/DELETE .../tables/{t}/rows` | 行内改值 / 删除记录（body 带整行原值 key） |
| `POST .../tables/{t}/table-def/preview`、`/apply` | 设计表 diff 预览与执行 |
| `GET .../schemas/{s}/backup`、`POST .../backup/netdisk` | 备份下载流 / 存网盘 |
| `GET /audit?target_type=database&target_id=...` | 查询历史弹层的数据源 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OPS_SQL_ROW_LIMIT` | 200 | 控制台与批量执行每条语句的结果集上限；`ensure_limit` 给裸 SELECT/WITH 补的也是它 |
| `OPS_EXPORT_MAX_ROWS` | 200000 | 全量导出行数上限，COUNT 后超限即拒，不开始流 |
| `OPS_BACKUP_MAX_ROWS` | 1000000 | 备份累计行数上限，逐表计数时超限即拒；仅结构备份不受限 |
| `OPS_BACKUP_MAX_BYTES` | 64MB | 存网盘的字节上限（网盘接口只收完整 bytes，超限传之前先拒） |
| `OPS_CMD_TIMEOUT` | 20 | 连接读写超时，同时作为 `MAX_EXECUTION_TIME`（毫秒） |
| `OPS_SSH_TIMEOUT` | 15 | 建连超时（MySQL 与 SSH 通道共用） |
| `OPS_OUTPUT_LIMIT` | 8192 | 标量文本截断长度，MySQL 结果集走行数上限不沾它（Redis 回复用） |

代码内常量（不读环境变量）：批量 `_BATCH_MAX_STATEMENTS=100`、消息预览 200 字、
浏览页大小上限 `MAX_PAGE_SIZE=500`（导出批次同值）、手输 WHERE 上限 1024 字符、
浏览类元数据上限 `_BROWSE_CAP=1000`、补全元数据 `_COMPLETION_CAP=20000`、
ER 图 `_ERD_MAX_TABLES=40` / 每实体 12 列、备份 `TABLE_CAP=400` 张表。

## 已知边界

- 只读会话是 MySQL 语义：`TRANSACTION READ ONLY` 与语句超时在 MySQL 5.7+/8.0 完整生效，
  MariaDB 只退化到 `max_statement_time`；目标不支持任何超时时只是少一道兜底。
- 批量执行不支持 `DELIMITER`、存储过程/触发器这类多语句复合定义；脚本上限 100 句。
- 全量导出与备份用 LIMIT/OFFSET 分批，深偏移页在大表上慢；一致性靠 InnoDB 可重复读
  快照（同 `mysqldump --single-transaction`，不锁表），非事务引擎不提供这层保证。
- 「导出全部」前端仍把整个文件攒成 Blob 再下载（20 万行量级可接受），更大规模需要流式
  落盘；文件一旦开始流，中途出错只能截断，所以所有可预期错误都在第一批之前判掉。
- 行内编辑提交的是字符串，类型靠 MySQL 隐式转换；行定位依赖读到的原值，无主键表退化为
  全列 `<=>` 匹配，重复行会只改/删其中一条（`LIMIT 1`）。
- 设计表不防预览与执行之间的并发 DDL，不改外键、不支持生成列；非 BTREE 索引在表单里
  只展示不改（但会原样带回，避免被 diff 当成删除）。
- 备份文件刻意不含 `DROP TABLE`，还原到已有同名表的库需先手工清表；视图只备份定义。
- 查询历史就是审计记录，能看多久取决于审计保留策略；收藏按用户隔离，台账连接全员共用
  （仅管理员可增删改）。

## 测试

- `backend/tests/test_ops_safety.py`：脚本拆分（字符串/反引号/注释/转义里的分号、
  未闭合串）、注释剥离、只读与可写判定（多语句/WITH 藏写/高危函数/账户与实例级操作
  仍拒）、Redis 白名单。
- `backend/tests/test_ops_export.py`：导出准备阶段的 COUNT 探测与稳定排序（有主键/无主键
  退第一列/保留用户排序与参数化筛选）、批次 LIMIT/OFFSET 拼接与短批收尾、失败与客户端
  中途断开都关连接、四种格式序列化（CSV BOM+CRLF、JSON 非 ASCII 与空表、Markdown 转义、
  INSERT 一批一条与日期/字节串化）、敌意 WHERE 与超长 WHERE 被拒。
- `backend/tests/test_ops_table_design.py`：diff 纯函数全覆盖——无变化零语句、新增列位置
  /FIRST、删列 destructive 与警告、改名走 CHANGE、来源列不存在被拒、类型收窄警告、纯调位
  用最长递增子序列做到最少语句、默认值三态、自增/ON UPDATE、生成列拒绝、重名/恶意类型/
  引号反斜杠拒绝、主键换一句完成/列不存在/删主键警告、索引先删后建与重排即重建、表注释；
  apply 侧拒绝改其他表、小写目标前缀、一行藏两句、空语句。
- `backend/tests/test_ops_backup.py` / `test_ops_backup_api.py`：准备阶段（基表 DDL+取数
  计划配对、视图只留定义、仅结构不碰数据、空库/超 400 表/行数累计超限/失败关连接）、选表
  归一（点名查、缺表报错、去空白去重、空名单=整库、超上限）、文件结构（头注释与范围、
  建库字符集、FOREIGN_KEY_CHECKS 开关、视图顺序、客户端断开收尾、close 幂等）、网盘字节
  上限与文件名时间戳；API 层覆盖 Redis 拒绝、网盘未启用/未绑定/超限/上传成败审计、下载
  流与 UTF-8 文件名、tables 参数畸形报错。
- 目前**没有**针对行级写（`update_row`/`delete_row`）、收藏 CRUD、批量执行摘要口径的
  独立测试文件，这三块的安全前提分别由安全网关测试与设计表 apply 测试间接覆盖。
