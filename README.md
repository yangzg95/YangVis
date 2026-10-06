# YangVis（杨维斯）

一个自托管的全能型 AI 工作台：统一控制台下集成智能对话、知识库问答（RAG）、智能运维与智能办公能力。
模型层不绑定任何服务商——只要兼容 OpenAI 协议（火山方舟、DeepSeek、OpenAI、vLLM 等），填入 `base_url + model + api_key` 即可使用。

## 功能模块

### 🏠 首页工作台
- 登录后默认落在首页：问候区 + 功能模块卡片（各模块在用户名下的数量：会话 / 文档 / 简历 / 幻灯片 / 服务器 / 连接 / 智能体）
- 最近对话列表，点一条直接进对话页并打开该会话（`/chat?conv=<id>` 深链）
- 对话模型未通过连通性测试时在首页先提示「去配置」，而不是点进各功能再撞 gate
- 管理员多一张 AI 网关卡片，主数字是今日调用数（今日 tokens 跟在后面），与网关自身概览的累计口径区分开
- 数量口径统一来自 `GET /api/dashboard/summary`（按 `owner_id` 隔离；会话数与对话页一致，不含运维页内嵌的问答会话）

### 💬 智能对话
- 多智能体体系：内置 + 自定义智能体，各自独立的 system prompt 与模型绑定，可按需对对话页开放/隐藏
- SSE 流式输出、会话重命名/搜索/删除、发送失败重试
- 绘图助手：Mermaid 图表生成与在线编辑
- 运维工具挂载：对话中可直接调用只读运维工具（巡检服务器、查数据库），危险写操作走「两阶段确认」卡片，超时自动按未批准处理

### 📚 知识库（RAG）
- 按用户隔离：文档、分块、向量全部带 `owner_id`，Qdrant 按用户建 collection，互不串数据
- 文档上传 → 切片 → 向量化 → 检索 → 带引用溯源的回答，全链路自建（LangChain + Qdrant）
- 对话模型与向量模型独立配置；未配向量模型时 RAG 明确阻断并引导
- 检索测试、低分结果阈值过滤、批量删除、单篇/全量索引重建

### 🔁 工作流编排
- 菜单路径「AI 构建 → 工作流」：一条工作流就是把「一次功能调用」拆成几步写模型的流水线——输入变量、每步的智能体与 Prompt 模板、输出格式都在页面上改，不用发版
- 步骤在左边的列表里改，右边贴一张只读结构图：**一列就是一个并行组**，列间顺序执行、列内几步同时跑。图上的线不用手连——从每步模板里的 `{{变量}}` 反推出来，只画「前面的组 → 这一步」这种后端认的引用
- 两边双向跟随：点图上的节点，列表滚到那一步并描一圈光环；在列表里滚动或编辑哪张卡片，图上对应的节点就亮哪一个
- 并行关系还是在列表里改：「加一步」自动开新组，「加一个并行步骤」并入上一组，上下调序按钮改先后，要把两组并成一个就在「高级」里把分组名改成同名
- 分组口径全站只有这一条：**相邻且分组名相同**才算一组（后端 `_group_order` 与结构图同一套算法，画出来几列就是跑起来几段）；同名被别的组隔开那种写法保存时就拦下
- 窄屏（`≤768px`）不画结构图，只留列表——列表永远是全集的兜底，改动的地方也只有一处数据
- 步骤标识、分组名、产出变量、JSON 契约与「可选」都收在「高级」里；模板里敲 `{{` 弹补全，引用芯片只列这一步真能引用的那些（声明的输入 + 前面并行组的产物，JSON 步骤还能按 `{{产物.字段}}` 取）
- 内置流程（简历分析那几条）点进去就是只读；新建时可以选一条内置的起步复制，标识得自己起——功能侧是按 `slug` 调用这条流程的
- 保存是整份替换，且不提交 `sort_order`（后端按数组顺序重新编号），界面顺序与执行顺序不会两张皮；引用了同组或后面步骤的产物会被后端拒绝并把原因弹回来
- 运行侧：列表卡片带「最近一次运行」，抽屉里查历次运行、逐步看渲染出来的 prompt 与原始输出，跑挂的那步可单步重跑，页面跟 SSE 实时进度

