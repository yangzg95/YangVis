# Web 终端（xterm.js + 持久 PTY 会话）

运维「服务器」页点「终端」进入的全屏黑窗。一条真 PTY：xterm.js 敲下的每个字节原样
转发给 SSH，反过来也一样，不做任何命令过滤——和用户自己 ssh 上去没有区别。

## 用户可见行为

- 全屏终端：流式输出、内容搜索（`Ctrl+F`）、URL 可点、深/浅主题切换（偏好记 localStorage）
- 剪贴板三套惯例：右键菜单、`Ctrl+Shift+C/V`、`Ctrl/Shift+Insert`、选中即复制（PuTTY 口径）
- 右键「在文件浏览器打开」：向 PTY 问出当前目录（OSC 697），展开左侧 SFTP 面板并跳到该目录
- 网络断开自动重连（指数退避 1s→2s→4s，封顶 10s，最多 5 次），顶栏可手动重连
- **断线不丢现场**：浏览器断开后远端会话在服务端保留 30 分钟，重连回放近期画面并接回原 shell
- 同一账号在第二个标签页打开同一台机器会**顶号**，旧页收到 4001
- 手机端不提供终端（整个组件不挂载，WS 根本不建立）

## 为什么会话要脱离 WebSocket

旧实现里 WS 与 SSH 同生命周期：浏览器一断（关页面、网络抖动、空闲被 NAT 掐线），
端点 `finally` 里立刻 `conn.close()`，远端 shell 死亡，重连只能拿到全新会话，现场全丢。
同时 SSH 没有心跳，空闲久了被中间设备/sshd 掐断（典型表现 `WinError 10054`）。

现在的拆法：**WS 只是会话的一块「屏幕」**。SSH 连接、PTY 进程、输出缓冲、命令行
审计跟踪器都活在会话对象里，按 `(user_id, server_id)` 索引放在模块级内存表；
浏览器断开只摘屏幕，会话在 TTL 内继续存活，重连即续接——思路与 tmux attach 相同，
但不依赖远端装 tmux。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/ops/terminal.py` | `TerminalSession` / `TerminalRegistry`、回放缓冲、泵/写者/收尸钟、命令行审计跟踪器 |
| `backend/app/routers/ops.py` 的 `/terminal` WS | 鉴权、新会话工厂（SSH+PTY）、浏览器→PTY 读循环；不管反方向 |
| `backend/app/services/ops/server.py` `connect()` | SSH 建连、TOFU 指纹、keepalive 参数 |
| `frontend/src/components/ServerTerminal.vue` | xterm 实例、WS 生命周期、控制帧处理、剪贴板/搜索/主题 |
| `frontend/src/views/OpsServerTerminal.vue` | 终端页外壳、自动重连调度、与文件面板/AI 面板的协作 |

### 会话生命周期

```
新 WS ──► registry.attach_or_create(key=(user,server))
            │  per-key asyncio.Lock：防止断线瞬连并发建出两条 SSH
            ├─ 有存活会话：attach(ws, resumed=True)  不查库、不重连 SSH
            └─ 没有：跑工厂（查台账→SSH→create_process）→ start() → attach(resumed=False)

