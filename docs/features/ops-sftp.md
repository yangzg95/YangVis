# SFTP 文件面板（远程文件浏览器）

运维「服务器」页终端旁的文件面板。在服务器台账与同一条 SSH 之上开 SFTP 子通道，提供
目录浏览、拖拽上传、下载、新建文件夹、删除，以及纯服务端的复制/剪切/粘贴。它与终端那条
持久 WebSocket 完全独立：每个 REST 请求各自 `connect()` 一条 SSH、做完就关，终端断线、
顶号都不影响文件面板。面板不做路径沙箱——账号能到的地方和自己 ssh 上去完全一样。

## 用户可见行为

- 进面板默认落在登录用户的 home；面包屑逐段可点，工具栏可上一级/刷新；双击目录进入，
  双击文件即下载。列表按「目录在前、名字小写排序」，显示大小与修改时间
- 条目超过 **2000** 条只回前 2000 条，列表底部明示「目录过大，仅显示前 2000 条」
- 上传三个入口：工具栏按钮、右键菜单「上传」、把文件**拖进面板**（整面遮罩提示目标目录）；
  多文件串行传，并行只会互抢带宽；拖入文件夹会被拒绝并提示先打包
- 单文件上限 **500MB**：上限随列目录接口下发，选完文件的那一刻就在本地拦下，不发请求；
  上传/下载都是「传输」标签页里的后台任务，进度、速率（指数平滑）、预估剩余、可取消，
  面板底部有一行活动任务摘要，点一下跳到传输页
- 上传到 100% 后状态显示「服务器写入中」：这段是网关/后端落盘的尾巴，浏览器量不出来；
  5 秒没有新进度会提示「已 Ns 无进展」（后端 60 秒停顿看门狗是真正的止损线）
- 新建文件夹走弹窗；删除有二次确认，**删目录是递归删除整棵子树**，确认文案里写明
- 右键复制/剪切/粘贴：搬运全程在服务器内完成，字节不经过浏览器；同名不覆盖，自动改名
  为 `xxx_副本.ext`、`xxx_副本2.ext`；剪切中的条目减淡虚线下划线；禁止把目录粘进它自己里面
- 只读账号可以浏览和下载，上传/新建/删除/粘贴在前端全部置灰，后端同样 403
- 错误在面板自己的错误区呈现并给「重试」，不弹全局 toast；网络层的 SFTP 异常都翻译成
  「路径不存在 / 权限不足 / 目标不是目录」这类中文句子
- 终端右键「在文件浏览器打开」经 OSC 697 问出当前目录后调面板 `openPath()` 跳过去，
  协作细节见 ops-terminal.md

## 关键取舍

### 每请求一条短 SSH，不搭终端的会话车

终端会话是按 `(user, server)` 复用的长命对象，文件操作是零星的短动作，硬复用会让面板
被终端断线/顶号牵连，还要处理「终端没开过却要传文件」。所以 `ops_files.py` 每个请求
单独 `server_ops.connect()`（含 TOFU 指纹校验与 keepalive），`finally` 里 `conn.close()`；
每次文件操作再在这条连接上短开一条 SFTP 会话（`async with await open_session(conn)`）。

### 打开 SFTP 会话要有自己的超时

SSH 建连有 `OPS_SSH_TIMEOUT` 兜底，但 `start_sftp_client()` 这一步以前没人管：对端
`MaxSessions` 打满或 sshd 卡住时，新通道请求被静默丢弃，表现为点一下无限转圈。
`open_session()` 用停顿看门狗套住这一步，超过 `OPS_SFTP_OP_TIMEOUT`（30s）报
「服务器无响应：打开 SFTP 会话超过 30 秒，请稍后重试」。

### 上传是 multipart 流式、边读边计数

上传接口收的是标准 `multipart/form-data`（字段名 `file`，目录在 query `path`），
但 500MB 不是内存上限：Starlette 对超过 1MB 的 multipart 自动 spool 到临时盘，
`_iter_body_chunks()` 按 256KB 块读 spool、累加字节数，**超限立刻抛 `_UploadTooLarge`
断流**，远端半截文件由 `upload_stream()` 的 `finally` 删掉。单请求内存上界约为
块大小 × 窗口并发，默认 256KB × 8 = 2MB。另外入口还先用 `Content-Length` 提前拒绝
（含表单开销余量，见 `uploads.content_length_exceeds`），超限与客户端取消都不记审计。

### 看门狗掐「不动」，不掐「慢」

