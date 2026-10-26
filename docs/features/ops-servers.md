# 服务器台账与 SSH 连接

运维「服务器」页是所有运维能力的入口：一张**全员共用**的服务器台账（地址、登录用户、
加密凭据），一条带主机指纹校验的 SSH 建连通道，以及一块全量操作审计面板。终端、SFTP、
AI 运维助手都从台账取机器、复用同一个 `connect()`；谁能写、写了什么，则由用户身上的
`ops_write` 单个权限位和 `ops_audit_log` 一张表统一收口。相邻专文只引用：Web 终端见
[ops-terminal.md](ops-terminal.md)，AI 写操作两阶段确认见 ops-chat-confirm.md，
SFTP 见 ops-sftp.md，MySQL/Redis 控制台见 ops-database.md / ops-redis.md。

## 用户可见行为

- 页面左侧服务器列表，顶部四张统计卡（总数 / 可用 / 异常 / 未测试），点卡片等同切状态
  筛选；名称或 IP 关键字走后端模糊搜索（300ms 防抖），连接状态在前端就地过滤。
- 每张卡片展示名称、`用户名@主机:端口`、认证方式（密码 / 私钥）、上次检测时间、备注；
  凭据无法解密时卡片上直接出现「凭据无法解密」红条（`credential_error`）。
- 「测试连接」所有登录用户都能点：后端真的建一次 SSH、跑 `uname -a`（10 秒超时、
  输出取 512 字符），结果写回台账的 `last_check_ok / last_check_error / last_checked_at`，
  列表圆点据此显示可用 / 异常 / 未测试。
- 「连接」或双击卡片在新标签页打开 Web 终端（见 ops-terminal.md）；终端握手要求
  `ops_write`，只读账号根本进不去。
- 新增 / 编辑 / 删除服务器只对管理员可见，后端三个端点同样挂 `require_admin`。
  编辑时密码 / 私钥留空表示「保持原样」；接口里凭据永远只回掩码（`sk-****` + 末四位）。
- 主机公钥与首次连接记录的指纹不一致时，所有入口都会被拒绝并给出醒目文案（服务器可能
  重装、也可能存在中间人），引导管理员在编辑页处理；修改主机 / 端口 / 登录用户 / 认证
  方式会自动作废旧指纹并清空上次检测结果。
- 右侧「最近操作记录」面板取最新 10 条 `target_type=server` 的审计：AI 跑的只读巡检、
  终端里手敲的命令、确认后执行的写操作、被拒绝 / 被拦下的尝试都在同一条时间线上，
  标明手动还是 AI、目标机器、时间与错误信息。

## 关键取舍

### 为什么台账全员共用

服务器是团队共享资产，不是个人备忘：只读账号也要能看到有哪些机器、让 AI 做只读巡检、
（将来）申请别人登记的机器上的信息。所以 `OpsServer.owner_id` 只记录登记人 / 日志归属，
**不再参与可见性过滤**（`_scope()` 保留为空壳只是让各查询点结构不变）；服务层
docstring 明确写着「台账全员共用，增删改由路由层限定管理员」。代价是名称必须全局唯一：
数据库层只有历史遗留的 `(owner_id, name)` 复合索引，全局唯一靠 `_require_unique_name`
预查 + 提交时捕 `IntegrityError` 双保险，存量历史重名不强刷，只挡新增和改名。

台账「改什么」（管理员）和「用它做什么」（`ops_write`）是两把独立的锁：能登记机器不
等于能在机器上动手，反之持票用户也不需要拿到台账管理权。

### TOFU 首次指纹信任

`known_hosts=None` 是这类工具的默认写法，等于关掉中间人防护；而自托管场景里几乎没人
维护一份现成的 known_hosts。折中是 TOFU（Trust On First Use）：首次连接时由
`_PinnedHostKeyClient.validate_host_public_key` 抓下服务端公钥指纹（asyncssh 的
`get_fingerprint()`，MD5 形态），写入 `ops_server.host_key`；之后每次握手逐字节比对，
对不上抛独立的 `HostKeyMismatch`——它和「连不上」语义完全不同，绝不自动接受新指纹。