### 🛠️ 智能运维
- 服务器台账：SSH 连接管理、连通性测试
- Web 终端：xterm.js + WebSocket + PTY，断线自动重连、内容搜索、可点链接，手敲命令也进审计
- SFTP 文件面板：目录浏览、拖拽上传、下载、新建/删除
- 数据库工作台（MySQL）：多页签查询（Navicat 式批量执行、消息/摘要面板）、SQL 收藏、查询历史、结果网格导出（CSV/JSON/Markdown/INSERT；当前页在前端序列化，全量走服务端分批流式并受 `OPS_EXPORT_MAX_ROWS` 约束）、表结构查看（DDL / 索引 / 外键）、设计表（表单编辑 → ALTER 预览确认 → 执行）、ER 图、数据浏览的行内改值与删除记录
- Redis 控制台：scan 浏览 + key 详情；连接开启写入后可新建 key、改 string 值（保留原 TTL）、五种类型的元素增删（list 按下标删、hash 覆盖 field、stream 删条目）、EXPIRE/PERSIST、key 列表勾选批量 UNLINK
- AI 运维助手：自然语言 → 命令/SQL；只读直接执行，写操作必须人工确认后才放行
- 权限闸门：`ops_write` 权限位控制一切写操作，台账全员共用、只读用户也能查
- 全量审计：AI 执行的、终端手敲的、SQL 执行的，统一留痕可翻查

### 📄 智能办公
- 简历分析：PDF/DOCX 文本抽取 → LLM 评估 → 关键信息提取与多份对比（prompt 走智能体体系，可自行调整）
- 百度网盘：xpan 开放平台绑定（oob 授权码模式，无需公网回调），简历文件可备份至网盘应用目录

### 🔌 AI 网关
- 对外一个统一密钥（`sk-yv-*`），背后可挂多个 OpenAI 兼容的上游厂商通道，平台级共享、仅管理员可管
- 模型名映射 + 优先级故障转移：同一个对外模型名可绑多条通道，按 `priority` 升序尝试，上游不可达/限流/鉴权失败时自动换下一条（仅在首字节发出前切换）
- 两层防自环：通道 `base_url` 保存时校验（本站域名 / localhost / 回环地址一律拒绝），转发出去的请求再盖一个 `X-Yangvis-Gateway` 标记头，入站看到它就回 508 `gateway_loop_detected`——内网别名之类保存时认不出来的情况靠这层兜底，否则请求会在网关里无限递归
- 端点：`POST /v1/chat/completions`（SSE 流式与非流式）、`POST /v1/embeddings`、`GET /v1/models`，错误体沿用 OpenAI 结构，标准 SDK 改 `base_url` 即可直连
- 监控与审计：每次调用记录通道、模型、token 数、耗时/首字节耗时、客户端 IP、重试轨迹，正文按 `AI_GATEWAY_LOG_MAX_CHARS` 截断入库（可用 `AI_GATEWAY_LOG_PAYLOAD` 整体关闭）；日志写失败绝不影响调用本身
- 概览页提供调用量、成功率、token 总量、延迟与按天趋势，以及按通道/模型/密钥的分布

### 👥 账号与权限
- 自建账号体系：`sys_user` 表 + PBKDF2 密码散列 + 本地签发 JWT，不依赖任何外部认证服务
- 管理员在「账号管理」里建号、禁用、授予运维写权限；首次启动可用环境变量引导初始管理员

