# 账号与权限（自建账号、本地 JWT、登录防护）

YangVis 不接任何外部认证服务：账号就是 `sys_user` 表里的一行，密码用 PBKDF2 散列，
登录后由本站自己签发一张 HS256 JWT。以前 `/auth` 路由代理过上游 noetix 平台的身份，
后来收回自建——`owner_id` 是知识库、模型配置等所有按用户隔离数据的隔离键，这个身份
必须是本站自己能背书的值。系统没有公开注册：账号要么由管理员在「账号管理」里创建，
要么在 `sys_user` 为空的首次启动时由环境变量引导出第一个管理员。

## 用户可见行为

- 登录页三要素：用户名、密码、图形验证码（4 位、去除 0/O/1/I 等易混字符，点图换新）；
  密码框检测 Caps Lock 并提示；登录失败自动换一张新验证码。
- 连续失败会被锁：同一「账号 + IP」在 10 分钟窗口内失败 5 次，锁定 10 分钟，期间
  连正确密码都返回「请约 N 分钟后再试」；同一 IP 累计失败 30 次（换账号喷洒）同样锁。
- 「密码错」「用户不存在」「账号已禁用」是**同一句**「用户名或密码错误」，验证码错误
  则单独提示且不计入失败次数（否则谁都能拿别人的用户名乱填验证码替他触发锁定）。
- 管理员「账号管理」页：建号（用户名 2–64 位、密码至少 8 位，昵称留空取用户名）、
  编辑昵称/邮箱/管理员位/运维写位（用户名是登录凭据，创建后不可改）、禁用/启用、
  重置他人密码。不能禁用自己、不能取消自己的管理员身份——前端置灰，后端同样拒绝。
- 权限只有两个布尔位推导：`is_admin` 给出角色 `admin`（否则 `user`）；
  `is_admin or ops_write` 给出 `ops:write` 权限。管理员是运维写的天然超集；
  `ops_write` 控制的具体闸门（确认 AI 写命令、PTY、文件写、可写库写语句）见
  ops-servers.md。
- 禁用在下一次请求即生效，不必等 token 过期；改密与重置密码**不踢**已签发的 token；
  登出只清浏览器本地副本。
- 自助改密接口（须验原密码、新旧不得相同、新密码至少 8 位）后端与前端 API 封装
  都已就绪，目前界面上还没有入口。