两个刻意的细节：自动固定指纹只发生在**用户自己发起**的连接上（`connect(server, service)`
传了 service 才回写），AI 工具调 `connect(server)` 不传，模型没有替用户信任一台主机的
资格；换地址、换端口、换用户、换认证方式都视为「换了台机器」，`update()` 主动清空
`host_key`，让下次连接重新走首次信任。

### 权限为什么做成单个位

写权限是 `sys_user.ops_write` 一个布尔位，判定只有一行：`CurrentUser.can_ops_write =
is_admin or ops_write`，管理员是天然超集。不做命令级 / 机器级细粒度授权，因为这一层
已经有更细的分级在兜底：AI 通道有 `safety.py` 的三级判定（只读直跑 / 待确认 / 禁止，
见 ops-chat-confirm.md），数据库还有「连接 writable 开关 × 用户通行证」双闸门。在这
之上再切一堆权限组合，只会让「为什么他能执行我不能」变成无法审计的配置谜题。真正的
保证在后端每一处写入口强制，前端置灰只是体验：

- 终端 WS 握手：无票直接以 close code 1008 拒绝（前端则干脆不挂载终端）；
- AI 写命令 confirm：`ops_write` 检查在 CAS 认领之前，无票不能把 pending 认领成
  approved；但**拒绝不需要权限**——拒绝永远是安全方向；
- MySQL 控制台 / 行内改值 / 表设计变更 / Redis 写端点：`row.writable and user.can_ops_write`
  双闸门，连接没开写开关或用户没票都不行；
- SFTP 上传 / 新建 / 删除 / 重命名：`_require_ops_write`，列目录与下载保持只读开放。

存量升级时 `bootstrap.py` 幂等 ALTER 出这一列并把老用户全部回填为 1（grandfather：
旧模型下他们本来就什么都能做），新建账号才默认 0。登录响应按两位推导出
`permissions: ["ops:write"]`，前端 `auth.canOpsWrite` 同一口径。

### 审计为什么统一一张表

AI 执行、终端手敲、SQL/Redis 控制台、SFTP 是四条完全不同的代码路径（异步工具、WS 字节
流、同步路由、流式上传），如果各记各的，「事后查这个人对这台机器做过什么」就得查四
个地方、对齐四种格式。它们全部写同一张 `ops_audit_log`，用 `actor`（user / ai）和
`verdict` 两个维度区分来路与性质，用 `target_type + target_id` 挂回台账资产。

`owner_id` 记的是**操作者本人**而不是资产登记人——台账共用之后张冠李戴会让审计失去
意义。可见性因此很简单：普通用户只看自己的操作，管理员能看到所有人对共享资产的操作，
这正是共享资产需要的团队可见性。AI 通道的审计写入自带 try/except 兜底：审计落库失败
只记日志，绝不拖垮正在进行的会话。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `OpsServer` / `OpsAuditLog` / `SysUser.ops_write` | 服务器表（加密凭据列、`host_key`、最近检测三列）、审计表、权限位 |
| `backend/app/services/ops/server.py` | `OpsServerService`（台账 CRUD、全局唯一名、`pin_host_key`、`record_check`、掩码序列化）；`connect()`（TOFU + keepalive）、`run_once()`（超时包装 / 输出截断）、`test_connection()`、`detect_server_os()` |
| `backend/app/services/ops/safety.py` | `Verdict` 与 `classify_command` / `classify_sql(_writable)` / `classify_redis(_writable)` 纯函数判定 |
| `backend/app/services/ops/actions.py` | AI 写命令待确认项的落库状态机（pending/approved/rejected/expired/executed/failed），详见 ops-chat-confirm.md |
| `backend/app/services/chat_ops.py` `Auditor` | AI 通道审计写入（自开短会话、可放工作线程、失败不炸会话） |
| `backend/app/services/ops/terminal.py` | `CommandLineTracker` 从 PTY 字节流重建命令行，`record_manual_command()` 记 `manual` |
| `backend/app/routers/ops.py` | `/servers` 五个端点、`/audit` 查询、终端 WS 握手闸门、数据库 / Redis 控制台双闸门与审计 |
| `backend/app/routers/ops_files.py` | SFTP 端点：`_require_ops_write` 闸门、文件写操作的 `sftp` 审计 |
| `backend/app/routers/chat.py` | `/actions/{id}/confirm` 在 CAS 之前查 `can_ops_write`；reject 不设限 |
| `backend/app/deps.py` + `backend/app/models/schemas.py` | `require_user` / `require_admin`；`CurrentUser.can_ops_write` |
| `backend/app/crypto.py` | Fernet `encrypt/decrypt`、展示掩码 `mask` / `is_masked` |
| `backend/app/config.py` | `OPS_SSH_TIMEOUT` 等连接参数（见下） |
| `backend/app/bootstrap.py` | `sys_user.ops_write`、`ops_audit_log.pending_action_id` 幂等 ALTER 与老用户回填 |
| `frontend/src/views/OpsServer.vue` | 统计卡 / 列表 / 搜索筛选 / 测试 / 台账编辑弹窗 / 最近操作面板 |
| `frontend/src/stores/auth.ts` | `canOpsWrite`（与后端同一口径，仅供按钮置灰） |

