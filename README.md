# YangVis（杨维斯）

一个自托管的全能型 AI 工作台：统一控制台下集成智能对话、知识库问答（RAG）、智能运维与智能办公能力。
模型层不绑定任何服务商——只要兼容 OpenAI 协议（火山方舟、DeepSeek、OpenAI、vLLM 等），填入 `base_url + model + api_key` 即可使用。

## 功能模块

### 🏠 首页工作台（[文档](docs/features/home-dashboard.md)）
- 问候区 + 模块卡片（各模块在本人名下的数量）+ 最近对话列表，点一条带 `/chat?conv=<id>` 深链直进会话
- 对话模型未测通先提示「去配置」；管理员多一张 AI 网关卡片，主数字是**今日**调用数（与网关概览的累计口径区分）；数量统一来自 `GET /api/dashboard/summary`，按 owner 隔离

### 💬 智能对话
- 多智能体体系：独立人设/温度与知识库、运维、记忆三个能力开关，内置+自建、可对对话页隐藏，聊到一半换人设强制另开会话（[文档](docs/features/agents.md)）
- 人设**配置版本化**：影响输出的改动追加不可变版本，历史运行与对比页永远对得上当时那版人设（[文档](docs/features/agent-versions.md)）
- 对话基础能力：SSE 流式、会话重命名/搜索/删除、失败重试、图片/文件附件、Mermaid 绘图与在线编辑（[文档](docs/features/chat-core.md)）
- 上下文管理：手动压缩（流式预览、锚点分界、可取消、可渐进再压）、窗口外历史自动滚动摘要、跨会话长期记忆（[压缩](docs/features/chat-compaction.md) · [摘要与记忆](docs/features/chat-memory.md)）
- 统一 Trace 时间线：对话思考过程与工作流每步现场同构渲染，每次模型往返、每个工具调用的起止/耗时/入参返回都对得上，失败步也保留挂点前轨迹（[文档](docs/features/unified-trace.md)）
- 对话可挂工具：内置**本地工具**（联网搜索、网页抓取、时间/UUID、网盘存文本、读简历，代码注册的只读目录）与外部 **MCP 服务**（stdio / SSE / Streamable HTTP，会话级勾选、每次回答建连即关）（[文档](docs/features/local-tools.md)）
- 对话里直接用运维只读工具（巡检、查库）；危险写操作走「两阶段确认」卡片，超时按未批准处理（[文档](docs/features/ops-chat-confirm.md)）

### 📚 知识库 RAG（[文档](docs/features/knowledge-base.md)）
- 按用户隔离的全链路自建 RAG：上传 → 切片 → 向量化（Qdrant 每用户一个 `kb_u<owner_id>` collection）→ 带 `[1][2]` 引用溯源的回答
- 对话模型与向量模型独立配置，未配向量模型整页阻断；支持检索预览、低分阈值过滤、批量删除与单篇/全量索引重建

### 🔁 工作流编排（[设计器](docs/features/workflow-designer.md) · [运行与清理](docs/features/workflow-runs.md) · [对比评测](docs/features/workflow-eval.md)）
- 把「一次功能调用」拆成几步写模型的流水线：输入变量、每步的智能体/Prompt 模板/输出格式（markdown 或 JSON 契约）页面上改，不用发版
- 向导弹窗式编辑器，**一个并行组一页**（组间串行、组内并发），结构图按模板里的 `{{变量}}` 自动反推、只读预览；分组唯一口径是「相邻且同名」，保存时后端把所有跑起来必失败的写法（同组互引、未声明变量、不可见人设等）一次拦下
- 10 条内置流程（简历分析、幻灯片生成、面试参考答案等）只读、可复制起步；功能侧按 slug 调内置流程，界面停用不影响功能本身
- 运行：SSE 实时进度、历次运行逐步看渲染 prompt 与原始输出（parsed/raw 切换）、失败步单步重跑或断点续跑、中途打开可跟随；历史按保留窗口自动清理
- 双运行对比（URL 即选择，可收藏转发）：输入差异 + 按步骤 key 对齐的输出；每步开跑即绑定当时人设版本，两侧版本不同有标记，输出差异可归因
- 对比页带 Rubric 打分（2–6 条标准、1–5 星，细则可复用）：保存即冻结标准快照，重复提交按更新、A/B 反选算另一次评测；被评测引用的运行在历史清理时硬豁免