### ⚙️ 模型设置
- 菜单路径「系统设置 → 模型与集成 → 模型管理」，按用户保存，对话/向量两种用途各自独立配置
- 这里配的是**站内自用模型**（控制台的对话、简历、幻灯片、知识库检索都从这里取）；把本站当成 OpenAI 接口给外部系统调用是「AI 网关」的职责，两个页面互相有跳转入口
- api_key 使用 Fernet 加密落库，任何接口都不返回明文
- 一键连通性测试，配错（如把部署 ID 填成模型名）当场暴露

---

## 技术栈

**前端**：Vue 3.5 · TypeScript 5.6 · Vite 5 · Pinia 2 · Vue Router 4 · Ant Design Vue 4 · xterm.js · Mermaid · Axios

**后端**：Python 3.11 · FastAPI 0.115 · SQLAlchemy 2.0 + PyMySQL · LangChain / LangGraph · Qdrant · asyncssh（SSH/SFTP/PTY）· redis-py · PyJWT · cryptography（Fernet）· pypdf / python-docx · httpx

**存储**：MySQL 8.0（业务数据 + 审计）· Qdrant（向量）

## 项目结构

```
noetix-tool/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口、路由挂载、SPA 静态托管
│   │   ├── config.py          # pydantic-settings 配置
│   │   ├── bootstrap.py       # 启动自检（密钥强度）+ 建表 + 引导管理员
│   │   ├── security.py        # PBKDF2 / JWT
│   │   ├── crypto.py          # api_key Fernet 加解密
│   │   ├── deps.py            # require_user / require_admin 依赖
│   │   ├── routers/           # auth users agents chat knowledge settings
│   │   │                      # ops ops_files resume netdisk ai_gateway(_openai)
│   │   ├── services/          # 业务层：RAG、对话、运维安全闸门、SFTP、简历…
│   │   └── models/            # SQLAlchemy 实体 + Pydantic schema
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── src/
│       ├── views/             # Login / 对话(客服) / Knowledge / Agents
│       │                      # OpsServer(Terminal) / OpsDatabase(Chat) / Resume
│       │                      # Users / Settings / AiGateway
│       ├── components/        # 终端、SFTP 面板、确认卡片、Mermaid 卡片…
│       ├── api/ router/ stores/
├── docs/                      # 设计方案、schema.sql（DDL 对照稿）
├── Dockerfile                 # 前后端一体镜像
├── docker-compose.yml         # app + MySQL + Qdrant
└── build.sh / build.bat / dev.sh / dev.bat
```

## 快速开始（Docker）

前置：Docker 与 Docker Compose。

```bash
cp backend/.env.example .env   # compose 会从根目录 .env 读取变量
```

编辑 `.env`，至少填三项（生成方式文件里有注释）：

```env
DB_PASSWORD=...            # MySQL root 密码
AUTH_JWT_SECRET=...        # ≥32 字节，否则应用拒绝启动
ENCRYPTION_KEY=...         # Fernet key，设定后不要轮换
# 首次启动可选：引导初始管理员（建号后建议删掉）
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=...
```

```bash
docker-compose up -d --build
```

| 入口 | URL |
| --- | --- |
| 应用首页 | http://localhost:18099 |
| 健康检查 | http://localhost:18099/api/health |
| API 文档 | http://localhost:18099/api/docs （ReDoc: `/api/redoc`） |

前端构建产物由 FastAPI 直接托管，非 `/api/*` 请求回退到 `index.html`（SPA history 模式）。

## 本地开发

```bash
# 后端（Python 3.11+）
cd backend
python -m venv .venv && .venv/Scripts/activate   # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填好 AUTH_JWT_SECRET / ENCRYPTION_KEY
uvicorn app.main:app --host 0.0.0.0 --port 18099 --reload

# 前端（Node 20+）
cd frontend
npm install
npm run dev                   # http://localhost:5173，/api 与网关的 /v1 已代理到 18099
```

也可以使用根目录的 `dev.sh / dev.bat` 同时拉起两端。MySQL 与 Qdrant 可用 `docker-compose up -d mysql qdrant` 单独启动。

