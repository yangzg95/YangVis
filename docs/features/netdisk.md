# 百度网盘（xpan 开放平台 · OAuth oob 绑定）

智能办公各模块的「备份到我自己的网盘」能力。每个用户在设置页 OAuth 授权绑定**本人的**
百度网盘（一人一绑），文件只能写进该应用在网盘里的专属目录 `/apps/<APP_NAME>/`。
不引入百度官方 SDK（2022 年 openapi-generator 产物，依赖老旧），全程用项目已有的
httpx 直连 xpan/openapi 端点。

授权走 **oob（Out-Of-Band）授权码模式**，自托管实例不需要公网回调地址：

```
1. 前端 GET  /api/office/netdisk/auth-url      后端拼出 baidu oauth 授权页地址
2. 浏览器新窗口打开授权页（redirect_uri=oob），用户登录并同意授权
3. 百度不跳转回调，而是在页面上直接显示一串授权码——用户手动复制
4. 前端 POST /api/office/netdisk/bind {code}   后端用码换 token、拉 uinfo，落库完成绑定
```

## 用户可见行为

- **设置页「百度网盘」Tab**（`Settings.vue`）：状态第一次切到该 Tab 时才懒加载
  - 后端未配置：整块显示「未启用」及所需环境变量名，不出现任何绑定按钮
  - 已配置未绑定：「绑定百度网盘」按钮 → 弹窗内三步引导（开授权页 → 复制授权码 → 粘贴完成）
  - 已绑定：蓝色 Tag 显示百度账号名，可「重新绑定」（同一行 upsert，等于换新令牌）或「解绑」
- **简历模块**：上传简历原件时若已绑定，自动备份一份到网盘 `resumes/<简历id>_<文件名>`；
  列表行出现云图标，可经后端代理下载原件；删除简历时联动删除网盘副本（失败不阻塞本地删除）
- **报告页**（简历分析/对比/面试工具包三类报告）：绑定后出现「存到网盘」，markdown 全文
  存到 `reports/<标题>.md`，成功提示网盘完整路径
- **对话工具面**：模型可调用「保存文件到百度网盘」工具，把对话产出的文本存到 `chat/` 子目录
- **运维数据库备份**：已绑定时可把 MySQL 备份包传到 `backups/` 子目录（见 ops-terminal 同域文档）
- 解绑**只删本地凭据**，网盘里已同步的文件原样保留，弹窗与 toast 都明确告知这一点

## 关键取舍

### 为什么 oob 不需要公网回调

标准授权码模式要把 `redirect_uri` 指到一个百度服务器能访问到的回调地址，自托管工作台
往往只在内网/本机。oob 模式下 `redirect_uri=oob`，百度授权完成后不重定向，而是把
授权码直接显示在页面上交给用户人工搬运。代价是多一次复制粘贴，换来的是零公网暴露面、
零回调路由（后端没有 callback 端点），授权码一次性、用完即废。`code` 入参限 1–128 字
符，绑定时还会 `strip()`——从页面复制回来常带首尾空白。

### 凭据怎么存

- access_token / refresh_token 在 `netdisk_account` 表里以 **Fernet 密文**存放
  （`LargeBinary(2048)` 字段 `*_enc`），与模型 API key 同一套 `app.crypto`（AES-128-CBC
  + HMAC，密钥来自 `ENCRYPTION_KEY`，不入库）；任何 API 响应只回 `baidu_name`/`expires_at`，
  token 本体永不出后端
- xpan 接口设计要求 access_token 走 **query 参数**，httpx INFO 日志会打印完整 URL，
  因此在 httpx logger 上挂了 `_TokenRedactingFilter`，把 `access_token=...` 正则打成
  `***`；HTTP 异常文本可能含 URL，日志只记异常类型
- 下载地址 dlink 里拼了 access_token，`open_download()` 只供服务端**代理下载**
  （简历原件下载即如此），绝不 302 下发浏览器——否则 token 会进浏览器历史与中间日志

### 为什么配置缺失就整体隐藏