- 首次启动：`sys_user` 为空且 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`
  都配了，才创建初始管理员；都没配则照常启动并写一条 warning——没有人能登录，
  但绝不留内置默认密码。建号后环境变量即失效（表非空不再引导），建议从环境里删掉。

## 关键取舍

### 为什么自己实现密码散列和 JWT，不引 passlib/bcrypt

标准库 `hashlib.pbkdf2_hmac` 已够用，依赖列表保持精简。散列串为
`pbkdf2_sha256$260000$salt$digest`（16 字节随机盐），迭代次数写在串内部，
以后调高轮数不会让老密码失效。损坏的散列按「密码错误」处理而不是抛 500，但留 warning。
JWT 只做 access token、不做 refresh token：这是一个内部工具，12 小时（`AUTH_TOKEN_TTL`）
到期重登即可。

### 签名密钥为什么没有默认值

一个「能跑起来」的默认密钥迟早被原样带到生产，那时任何人都能自签管理员 token。
所以 `AUTH_JWT_SECRET` 为空或不足 32 字节时 `validate_settings()` 直接抛
`ConfigurationError` 拒启动，而且检查发生在建表之前——中途失败会留下一个看似已经
成功初始化的半拉数据库。`AUTH_TOKEN_TTL <= 0` 同样拒启动。

### 三种登录失败为什么必须同形，还要补一次假散列

区分「无此用户」和「密码错误」等于把登录接口变成用户名探测机。三种失败统一
HTTP 200 + `code=401` + 同一句话；用户不存在时还对一份现场生成的占位散列跑一次
`verify_password`，让响应时序不泄露账号是否存在。计数键统一 `strip().lower()`：
线上 MySQL 默认排序规则大小写不敏感，不收敛的话 `Alice`/`alice` 各拿一轮尝试额度。

### 验证码为什么必须加密，不能只签名

答案只有 4 位、字母表 32 个字符，任何确定性派生值（签名/MAC）都能被离线穷举出来。
所以 `captcha_id` 本身是用 `ENCRYPTION_KEY` 加密的 Fernet 令牌，payload 就是答案，
答案从不出现在客户端可读处；过期靠 Fernet TTL 校验，篡改解不开。代价是 TTL 内可重放：
可接受，因为尝试次数的真正上限在失败计数，且锁定窗口（10 分钟）长于验证码 TTL
（5 分钟），重放跨不过一个锁定周期。无状态令牌也让 gunicorn 多 worker 下任意进程
都能独立校验。

### 失败计数为什么放进程内存

计数不落库（部署口径），实现是模块内字典 + 线程锁，过期条目顺带修剪。已知取舍：
多 worker 各持一份计数，攻击者实际额度约为「上限 × worker 数」；重启清零也可接受——
锁定本就是短期惩罚。IP 维度取 `X-Forwarded-For` 第一跳（线上在 nginx 后面，
否则全站访客共享反代地址那一份额度），本地直跑回落到对端地址；这一维可被伪造头
绕过，所以只是补充，账号维度才是主力。登录成功只清账号维计数、不清 IP 维：
NAT 出口下一个正常用户的成功登录不该替同源攻击流量销账。

### 无状态 JWT 撤不掉，禁用和权限变更怎么生效

没有 deny list（这个规模不值得）。`require_user` 每次请求都拿 token 里的 `sub`
回库读 `SysUser`：行不存在或 `status=False` 一律 401，禁用因此在下次请求即时生效，
提权也是下次请求即生效。代价是每请求一次主键查询。浏览器 `WebSocket` 构造函数设
不了请求头，token 只能走 query string（会进访问日志），所以握手用单独签发的 60 秒
`scope=ws` 入场票，且 REST 端拒绝带 scope 的票；细节见 ops-terminal.md。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `SysUser` | 表 `sys_user`：`username` 唯一、`password_hash`、`status`、`is_admin`、`ops_write`、`last_login_at` |
| `backend/app/models/schemas.py` | `LoginRequest`（验证码字段必填）、`UserInfo`/`CurrentUser`、`UserCreateRequest`/`UserUpdateRequest`/`UserStatusRequest`/`PasswordResetRequest`/`PasswordChangeRequest`、`UserItem`（无散列字段） |
| `backend/app/security.py` | `hash_password`/`verify_password`（常数时间比较）、`create_access_token`/`decode_access_token`、Bearer 与裸 token 双兼容、WS 入场票签发校验 |
| `backend/app/routers/auth.py` | `/auth/captcha`、`/auth/login`、`/auth/me`、`/auth/logout`、`/auth/password`；锁定→验证码→密码的校验顺序 |
| `backend/app/routers/users.py` | `/users` 整组路由，router 级 `Depends(require_admin)`；建号、部分更新、禁用、重置密码、自保护规则 |
| `backend/app/services/login_guard.py` | 进程内失败计数与锁定：`(username, ip)` 与 `("", ip)` 两维、滑动窗口、锁定后计数归零 |
| `backend/app/services/captcha.py` | Fernet 令牌签发/校验、PIL 绘制干扰 PNG（132×48） |
| `backend/app/deps.py` | `require_user`（身份唯一入口，回库读 status）、`require_admin`（403）、`resolve_ws_user` |
| `backend/app/netutil.py` | `client_ip`：XFF 第一跳、回落对端地址、截断 64 字符 |
| `backend/app/bootstrap.py` | `validate_settings` 密钥强度自检；`bootstrap_admin` 空表引导；`ops_write` 列的幂等 ALTER 与回填 |
| `frontend/src/views/Login.vue` | 登录表单、Caps Lock 提示、验证码刷新、失败换新图 |
| `frontend/src/views/Users.vue` | 用户管理：非管理员显示 403 页、表格/弹窗、自身开关置灰、重置密码提示 |
| `frontend/src/stores/auth.ts`、`utils/auth.ts`、`api/auth.ts` | 登录态、token 的 localStorage 存取（存储被禁用时静默降级）、账号域 API |

### 接口（统一 `/api` 前缀，业务失败走响应体 `code`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/auth/captcha` | 返回 `captcha_id`（Fernet 令牌）与 PNG data URI |
| POST | `/auth/login` | 校验通过签发 token 并返回 `UserInfo`；锁定 429、凭证错 401、验证码错 -1 |
| GET | `/auth/me` | 按 token 解析当前用户资料 |
| POST | `/auth/logout` | 恒成功的信封；真正的清理在客户端 |
| POST | `/auth/password` | 自助改密，须验原密码 |
| GET/POST | `/users` | 列表（id 倒序）/ 建号，仅管理员 |
| PUT | `/users/{id}` | 部分更新（`exclude_unset`，改昵称不清邮箱），仅管理员 |
| PUT | `/users/{id}/status` | 启用/禁用，仅管理员 |
| POST | `/users/{id}/password` | 重置密码（无需原密码），仅管理员 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUTH_JWT_SECRET` | 空（**必填**） | JWT 签名密钥；空或 <32 字节拒启动 |
| `AUTH_JWT_ALGORITHM` | `HS256` | 签名算法 |
| `AUTH_TOKEN_TTL` | 43200 | access token 有效期秒数（12 小时）；≤0 拒启动 |
| `AUTH_CAPTCHA_TTL` | 300 | 验证码令牌有效秒数（5 分钟） |
| `AUTH_CAPTCHA_LENGTH` | 4 | 验证码字符数 |
| `AUTH_LOGIN_MAX_FAILURES` | 5 | 同「账号+IP」窗口内失败上限，达到即锁 |
| `AUTH_LOGIN_LOCK_SECONDS` | 600 | 失败统计窗口与锁定时长（同为 10 分钟） |
| `AUTH_LOGIN_IP_MAX_FAILURES` | 30 | 同 IP 窗口内失败总上限，挡密码喷洒 |
| `BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` | 空 | 仅 `sys_user` 为空时引导初始管理员；密码 <8 位拒启动 |
| `ENCRYPTION_KEY` | 空（**必填**） | 验证码令牌（及模型 api_key，见 model-settings.md）的 Fernet 密钥，须为合法 Fernet key |
| `OPS_WS_TICKET_TTL` | 60 | WebSocket 入场票有效期，详见 ops-terminal.md |

## 升级行为

- `sys_user.ops_write` 由 `migrate_schema` 幂等补列：列不存在时
  `ADD COLUMN ops_write TINYINT(1) NOT NULL DEFAULT 0`，同一次启动把存量用户全部
  回填为 1（grandfather：旧模型下他们本来就什么都能做，保持现状）；新账号由
  `users.py` 显式赋值，默认 0。

## 测试

- `backend/tests/test_auth_users_api.py`（真 app + 文件 SQLite，不覆盖鉴权依赖）：
  验证码 PNG 与不透明令牌；登录成功路径与 `roles`/`permissions` 从两个布尔位推导
  （含管理员天然 `ops:write`）；三种失败同形同码；验证码先于密码校验且错误不计数；
  5 次失败后锁定且锁校验排在验证码前；大小写/空格变体共用一个计数键；成功登录清零
  且是真清零；无 token/伪造/过期 token、Bearer 大小写、WS 票打 REST 全被拒；
  禁用下次请求 401、恢复后 200；改密验旧拒重复且不踢旧 token；非管理员访问
  `/users` 整组 403；建号查重、短用户名 422、昵称兜底、列表绝不带 `password_hash`；
  更新只写传入字段；不能给自己降级/禁用；重置密码生效。
- `backend/tests/test_login_guard.py`：账号维/IP 维锁定与作用域、喷洒锁整 IP、
  成功清零、窗口过期重置、锁定到期自动解锁；验证码往返/错答案/篡改/过期；
  `client_ip` 的 XFF 第一跳、回落对端地址、64 字符截断。