## 配置项

全部通过环境变量 / `.env` 注入（`backend/app/config.py`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_NAME` / `PORT` | yangvis / 18099 | 应用基础信息 |
| `DB_HOST` `DB_PORT` `DB_USER` `DB_PASSWORD` `DB_NAME` | localhost… | MySQL 连接 |
| `QDRANT_HOST` `QDRANT_PORT` `QDRANT_TIMEOUT` | localhost / 6333 | 向量库 |
| `AUTH_JWT_SECRET` | **必填** | JWT 签名密钥，<32 字节拒绝启动 |
| `AUTH_TOKEN_TTL` | 43200 | token 有效期（秒） |
| `ENCRYPTION_KEY` | **必填** | api_key 的 Fernet 密钥，轮换后旧 key 全部无法解密 |
| `BOOTSTRAP_ADMIN_USERNAME/PASSWORD` | 空 | 仅 sys_user 为空时建初始管理员 |
| `KB_MAX_UPLOAD_BYTES` `KB_EMBED_BATCH_SIZE` `KB_SCORE_THRESHOLD` | 10MB / 16 / 0.3 | 知识库入库与检索 |
| `OPS_SSH_TIMEOUT` `OPS_CMD_TIMEOUT` `OPS_AGENT_CMD_TIMEOUT` | 15 / 20 / 120 | 运维通道超时（秒） |
| `OPS_OUTPUT_LIMIT` `OPS_SQL_ROW_LIMIT` | 8192 / 200 | 回传模型的输出/行数上限 |
| `OPS_EXPORT_MAX_ROWS` | 200000 | 数据页「导出全部」的单次行数上限，超了直接拒 |
| `OPS_CONFIRM_TIMEOUT` | 180 | 写操作确认等待时长（秒） |
| `OPS_SFTP_MAX_UPLOAD_BYTES` `OPS_SFTP_LIST_LIMIT` | 500MB / 2000 | SFTP 面板的单文件大小上限与单次列目录条数 |
| `OPS_SFTP_OP_TIMEOUT` `OPS_SFTP_STALL_TIMEOUT` | 30 / 60 | 打开 SFTP 会话的等待上限、传输连续这么久没有新字节进出就判死（秒） |
| `BAIDU_NETDISK_APP_KEY/SECRET_KEY/APP_NAME` | 空 | 任一为空则网盘功能整体隐藏 |
| `MODEL_HTTP_TIMEOUT` | 30 | 调用户模型服务的出站超时 |
| `AI_GATEWAY_CONNECT_TIMEOUT` `AI_GATEWAY_READ_TIMEOUT` | 10 / 300 | 网关转发上游的连接/读取超时（秒） |
| `AI_GATEWAY_LOG_PAYLOAD` `AI_GATEWAY_LOG_MAX_CHARS` | true / 8000 | 是否把请求响应正文写进审计日志、单条正文截断字符数 |
| `AI_GATEWAY_MAX_ATTEMPTS` | 3 | 单次调用最多尝试几条上游通道 |
| `AI_GATEWAY_PUBLIC_ORIGIN` | 空 | 对外公布的站点 origin（如 `https://ai.nanwa.xyz`），概览页据此显示接入地址；留空则按打开控制台时的 origin 推导。`/v1` 由程序拼接 |

### 上传上限与反代配合

SFTP 文件面板的单文件大小上限由三处共同决定，**改一边必须同步改另一边**，否则超限的传输会以看不懂的姿势失败：

| 位置 | 作用 |
| --- | --- |
| `OPS_SFTP_MAX_UPLOAD_BYTES`（默认 500MB） | 后端真正的字节上限，边读边计数，超限即断 |
| 反代 `client_max_body_size`（略大于它，如 512m） | 网关的放行口径，multipart 信封本身还要占一点 |
| 列表接口回传的 `max_upload_bytes` | 前端选文件时本地预检，超限直接报错、不发请求 |