门只有一道，在服务层 `NetdiskService._configured`：`BAIDU_NETDISK_APP_KEY` /
`SECRET_KEY` / `APP_NAME` 三项任一为空即未启用。`/status` 接口对此不报错，正常返回
`{configured: false}`，前端据 `v-if="netdisk.configured"` 隐藏全部入口（设置 Tab 显示
未启用提示，报告页按钮经 `configured && bound` 判定不出现，对话工具则返回一句
「功能尚未启用」的工具结果而不是抛错）。强操作（auth-url、上传等）另有
`_require_configured()` 抛 `4201` 兜底，前端绕过了也进不去。

### token 刷新时机

xpan access_token 有效期约 30 天。每次取 token（`_access_token()`，即任何上传/下载/
删除调用前）比较 `expires_at <= now + 5 分钟`（`_REFRESH_MARGIN`），临期就先用
refresh_token 调 `oauth/2.0/token` 换新并落库，避免「请求做到一半过期」。刷新失败提示
「网盘授权刷新失败，请重新绑定」。`expires_at` 为 NULL 的历史行不视为过期，不会每调必刷。
到期时刻按响应里的 `expires_in` 从当前起算，统一存 naive UTC（MySQL DATETIME 不带时区）。

### owner 隔离与解绑

`netdisk_account.owner_id` 唯一索引，`NetdiskService(db, user_id)` 的所有查询都带
`owner_id` 过滤，A 永远碰不到 B 的网盘凭据；一人一行，重新绑定是同 row upsert。
`unbind()` 删除整行（密文随之消失），未绑定再解绑抛 `4202`；不动远端文件。

### 路径只能落在应用目录

`_remote_path()` 把相对路径逐段清洗：`/`、`\`、控制字符替换为 `_`，`.`/`..` 段整体
替换为 `_`（不做路径抵消），空段跳过；全空则报「目标路径为空」。结果恒为
`/apps/<APP_NAME>/...`，调用方传什么都逃不出应用目录。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/netdisk.py` | `NetdiskService`：配置门、oob 授权链接、绑定/解绑、token 刷新、三步上传、dlink 下载、best-effort 删除、errno 映射、日志打码 filter |
| `backend/app/routers/netdisk.py` | `/api/office/netdisk/*`：status、auth-url、bind、save-text、unbind |
| `backend/app/models/entities.py` `NetdiskAccount` | `netdisk_account` 表：owner 唯一、双 Fernet 密文、baidu_uid/name、expires_at |
| `backend/app/models/schemas.py` | `NetdiskStatus` / `NetdiskAuthUrl` / `NetdiskBindRequest` / `NetdiskSaveTextRequest|Result` |
| `backend/app/routers/resume.py` | 简历上传后 `resumes/` 备份与 `mark_netdisk` 回填；`/resumes/{id}/download` 经 dlink 代理流式下载（300s 超时）；删除联动 `delete_file` |
| `backend/app/services/resume.py` | `netdisk_fs_id`/`netdisk_path` 字段回填，列表项 `has_netdisk` 标记 |
| `backend/app/routers/ops.py` | 数据库备份传 `backups/<db_id>_<文件名>`，未配置时降级为仅本地下载 |
| `backend/app/services/tools/builtins/netdisk_tools.py` | 对话本地工具 `save_text_to_netdisk`（异步、带 ctx、固定 `chat/` 前缀、1MB 上限） |
| `frontend/src/api/netdisk.ts` | 绑定域 API 封装（bind/save-text 超时放宽到 60s） |
| `frontend/src/views/Settings.vue` | 「百度网盘」Tab、懒加载状态、绑定三步弹窗、重新绑定/解绑 |
| `frontend/src/views/Resume.vue` | 云图标、原件代理下载、删除确认文案随 `has_netdisk` 变化 |
| `frontend/src/views/ReportView.vue` | 「存到网盘」按钮（仅 configured && bound 出现） |

### xpan 三步上传