`stall_guard()` 只对「连续 N 秒没有新字节进出」敏感（`OPS_SFTP_STALL_TIMEOUT`，60s）：
大文件跑几十分钟只要一直在动就不碰，对端被 OOM、通道半死这种真 wedge 才掐，否则前端
永远挂着「传输中」的僵尸任务。没有直接用 `asyncio.wait_for`：3.12 起被等任务若吞掉
CancelledError 并「正常完成」，`wait_for` 会把结果正常返回而不报超时；这里显式
wait → cancel → 回收，超时一定抛错、在途任务一定收干净。读侧（窗口回收）、写侧
（窗口回收与收尾）、上传 body 读取三处都套了同一条狗。

### 窗口流水线与 256KB 天花板

下载 `iter_file_chunks()` 在 stat 拿到 size 时按递增 offset 保持最多 8 个在途读任务，
按序 yield，省掉高延迟链路上每块一个 RTT 的空等；SFTP 服务端合法短读，`_read_block()`
循环补足；读到空块说明文件传输途中被截断，提前结束（响应变短，浏览器按下载失败处理）。
上传方向为每个到达的块分配 offset，窗口内并发 `remote.write`。块大小钉死 256KB：
OpenSSH 收到超过 256KB 的单条 SFTP 消息会直接断连；写打开时不传大 block_size，靠
asyncssh 按服务端 `write_len` 自动再切分兜底。

### 上限跟着列表接口下发前置预检

`SftpListResult.max_upload_bytes` 把后端配置原样带给前端（它是页面第一个请求），
transfers store `noteUploadLimit()` 记住，`startUpload()` 在发请求前先本地比对
`file.size`——传完一整轮再收一张超限错误是最浪费时间的失败。这也是三处上限必须联动
的原因之一（见下文「与反代的配合」）。

### 下载错误的两种形态