反代必须为这条通道关掉**请求体缓冲**，否则上传进度条是假的。另外**必须配一条精确匹配**把它上面的台账接口钉回来：`location` 名以 `/` 结尾时，nginx 会把去掉斜杠的 `/api/ops/servers`（列表与新建）当成它的「父目录」，转发之前就 301 补一个斜杠；浏览器跟到 `/api/ops/servers/` 时后端没有这条路由，只会回 SPA 的 `index.html`（HTTP 200 的 HTML），前端把这段 HTML 当列表数据用，台账页就空掉甚至崩在渲染上。

```nginx
location = /api/ops/servers {      # 精确匹配优先，也不构成目录节点，301 不再发生
    proxy_pass http://127.0.0.1:18099;
    proxy_http_version 1.1;
}

location ^~ /api/ops/servers/ {
    proxy_pass http://127.0.0.1:18099;
    proxy_http_version 1.1;
    client_max_body_size 512m;        # 略大于 OPS_SFTP_MAX_UPLOAD_BYTES
    proxy_request_buffering off;      # 关键：不关就没有真实上传进度
    proxy_buffering off;              # 下载同理，边读边发
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

默认 `proxy_request_buffering on` 会让 nginx 先把整个请求体吸进自己的临时盘再转给后端，浏览器几秒内就把数据全交完，进度条从此停在 100% 干等几分钟——看着就是卡死。在一套与线上一致的配置下实测（`http2 on`、20MB 请求体、后端固定 2MB/s 消费）：缓冲开时请求体 0.12 秒交给 nginx、之后空等 10.16 秒才拿到响应；缓冲关时 nginx 按后端真实速率反压浏览器，交数据本身就耗时 5.49 秒，浏览器看到的就是端到端速率。

超时给到 1 小时：慢但仍在动的长传输不该由 nginx 掐断，真正断流的由后端停顿看门狗（`OPS_SFTP_STALL_TIMEOUT`，60 秒没有新字节进出就判死）负责报错。前缀写成 `^~` 是为了压过可能更短的 `location /api/`；这条只管文件通道，终端 WebSocket 走 `/api/ops/terminal`，不受它影响。站点其它 location 里已有的通用反代头（`Host` / `X-Forwarded-*`）照抄一份即可。

## API 概览

统一前缀 `/api`，统一返回 `{ "code": 0, "message": "success", "data": ... }`，除登录外全部需要 Bearer token。

| 分组 | 代表路由 |
| --- | --- |
| `/api/auth` | 登录 / me / 改密 / 登出 |
| `/api/users` | 账号 CRUD、禁用、权限位（仅管理员） |
| `/api/agents` | 智能体 CRUD、复制 |
| `/api/chat` | 会话与消息、SSE completions、运维写操作 confirm/reject |
| `/api/knowledge` | 知识类型与文档、检索测试、索引重建 |
| `/api/settings` | 模型配置 CRUD + 连通性测试 |
| `/api/ops` | 服务器/数据库台账、SQL 执行与收藏、表结构与设计表、数据浏览与行级写、全量导出、Redis 浏览与结构化写、审计、WS 终端 |
| `/api/ops/servers/{id}/files` | SFTP 列目录/上传/下载/新建/删除 |
| `/api/office` | 简历上传/分析/对比/下载 |
| `/api/office/netdisk` | 网盘绑定状态、授权链接、绑定/解绑 |
| `/api/ai-gateway` | 网关概览、上游通道、模型路由、统一密钥、调用日志与统计（仅管理员） |

例外：对外的 OpenAI 兼容端点挂在站点根的 `/v1` 下（不带 `/api` 前缀、不套统一返回结构、不用 JWT，而是用网关自己签发的 `sk-yv-*` 密钥），这样调用方的 `base_url` 直接填 `https://<站点>/v1` 就能被各家 SDK 识别。