`upload_file(rel_path, raw)` 按 4MB 切片（xpan 片长上限），每片一个 MD5：
`file?method=precreate`（带 block_list 预创建拿 uploadid）→ 逐片 POST
`pcs/superfile2`（multipart，partseq 从 0 起）→ `file?method=create`（rtype=3），
成功返回 `(fs_id, 完整路径)`；0 字节也兜一片空片走完全程。任一步 errno 非 0 立即中止，
precreate 无 uploadid / create 无 fs_id 都显式报错。

### 错误处理

业务码统一在 `app/errors.py`：`4201 NETDISK_NOT_CONFIGURED`、`4202 NETDISK_NOT_BOUND`、
`4203 NETDISK_API_ERROR`。OAuth 端点是 `error`/`error_description` 风格，xpan 业务接口
是 `errno` 风格，两套分别翻译；常见 errno（-6/42001 授权失效、-7 文件不存在、2 参数错误、
31034 限频）给中文文案且不把数字甩给用户，未知 errno 保留编号与 errmsg 便于排查；
响应非 JSON / 非 dict、HTTP 传输异常都落成可读的 `BusinessError`。`delete_file` 是
best-effort：任何异常（含未绑定）只记 warning，绝不拖垮调用方（简历删除、网盘只是备份
通道的上传失败同理，均在调用侧吞掉并记日志）。

## 配置

`backend/app/config.py`（后端 `.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BAIDU_NETDISK_APP_KEY` | 空 | xpan 开放平台应用的 AppKey；空=功能整体隐藏 |
| `BAIDU_NETDISK_SECRET_KEY` | 空 | SecretKey，仅换 token/刷新时出网使用，不进授权链接 |
| `BAIDU_NETDISK_APP_NAME` | 空 | 控制台登记的应用目录名，远端路径固定前缀 `/apps/<它>/` |
| `BAIDU_NETDISK_REDIRECT_URI` | `oob` | oob 授权码模式；授权码由用户手动复制，无需公网回调 |
| `ENCRYPTION_KEY` | — | Fernet 主密钥（全局共用），双 token 密文靠它加解密，不入库 |

授权 scope 固定 `basic,netdisk`；OAuth 主机 `openapi.baidu.com`、业务主机
`pan.baidu.com`、分片主机 `d.pcs.baidu.com`，HTTP 超时 60s。

## 测试

- `backend/tests/test_netdisk.py`（服务层，httpx `MockTransport` + 文件型 SQLite，
  异步入口统一 `asyncio.run`，不连百度）：
  - 配置门：三项全空时 status/auth-url/upload 全部被 `4201` 挡住；configured 与 bound 两态分离
  - 绑定：授权码 strip、密文落库、`expires_at` 按 expires_in 起算、换 token 后立刻调 uinfo
    验活并拿账号名；重复绑定只留一行；OAuth error 文案翻译；uinfo 失败整体作废不留半行
  - 解绑：未绑定抛 `4202`；解绑删行且表中不剩任何凭据
  - 刷新：剩余 <5 分钟先 refresh 再用新 token；新 token 不刷；expires_at=NULL 不刷；
    refresh 失败提示重新绑定；未绑定的文件操作抛 `4202`
  - 路径：`..`/反斜杠/控制字符/前导斜杠等 7 组参数化用例钉死逃不出应用目录，空白路径报错
  - 三步上传：precreate→slice→create 顺序与表单字段、4MB 切片多 partseq 与每片 MD5、
    空文件兜一片、首个 errno 即中止、缺 uploadid/fs_id 显式失败
  - 下载：dlink 拼 token 与 `User-Agent: pan.baidu.com` 钉死、`?`/`&` 两种拼接、
    无 dlink 提示「文件可能已被删除」
  - 错误与日志：常见 errno 中文映射、未知 errno 保留编号、非 JSON/非 dict 响应、
    传输异常日志不含 token、删除 best-effort（含未绑定）、`_TokenRedactingFilter` 打码
- `backend/tests/test_tool_netdisk.py`（对话工具模型面）：注册形态（异步、带 ctx、
  参数仅 filename/content）、拒绝路径型/空文件名、空内容与 1MB 上限、未配置/未绑定
  返回提示而非抛错、成功固定落 `chat/` 且只吃文件名末段、`BusinessError` 转工具结果文案