### 🛠️ 智能运维
- 服务器台账（[文档](docs/features/ops-servers.md)）：SSH 连接管理、TOFU 主机指纹、连通性测试、凭据 Fernet 加密；台账全员共用，只读用户也能查
- Web 终端（[文档](docs/features/ops-terminal.md)）：xterm.js + WebSocket + 真 PTY，断线自动重连、服务端会话保留 30 分钟（断线不丢现场）、内容搜索/可点链接/双主题，手敲命令同样进审计
- SFTP 文件面板（[文档](docs/features/ops-sftp.md)）：目录浏览、拖拽上传/下载/新建/删除/复制粘贴，500MB 上限边传边计数、无新字节停顿看门狗
- MySQL 工作台（[文档](docs/features/ops-database.md)）：多页签批量执行（消息/摘要面板）、SQL 收藏/历史、CSV/JSON/Markdown/INSERT 导出（全量走服务端分批流式）、表结构、设计表（ALTER 预览后执行）、ER 图、数据浏览行内改值；每条会话强制 `READ ONLY` 事务，不靠 SQL 解析保证只读
- Redis 控制台（[文档](docs/features/ops-redis.md)）：SCAN 浏览 + key 详情，五类元素结构化增删、改 string 值保留原 TTL、EXPIRE/PERSIST、批量 UNLINK
- AI 运维助手（[文档](docs/features/ops-chat-confirm.md)）：自然语言 → 命令/SQL，只读直执、写操作必须人工确认（15 分钟不批自动作废）；命令步骤与正文按真实顺序交错、思考链与进度折成细字行，SSH 断线自动重连重试一次，命令 300s / 单轮 900s 超时
- 安全底座：`ops_write` 单个权限位闸住一切写操作（前端+后端双重）；AI、终端手敲、SQL、SFTP 统一进 `ops_audit_log`，普通用户看自己、管理员看全部（[文档](docs/features/ops-servers.md)）

### 📄 智能办公
- 简历分析（[文档](docs/features/resume.md)）：PDF/DOCX 文本抽取 → 内置工作流跑 LLM 评估 → 关键信息提取与 2–10 份多份对比，独立报告整页展示，分析 prompt 走内置工作流+智能体体系，复制出来即可自行调整
- 在线简历工作台（[文档](docs/features/resume-builder.md)）：整页三栏编辑（模块导航 + 表单 + A4 实时预览），五模块拖拽排序、富文本、3 版式 × 7 主题，AI 帮写/润色/检查走三个专用智能体，浏览器打印导出 PDF
- 幻灯片（[文档](docs/features/slides.md)）：AI 按结构化 JSON 生成自包含 HTML 幻灯片，工作台支持表单与源码双模式编辑+实时预览，放映页满屏播放/键盘翻页/就地改内容，浏览器打印出 PDF
- 面试记录（[文档](docs/features/interview.md)）：面试场次与题目手工录入，参考答案走内置工作流异步生成、轮询看进度
- 百度网盘（[文档](docs/features/netdisk.md)）：xpan 开放平台绑定（oob 授权码模式，无需公网回调），简历/报告/数据库备份可存至网盘应用目录，也可作为对话工具让模型存文本

### 🧰 开发工具
- YApi 同步（[文档](docs/features/yapi-sync.md)）：菜单「开发工具 → YApi 同步」，选本机目录扫描 Java Controller（Spring/Swagger 注解），一个 Controller 一个分类、按「方法 + 路径」判重新增或更新到 YApi；该功能面向开发者本机使用