| 对外端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | 对话补全，支持 `stream: true` 的 SSE 透传 |
| `POST /v1/embeddings` | 向量化 |
| `GET /v1/models` | 列出网关已映射的对外模型名（不计入审计日志） |

## AI 网关接入

网关不是独立服务，它就跑在应用里，只是挂在站点根的 `/v1` 下（不带 `/api` 前缀），所以 `base_url` = 站点 origin + `/v1`。

| 部署形态 | base_url |
| --- | --- |
| compose 直连（默认映射 18099） | `http://<服务器IP>:18099/v1` |
| 反向代理 + 域名 | `https://<你的域名>/v1` |
| 反向代理，网关单独一个子域 | `https://ai.<你的域名>/v1` |
| 本地开发（Vite 5173） | `http://localhost:5173/v1`，已代理到 18099 |

控制台「系统设置 → AI 网关 → 概览」里的「接入地址」是后端算好直接给的，可复制。默认按打开控制台时的 origin 推导；**如果网关走的是另一个域名**，设 `AI_GATEWAY_PUBLIC_ORIGIN=https://ai.<你的域名>`，概览页就会显示那个地址——否则管理员从控制台域名打开页面，看到的会是一个「也能用、但不是对外公布的」地址。

凭据与模型：`api_key` 用「统一密钥」页签签发的 `sk-yv-*`（明文只在创建那一次显示，库里只存 SHA-256 摘要），与控制台登录的 JWT 互不通用；请求里的 `model` 写「模型路由」页签配置的对外模型名，同一个名字可按 `priority` 绑多条上游做故障转移。

```bash
curl https://<你的域名>/v1/chat/completions \
  -H "Authorization: Bearer sk-yv-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "你好"}], "stream": true}'
```

走 nginx 反代时 `/v1` 要单独转发，并为 SSE 关掉缓冲、放宽读超时：

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:18099;
    proxy_http_version 1.1;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;   # 对齐 AI_GATEWAY_READ_TIMEOUT，nginx 默认 60s 会掐断长回答
}
```

转发类调用（`chat/completions`、`embeddings`）的响应都带 `X-Yangvis-Request-Id`，报障时提供它可以直接定位到「调用日志」里那一行。认证阶段就被拒的请求和 `GET /v1/models` 没有这个头——它们本来就不产生日志行。

## 安全设计与部署注意

应用内置的安全机制：

- 密钥无默认值：`AUTH_JWT_SECRET` / `ENCRYPTION_KEY` 未设置或过短时启动直接失败，杜绝「默认密钥上线」
- 模型 api_key Fernet 加密落库，接口只回掩码；密码 PBKDF2 散列
- 运维写操作两阶段确认 + 超时自动作废；`ops_write` 权限位闸门；全量命令/SQL 审计
- WebSocket 终端使用一次性入场票（60s 过期），不在 URL 里带 token
- AI 网关密钥只存 SHA-256 摘要（明文仅在创建那一次返回），上游厂商 api_key 用 Fernet 加密、接口只回掩码；转发时只会带上游自己的密钥，调用方的网关密钥不会外泄

部署者需要自行负责的：

- **这个工具的本质是把服务器 shell 和数据库查询能力开放给登录用户**，务必放在内网或反向代理 + HTTPS 之后，并只对可信人员建号
- 生产部署建议删掉 compose 里 MySQL / Qdrant 的端口映射（两者均无鉴权），只保留 app 的 18099
- `ENCRYPTION_KEY` 一经设定不要轮换，否则已存的 api_key 全部作废
- `/v1` 网关等于把上游模型额度开放给持有密钥的调用方：按需签发、随时停用，并注意调用日志正文可能含敏感内容（可用 `AI_GATEWAY_LOG_PAYLOAD=false` 关闭留档，或定期在日志页清理历史）

## License

[MIT](LICENSE)