下载是全项目唯一不套统一信封的端点。第一个字节产出前出错（连不上、路径不存在、是
目录），返回 HTTP 200 + 信封形状 JSON，前端按 `application/json` 识别；响应已经开始
吐字节后出错（基本只有看门狗），错误没法再变成 JSON，只能就地收尾，浏览器按「下载被
截断」处理。文件名走 RFC 5987：`filename*=UTF-8''…` 处理中文，另给 ASCII 兜底。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/ops/sftp.py` | 文件操作层：`open_session`/`list_dir`/`make_dir`/`remove`、`stall_guard`、`iter_file_chunks`、`upload_stream`、`paste`/`unique_target`、`map_sftp_error`、`normalize_path` |
| `backend/app/routers/ops_files.py` | `/ops/servers/{id}/files/*` 路由：鉴权与 `ops_write` 闸门、每请求一连接、流式计数、StreamingResponse、审计（`verdict="sftp"`） |
| `backend/app/services/ops/server.py` `connect()` | SSH 建连、TOFU 指纹、`OPS_SSH_TIMEOUT` 与 keepalive；SFTP 通道由 sftp 层在这条连接上开 |
| `backend/app/models/schemas.py` | `SftpEntry`、`SftpListResult`（含 `max_upload_bytes`）、`SftpMkdirRequest`、`SftpPasteRequest`、`SftpPasteResult` |
| `backend/app/services/uploads.py` | `content_length_exceeds()`、`oversize_message()`，与知识库上传共用的超限口径 |
| `frontend/src/components/ServerFilePanel.vue` | 列表/面包屑/工具栏/右键菜单、拖拽（深度计数防 enter/leave 抖动）、新建/删除/粘贴、暴露 `openPath()` |
| `frontend/src/components/TransferPanel.vue` | 「传输」标签页：进度条、速率与剩余、不确定态呼吸条、取消、清除已完成 |
| `frontend/src/stores/transfers.ts` | 全局传输任务单例：axios 上传进度、下载进度、AbortController 取消、上限预检、停滞计时 |
| `frontend/src/api/ops.ts` | `opsApi.sftp*` 一组调用与 `sftpDownloadUrl()`；传输类请求 `timeout: 0` |
| `frontend/src/utils/download.ts` | `streamToDisk`（File System Access API 边下边存盘）、`saveBlobResponse`（blob 兜底）、`pickSaveTarget` |

接口（前缀均为 `/api/ops/servers/{server_id}/files`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/list?path=` | 列目录；path 空/`~` → home；回传解析后 `path`、`items`、`truncated`、`max_upload_bytes` |
| GET | `/download?path=` | `StreamingResponse`（octet-stream + RFC 5987 文件名 + Content-Length）；错误为 200 JSON 信封 |
| POST | `/upload?path=` | multipart 单文件，流式计数；成功 `message="uploaded"`，超限/失败走 `code=-1` 业务拒绝 |
| POST | `/mkdir` | body `{path}`，新建文件夹 |
| DELETE | `/?path=` | 删文件或递归删目录 |
| POST | `/copy`、`/move` | body `{src, dst_dir}`，服务端搬运；返回实际落地 `path` 与 `renamed` |

路径规整规则（`normalize_path`）：空串与 `~` 回 home、`~/` 展开，其余原样透传，
**故意不做 normpath**——手输 `/var/../etc` 交给 SFTP 服务端解析，和终端行为一致。

### 上传数据流

```
File → FormData → axios(onUploadProgress) → [nginx 不缓冲请求体]
  → _iter_body_chunks：读 256KB 块 + 停顿看门狗 + 累计字节（超 500MB 即抛）
  → upload_stream：open(wb)，每块按到达顺序分配 offset，窗口 8 个并发 write
  → 成功记审计（含字节数）；失败/取消：回收在途写任务 + remove 半截文件
```

文件名只取 basename（防 multipart 带客户端本地路径），`.`/`..`/空名拒绝；覆盖语义为
直接 `"wb"` 截断重写。

### 下载数据流

```
connect → open_session → stat（拿 size；顺便拒绝目录）
  → StreamingResponse：open(rb) → iter_file_chunks（8 路窗口预读、短读补足、钳在 size 内）
  → 浏览器：支持 File System Access 时 streamToDisk 边下边落盘（先弹保存框），
    否则 axios blob 整收后 saveBlobResponse
连接与 sftp 客户端交给流生成器持有，在生成器 finally 里 exit/close。
```

## 与反代的配合

500MB 上限由后端配置、nginx `client_max_body_size`（略大，如 512m）、列表下发的预检
三处共同决定，改一处必须同步另外两处；文件通道必须关请求体/响应缓冲
（`proxy_request_buffering off` / `proxy_buffering off`），否则进度条是假的，
`proxy_read/send_timeout` 放宽到 3600s（掐断流交给后端 60s 看门狗，不掐慢传输）；
另需一条 `location = /api/ops/servers` 精确匹配，防止带斜杠 location 的 301 把台账
接口带偏。完整配置、实测数据与踩坑说明见 README「上传上限与反代配合」一节，此处不重复。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OPS_SFTP_MAX_UPLOAD_BYTES` | 524288000（500MB） | 单文件字节上限，边读边计数；改了同步 nginx 与网关口径 |
| `OPS_SFTP_LIST_LIMIT` | 2000 | 单次列目录条目上限，超出截断并置 `truncated` |
| `OPS_SFTP_CHUNK_SIZE` | 262144（256KB） | 传输块大小；不得调大，OpenSSH 单条 SFTP 消息上限 256KB |
| `OPS_SFTP_PIPELINE_REQUESTS` | 8 | 读写窗口内并发请求数；与块大小决定单传输内存上界（约 2MB） |
| `OPS_SFTP_OP_TIMEOUT` | 30.0 | 打开 SFTP 子通道的等待上限（秒） |
| `OPS_SFTP_STALL_TIMEOUT` | 60.0 | 传输停顿看门狗：连续多少秒无新字节进出即判死 |

## 测试

- `backend/tests/test_ops_sftp_stream.py`：用内存 fake（FakeRemote/FakeSftp/FakeConn，
  可注入乱序完成、短读、卡死）覆盖传输层——窗口预读乱序完成仍按序产出、短读循环补足、
  传输途中文件被截断干净收尾、某块出错取消全部在途任务并透传、size 未知退化为顺序读、
  空文件零写出；上传 offset 在乱序下仍正确、失败翻译成 `SftpError` 并删除半截文件、
  迭代器自身异常（超限）原样透传也清半截；body 计数恰为上限放行、超限即抛；预读/顺序读/
  写窗口/body 读取四条路径的停顿看门狗；`open_session` 超时报可读错误；`stall_guard`
  对吞掉 CancelledError 的任务仍保证超时抛出
- `backend/tests/test_ops_sftp_paste.py`：副本命名（后缀留在最后一段、`.tar.gz`/
  隐藏文件口径）、原名优先与 `_副本`/`_副本2` 改名、空目标目录按 home 处理、禁止粘进
  自身、根目录不能粘贴、剪切回原目录直接拒绝、复制递归并保留属性、剪切优先 rename、
  跨文件系统 rename 被拒后回退 copy+remove、回退时源目录树递归删除
- 路由层（multipart 解析、StreamingResponse、`ops_write` 403、审计落库、Content-Length
  预拒）目前没有专门的 HTTP 级测试文件，覆盖止于 service 层