### 🔌 AI 网关（[文档](docs/features/ai-gateway.md)）
- 对外一个统一密钥（`sk-yv-*`，只存 SHA-256 摘要、明文仅创建时显示），背后挂多个 OpenAI 兼容上游通道，平台级共享、仅管理员可管
- 模型名映射 + `priority` 优先级故障转移：上游不可达/限流/鉴权失败自动换下一条（仅在首字节发出前切换，默认最多 3 条）
- `model: "auto"`：调用方不点名，硬要求（embedding/vision/tools）按规则判，任务类型问一次池内最高优先级模型来判（可整体关掉、超时或答得不像话退回规则），没有满足硬要求的路由直接 404 `auto_model_not_found`，每次决策落进日志的 `auto_route`
- 两层防自环：通道 `base_url` 保存时拒绝本站/localhost/回环地址；转发请求盖 `X-Yangvis-Gateway` 头，入站再见到就回 508 `gateway_loop_detected`（兜内网别名等保存期认不出的情况）
- OpenAI 兼容三端点挂在站点根 `/v1`：`chat/completions`（SSE/非流式）、`embeddings`、`models`，标准 SDK 改 `base_url` 直连；每次调用记通道/模型/token/首字节耗时/IP/重试轨迹，正文可截断可关，日志失败不影响调用

### 👥 账号与权限（[文档](docs/features/accounts.md)）
- 自建账号体系：`sys_user` + PBKDF2 散列 + 本地 JWT（无状态但每请求回库查状态，禁用即时生效），登录失败按账号+IP 双维锁定并加验证码，不依赖任何外部认证
- 管理员在「用户管理」建号、禁用、授予 `ops_write`；首次启动可用环境变量引导初始管理员

### ⚙️ 模型设置（[文档](docs/features/model-settings.md)）
- 「系统设置 → 模型与集成」按用户保存，对话/向量两种用途独立配置，一键连通性测试（向量还会实测维度）；api_key 用 Fernet 加密落库、接口只回掩码
- 这里配的是**站内自用模型**；把本站当 OpenAI 接口给外部系统调用是「AI 网关」的职责，两页互相有入口
- 对话模型「思考过程」三态：自动（服务商返回才显示）/ 开启（额外请求参数原样并入，字段名由配置人按厂商文档填，本站不猜）/ 关闭（解析层丢弃、界面与落库都不留）

---

## 功能实现文档

每个功能点在 `docs/features/` 下有一篇实现文档，统一按「用户可见行为 → 关键取舍 →
实现结构（真实文件路径）→ 升级行为 → 测试」组织：

| 模块 | 文档 |
| --- | --- |
| 首页工作台 | [home-dashboard.md](docs/features/home-dashboard.md) |
| 智能对话（会话/SSE/重试/附件/绘图） | [chat-core.md](docs/features/chat-core.md) |
| 多智能体体系 | [agents.md](docs/features/agents.md) |
| 智能体配置版本化 | [agent-versions.md](docs/features/agent-versions.md) |
| 滚动摘要与长期记忆 | [chat-memory.md](docs/features/chat-memory.md) |
| 手动对话压缩 | [chat-compaction.md](docs/features/chat-compaction.md) |
| 统一 Trace 时间线 | [unified-trace.md](docs/features/unified-trace.md) |
| 本地工具与 MCP 服务 | [local-tools.md](docs/features/local-tools.md) |
| 知识库（RAG） | [knowledge-base.md](docs/features/knowledge-base.md) |
| 工作流设计器（并行组/结构图） | [workflow-designer.md](docs/features/workflow-designer.md) |
| 工作流运行（SSE/重跑/清理） | [workflow-runs.md](docs/features/workflow-runs.md) |
| 双运行对比与 Rubric 评测 | [workflow-eval.md](docs/features/workflow-eval.md) |
| 服务器台账/权限闸门/审计 | [ops-servers.md](docs/features/ops-servers.md) |
| Web 终端（持久 PTY） | [ops-terminal.md](docs/features/ops-terminal.md) |
| SFTP 文件面板 | [ops-sftp.md](docs/features/ops-sftp.md) |
| MySQL 数据库工作台 | [ops-database.md](docs/features/ops-database.md) |
| Redis 控制台 | [ops-redis.md](docs/features/ops-redis.md) |
| AI 运维助手与两阶段确认 | [ops-chat-confirm.md](docs/features/ops-chat-confirm.md) |
| 简历分析与多份对比 | [resume.md](docs/features/resume.md) |
| 在线简历工作台 | [resume-builder.md](docs/features/resume-builder.md) |
| 幻灯片 | [slides.md](docs/features/slides.md) |
| 面试记录 | [interview.md](docs/features/interview.md) |
| 百度网盘 | [netdisk.md](docs/features/netdisk.md) |
| YApi 同步 | [yapi-sync.md](docs/features/yapi-sync.md) |
| AI 网关 | [ai-gateway.md](docs/features/ai-gateway.md) |
| 账号与权限 | [accounts.md](docs/features/accounts.md) |
| 模型设置 | [model-settings.md](docs/features/model-settings.md) |