### SSH 建连与命令执行

`connect(server, service=None)` 的参数组装：`connect_timeout=OPS_SSH_TIMEOUT`，外层再套
`asyncio.wait_for(..., OPS_SSH_TIMEOUT + 5)` 防握手卡死；`OPS_SSH_KEEPALIVE > 0` 时加
`keepalive_interval` 与固定的 `keepalive_count_max=3`，所有 SSH 通道（终端 / SFTP /
AI）统一受益。密码认证解密 `password_enc`；私钥认证解密后经
`asyncssh.import_private_key(raw_key, passphrase)` 预解析，解析失败转成
`OpsConnectError("私钥无法解析：…")`。指纹异常、超时、OSError/asyncssh.Error 三类失败
分别落成 `HostKeyMismatch`、超时文案和「连接失败：…」。

`run_once()` 面向一次性执行：默认 `OPS_CMD_TIMEOUT` 秒、输出截断到 `OPS_OUTPUT_LIMIT`。
远端 OS 用 `uname -s` 探测并按 `server_id` 缓存在模块级 `_server_os_cache`；Linux 下
命令被 `timeout Ns sh -c '…'` 包装（单引号转义），超时由远端杀进程、退出码 124 统一
翻译成「已中断」；非 Linux 不包装，超时时明确提示「远端进程可能仍在运行」。stderr 非空
时以 `\n[stderr]\n` 拼到 stdout 后面。AI 通道调用时单独传 `OPS_AGENT_CMD_TIMEOUT`，
`du`/`find` 这类盘点命令不被 20 秒误杀。

### 凭据存储

三个凭据列 `password_enc` / `private_key_enc` / `passphrase_enc` 都是 Fernet 密文
（AES-128-CBC + HMAC），密钥是环境变量 `ENCRYPTION_KEY`——与模型 API key 同一套
`app/crypto.py`，密钥永不落库。空明文不加密（存空字节）。`to_item()` 按当前认证方式
解密对应字段，成功只回 `mask()` 后的掩码；解密抛 `DecryptionError`（典型原因是
`ENCRYPTION_KEY` 轮换）时不抛 500，而是回空掩码加 `credential_error`，并打 warning 日志。
更新时 `is_masked()` 识别回传的掩码值，防止「只改备注」把掩码当新凭据写回去、悄悄清掉
一把能用的密码。

### 审计记录形态

`ops_audit_log` 真实字段：`id`、`owner_id`（操作者）、`target_type`（`server` |
`database`）、`target_id`、`target_name`（登记时刻下的名字快照）、`actor`（`user` |
`ai`）、`command`（LONGTEXT，各写入点统一截到 8000 字符）、`verdict`、`success`、
`error`（≤512）、`pending_action_id`（反链 `ops_pending_action`，只读 / 手动为 NULL）、
`created_at`；索引 `idx_audit_owner_time(owner_id, created_at)`。