detach(ws) ──► 摘屏幕 + 取消写者 + 拉起收尸钟（TTL）
重连/顶号   ──► 取消旧写者 → 顶掉旧屏幕(4001) → 拍快照 → 挂新队列 → hello+回放+live
PTY EOF/异常 ─► _destroy()：摘表、关 SSH、给当前屏幕排 ended 帧 + 4002
TTL 到期    ──► _destroy(notify=False)
```

### 输出通道：每块屏幕一个队列 + 单写者

泵（SSH→缓冲/队列）与回放共享同一会话，顺序必须严格，否则重连时画面会重复或穿插：

1. `attach` 里**同步**拍快照（`list(self._chunks)`），再挂队列——两步之间不能有 `await`，
   否则泵会在期间把同一块输出同时放进快照和队列，屏幕重一遍。
2. `hello` 之后逐块发快照，最后发 `live`；这期间到达的实时输出全部在队列里等着。
3. 写者任务是队列唯一消费者，回放结束后接管发送，实时帧天然排在 `live` 之后。
4. 新建会话也走回放：shell 的 motd/首批提示符可能抢在 attach 之前进缓冲。

### 服务端 → 浏览器的控制帧

裸 PTY 输出以 `{` 开头并不稀奇，控制帧用 `\x1e`（记录分隔符，真实输出里几乎不出现）
前缀消歧：

| 帧 | 时机 |
| --- | --- |
| `{"type":"hello","resume":bool}` | 握手后第一帧；前端据此 `term.reset()`，随后是回放 |
| `{"type":"live"}` | 回放结束、进入实时 |
| `{"type":"ended","reason":...}` | 远端 shell 退出/SSH 断，随后 WS 以 4002 关闭 |
| `{"type":"takenover"}` | 被新标签页顶号，随后 4001 关闭 |

浏览器 → 服务端仍是两类文本帧：裸字符串=键盘输入；`{"type":"resize","cols","rows"}`=窗口变化。

WS 关闭码：`4001` 顶号、`4002` 会话终结、`1008` 鉴权/查无此机、前端自定义 `4000`
表示本端主动断开（切服务器/卸载，不触发自动重连）。

### 回放缓冲

- `deque` 按 4096 读取块存放，上限约 20 万字符（`REPLAY_MAX_CHARS`），整块淘汰
- 只够重建近期画面；更早的回滚找不回来
- vim/top 等全屏程序重连时靠前端连上后那次 resize 触发重绘；若切换备用缓冲的转义序列
  已被截在缓冲之外，画面顶部可能有少量残片——这是无远端协同方案的务实上限

### 命令行审计

真 PTY 不做过滤，但「敲过什么」要留痕。`CommandLineTracker` 从原始输入流里尽力重建
命令行（可打印字符入缓冲、退格/Ctrl+U/Ctrl+W 处理、回车提交；翻历史等复杂编辑不保证）。
硬规则：输出侧出现密码提示（sudo、`mysql -p` 等，正则见 `_PASSWORD_PROMPT`）后，
下一行输入不记——把密码写进审计库比漏记一条命令糟得多。前端右键 cwd 探测的
`printf OSC 697` 也不落审计（`is_cwd_probe`）。记录以 `verdict="manual"` 写入审计表。

### SSH 保活

`server.py` 的建连参数新增 `keepalive_interval=25s`、`keepalive_count_max=3`：
定期发心跳防空闲掐线，连续 3 次无响应才判死。所有 SSH 通道（不只终端）都受益。

## 前端要点

- 重连**不销毁 xterm、不换 `:key`**：父页面调用组件暴露的 `reconnect()`，同实例开新 WS；
  连接前不清屏（重连失败时旧画面必须留在屏幕上），收到 `hello` 才 reset
- `generation` 自增计数器作废过期的异步拿票/回调（切走服务器后旧连接的消息不能再写屏）
- 手敲半行内容跟踪在 `pendingLine`：AI 补提示符、右键探测 cwd 时要避开，防误提交
- 续接成功后父页面把断线期间暂存的「在终端打开」命令（`pendingInput`）补送

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OPS_TERMINAL_TTL` | 1800 | 最后一个浏览器断开后会话存活秒数；0=断线即销毁 |
| `OPS_SSH_KEEPALIVE` | 25 | SSH keepalive 间隔秒数；0 关闭 |
| `OPS_WS_TICKET_TTL` | 60 | WS 一次性入场票有效期，token 不进 URL |

## 已知边界

- 会话表在单 worker 内存里（当前 uvicorn 单进程部署）；**后端重启全部丢失**，
  多 worker 部署会导致重连落不到同一进程——水平扩展前需要外置（Redis 流或每用户粘性路由）
- 每个用户对每台服务器最多一个会话、一块屏幕（后登顶先登）
- PTY 是 `ops_write` 权限的第一道闸门：只读账号在前端就不挂载终端，后端握手同样以 1008 拒绝