---

## 技术栈

**前端**：Vue 3.5 · TypeScript 5.6 · Vite 5 · Pinia 2 · Vue Router 4 · Ant Design Vue 4 · xterm.js · Mermaid · Axios

**后端**：Python 3.12 · FastAPI 0.115 · SQLAlchemy 2.0 + PyMySQL · LangChain / LangGraph · Qdrant · asyncssh（SSH/SFTP/PTY）· redis-py · PyJWT · cryptography（Fernet）· pypdf / python-docx · httpx

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
├── docs/                      # features/ 功能实现文档、schema.sql（DDL 对照稿）、设计复盘
├── scripts/                   # dev.sh/.bat 本地起两端；build.sh/.bat 构建镜像；docker-run.ps1 裸机构建部署
├── Dockerfile                 # 前后端一体镜像
└── docker-compose.yml         # app + MySQL + Qdrant
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

构建镜像走国内源：`node:20-alpine` 取 `docker.1ms.run`、`python:3.12-slim` 取
`docker.m.daocloud.io`，apt 用 USTC、npm 用 npmmirror、pip 用 tuna。Dockerfile 顶部
**不钉 `# syntax=` 前端**——这份文件只用经典指令，BuildKit 自带的够用；钉了反而每次
构建开头要多拉一次外部镜像，镜像站 502 一下整场构建还没开始就挂。若哪天确实用到了
`RUN --mount` 这类新语法，再把前端镜像换成能访问的源。

## 本地开发