| verdict | 含义 | 主要写入点 |
| --- | --- | --- |
| `readonly` | 白名单内的只读执行（成功与失败都记） | AI 巡检工具、MySQL 只读控制台 |
| `write` | 可写连接 × 持票用户的控制台写执行（含执行失败） | SQL 控制台 / 批量、行内改值、表设计、Redis 写 |
| `confirmed` | AI 写命令经用户确认后执行，带 `pending_action_id` 反链 | `chat_ops.execute_confirmed` |
| `rejected` | 用户拒绝了 AI 的写提议 | `chat/service.py reject_action` |
| `forbidden` | 被闸门挡下：危险命令、无写权限、只读连接上的写尝试、硬禁 SQL/Redis 操作 | 各执行通道的拒绝分支 |
| `manual` | 人在 PTY 里手敲；`success` 只表示「已送达」，拿不到退出码 | `terminal.record_manual_command` |
| `sftp` | SFTP 文件写操作（上传 / 新建 / 删除 / 重命名等） | `routers/ops_files.py` |

> `sftp` 是代码里实际在用的取值，但 `OpsAuditLog.verdict` 的字段注释只列了前六种，
> 以代码为准。手敲命令的重建是尽力而为（退格 / Ctrl+U/W/Ctrl+C 处理，翻历史不保证），
> 且输出侧出现密码提示后下一行输入绝不落审计。

查询接口 `GET /api/ops/audit`：参数 `target_type`（正则限定 `server|database`）、
`target_id`、`limit`（1–200，默认 50）、`offset`；按 id 倒序，`total` 是过滤后的真实
总数。响应项 `OpsAuditItem` 暴露 id / 目标三元组 / actor / command / verdict / success /
error / created_at，不含 `owner_id` 与 `pending_action_id`。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENCRYPTION_KEY` | 空（不提供则启动失败） | Fernet 密钥，保护 SSH 凭据与模型 API key；轮换后旧密文不可解密 |
| `OPS_SSH_TIMEOUT` | 15 | SSH 建连超时秒数；外层兜底再宽限 5 秒 |
| `OPS_SSH_KEEPALIVE` | 25 | keepalive 间隔秒数，空闲防空掐；0 关闭；无响应上限固定 3 次 |
| `OPS_CMD_TIMEOUT` | 20 | 一次性 SSH 命令默认超时（测试连接固定用 10 秒） |
| `OPS_AGENT_CMD_TIMEOUT` | 300 | AI 通道单条命令超时，盘点命令留 5 分钟 |
| `OPS_OUTPUT_LIMIT` | 8192 | 单条命令回传 / 入库的输出字符上限，超出截断并标注 |

## 测试

- `backend/tests/test_ops_safety.py`（19）：SQL 脚本扫描器（字符串 / 反引号 / 注释里的
  分号不误切、转义与双写引号、未闭合保守处理）、只读判定、可写模式放开 DML/DDL 但仍
  拦多语句 / 高危函数 / 账户实例级操作、Redis 可写模式翻转后硬禁清单依旧有效。
- `backend/tests/test_ops_confirm.py`（44）：待确认状态机（CAS 只赢一次、过期不可
  confirm 但可 reject、终态不可重复迁移、启动物化遗留 pending、属主隔离）；`FakeSSH`
  把 `server_ops.connect/run_once` 整体打桩，覆盖只读直跑不打扰用户、危险命令不出卡片、
  只读通道拒绝夹带重定向、AI 通道用 `OPS_AGENT_CMD_TIMEOUT`、一轮回答复用一条 SSH、
  连接失败也留痕、确认执行写审计并带 `pending_action_id` 反链、非零退出记 failed、
  服务器被删、拒绝无需 `ops_write` 等。
- `backend/tests/test_ops_confirm_api.py`（16）：confirm SSE 帧序、无写权限时行留在
  pending、有 `ops_write` 而不是管理员也能确认、重复确认 409 且命令只跑一次、过期 409、
  reject 幂等、过期卡片仍参与回放、属主隔离等。
- `backend/tests/test_ops_redis_write.py`（23）：各类写操作生成的 Redis 命令文本、
  TTL 保留 / 恢复、UNLINK 批量上限与审计文本截断、Redis 错误转成 OpsError 且连接关闭。
- `backend/tests/test_auth_users_api.py`：登录权限由 `is_admin` / `ops_write` 两位推导，
  管理员与持票普通用户都得到 `permissions: ["ops:write"]`。
- 已知缺口：TOFU 指纹的首次固定 / 不匹配拒绝、台账 CRUD 与管理员授权目前没有独立测试
  文件——SSH 路径在确认链路测试里被整体打桩，指纹握手分支尚未被直接钉住。