```bash
# 后端（Python 3.12–3.13；生产镜像固定 3.12-slim）
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

也可以使用 `scripts/` 下的 `dev.sh / dev.bat` 同时拉起两端。MySQL 与 Qdrant 可用 `docker-compose up -d mysql qdrant` 单独启动。

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
| `OPS_SSH_TIMEOUT` `OPS_CMD_TIMEOUT` `OPS_AGENT_CMD_TIMEOUT` | 15 / 20 / 300 | 运维通道超时（秒）：建连 / 交互终端单条命令 / AI 通道单条命令 |
| `OPS_OUTPUT_LIMIT` `OPS_SQL_ROW_LIMIT` | 8192 / 200 | 回传模型的输出/行数上限 |
| `OPS_EXPORT_MAX_ROWS` | 200000 | 数据页「导出全部」的单次行数上限，超了直接拒 |
| `OPS_CONFIRM_TIMEOUT` | 900 | 写操作确认等待时长（秒），超时按未批准处理 |
| `OPS_SFTP_MAX_UPLOAD_BYTES` `OPS_SFTP_LIST_LIMIT` | 500MB / 2000 | SFTP 面板的单文件大小上限与单次列目录条数 |
| `OPS_SFTP_OP_TIMEOUT` `OPS_SFTP_STALL_TIMEOUT` | 30 / 60 | 打开 SFTP 会话的等待上限、传输连续这么久没有新字节进出就判死（秒） |
| `BAIDU_NETDISK_APP_KEY/SECRET_KEY/APP_NAME` | 空 | 任一为空则网盘功能整体隐藏 |
| `MODEL_HTTP_TIMEOUT` | 30 | 调用户模型服务的出站超时 |
| `AI_GATEWAY_CONNECT_TIMEOUT` `AI_GATEWAY_READ_TIMEOUT` | 10 / 300 | 网关转发上游的连接/读取超时（秒） |
| `AI_GATEWAY_LOG_PAYLOAD` `AI_GATEWAY_LOG_MAX_CHARS` | true / 8000 | 是否把请求响应正文写进审计日志、单条正文截断字符数 |
| `AI_GATEWAY_MAX_ATTEMPTS` | 3 | 单次调用最多尝试几条上游通道 |
| `AI_GATEWAY_AUTO_LLM` | true | `model: "auto"` 是否允许调用一次上游模型来判任务类型；关掉就只按规则和 `priority` 选 |
| `AI_GATEWAY_AUTO_CLASSIFY_TIMEOUT` | 8 | 上面那次分类的独立超时（秒），超时/报错都按「没判出来」继续走优先级 |
| `AI_GATEWAY_AUTO_CLASSIFY_CHARS` | 1200 | 送进分类 prompt 的用户正文截断长度 |
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
| `/api/agents` | 智能体 CRUD、复制、版本史与版本快照（`/{id}/versions`） |
| `/api/chat` | 会话与消息、SSE completions、运维写操作 confirm/reject |
| `/api/knowledge` | 知识类型与文档、检索测试、索引重建 |
| `/api/workflows` | 流程定义 CRUD/复制、发起运行与 SSE 跟随、运行详情、单步重跑/断点续跑、历史清理；评分细则 `/rubrics` 与双运行对比评测 `/evaluations` |
| `/api/settings` | 模型配置 CRUD + 连通性测试 |
| `/api/ops` | 服务器/数据库台账、SQL 执行与收藏、表结构与设计表、数据浏览与行级写、全量导出、Redis 浏览与结构化写、审计、WS 终端 |
| `/api/ops/servers/{id}/files` | SFTP 列目录/上传/下载/新建/删除 |
| `/api/office` | 简历上传/分析/对比/下载 |
| `/api/office/netdisk` | 网盘绑定状态、授权链接、绑定/解绑 |
| `/api/ai-gateway` | 网关概览、上游通道、模型路由、统一密钥、调用日志与统计（仅管理员） |

例外：对外的 OpenAI 兼容端点挂在站点根的 `/v1` 下（不带 `/api` 前缀、不套统一返回结构、不用 JWT，而是用网关自己签发的 `sk-yv-*` 密钥），这样调用方的 `base_url` 直接填 `https://<站点>/v1` 就能被各家 SDK 识别。

| 对外端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | 对话补全，支持 `stream: true` 的 SSE 透传；`model` 可以是 `auto` |
| `POST /v1/embeddings` | 向量化；`model` 同样可以是 `auto`（此时按 embedding 能力筛候选） |
| `GET /v1/models` | 列出网关已映射的对外模型名（不计入审计日志）；只要有一条路由勾了「参与 Auto」，`auto` 就会出现在列表里 |

## AI 网关接入

网关不是独立服务，它就跑在应用里，只是挂在站点根的 `/v1` 下（不带 `/api` 前缀），所以 `base_url` = 站点 origin + `/v1`。

| 部署形态 | base_url |
| --- | --- |
| compose 直连（默认映射 18099） | `http://<服务器IP>:18099/v1` |
| 反向代理 + 域名 | `https://<你的域名>/v1` |
| 反向代理，网关单独一个子域 | `https://ai.<你的域名>/v1` |
| 本地开发（Vite 5173） | `http://localhost:5173/v1`，已代理到 18099 |

控制台「系统设置 → AI 网关 → 概览」里的「接入地址」是后端算好直接给的，可复制。默认按打开控制台时的 origin 推导；**如果网关走的是另一个域名**，设 `AI_GATEWAY_PUBLIC_ORIGIN=https://ai.<你的域名>`，概览页就会显示那个地址——否则管理员从控制台域名打开页面，看到的会是一个「也能用、但不是对外公布的」地址。

凭据与模型：`api_key` 用「统一密钥」页签签发的 `sk-yv-*`（明文只在创建那一次显示，库里只存 SHA-256 摘要），与控制台登录的 JWT 互不通用；请求里的 `model` 写「模型路由」页签配置的对外模型名，同一个名字可按 `priority` 绑多条上游做故障转移。不想让调用方关心模型名，就写 `auto`，并在路由上勾「参与 Auto」+ 填能力标签（详见上文特性说明）；`auto` 是保留名，不能自己建一条叫它的路由。

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
