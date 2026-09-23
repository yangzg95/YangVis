# YangVis（杨维斯）

一个自托管的全能型 AI 工作台：统一控制台下集成智能对话、知识库问答（RAG）、智能运维与智能办公能力。
模型层不绑定任何服务商——只要兼容 OpenAI 协议（火山方舟、DeepSeek、OpenAI、vLLM 等），填入 `base_url + model + api_key` 即可使用。

## 功能模块

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

### 🛠️ 智能运维
- 服务器台账：SSH 连接管理、连通性测试
- Web 终端：xterm.js + WebSocket + PTY，断线自动重连、内容搜索、可点链接，手敲命令也进审计
- SFTP 文件面板：目录浏览、拖拽上传、下载、新建/删除
- 数据库工作台：MySQL 多页签查询（Navicat 式批量执行、消息/摘要面板）、SQL 收藏、Redis 只读浏览（scan/key 详情）
- AI 运维助手：自然语言 → 命令/SQL；只读直接执行，写操作必须人工确认后才放行
- 权限闸门：`ops_write` 权限位控制一切写操作，台账全员共用、只读用户也能查
- 全量审计：AI 执行的、终端手敲的、SQL 执行的，统一留痕可翻查

### 📄 智能办公
- 简历分析：PDF/DOCX 文本抽取 → LLM 评估 → 关键信息提取与多份对比（prompt 走智能体体系，可自行调整）
- 百度网盘：xpan 开放平台绑定（oob 授权码模式，无需公网回调），简历文件可备份至网盘应用目录

### 👥 账号与权限
- 自建账号体系：`sys_user` 表 + PBKDF2 密码散列 + 本地签发 JWT，不依赖任何外部认证服务
- 管理员在「账号管理」里建号、禁用、授予运维写权限；首次启动可用环境变量引导初始管理员

### ⚙️ 模型设置
- 按用户保存，对话/向量两种用途各自独立配置
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
│   │   │                      # ops ops_files resume netdisk
│   │   ├── services/          # 业务层：RAG、对话、运维安全闸门、SFTP、简历…
│   │   └── models/            # SQLAlchemy 实体 + Pydantic schema
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── src/
│       ├── views/             # Login / 对话(客服) / Knowledge / Agents
│       │                      # OpsServer(Terminal) / OpsDatabase(Chat) / Resume
│       │                      # Users / Settings
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
npm run dev                   # http://localhost:5173，/api 已代理到 18099
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
| `OPS_CONFIRM_TIMEOUT` | 180 | 写操作确认等待时长（秒） |
| `OPS_SFTP_MAX_UPLOAD_BYTES` `OPS_SFTP_LIST_LIMIT` | 50MB / 2000 | SFTP 面板限制 |
| `BAIDU_NETDISK_APP_KEY/SECRET_KEY/APP_NAME` | 空 | 任一为空则网盘功能整体隐藏 |
| `MODEL_HTTP_TIMEOUT` | 30 | 调用户模型服务的出站超时 |

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
| `/api/ops` | 服务器/数据库台账、SQL 执行与收藏、Redis 浏览、审计、WS 终端 |
| `/api/ops/servers/{id}/files` | SFTP 列目录/上传/下载/新建/删除 |
| `/api/office` | 简历上传/分析/对比/下载 |
| `/api/office/netdisk` | 网盘绑定状态、授权链接、绑定/解绑 |

## 安全设计与部署注意

应用内置的安全机制：

- 密钥无默认值：`AUTH_JWT_SECRET` / `ENCRYPTION_KEY` 未设置或过短时启动直接失败，杜绝「默认密钥上线」
- 模型 api_key Fernet 加密落库，接口只回掩码；密码 PBKDF2 散列
- 运维写操作两阶段确认 + 超时自动作废；`ops_write` 权限位闸门；全量命令/SQL 审计
- WebSocket 终端使用一次性入场票（60s 过期），不在 URL 里带 token

部署者需要自行负责的：

- **这个工具的本质是把服务器 shell 和数据库查询能力开放给登录用户**，务必放在内网或反向代理 + HTTPS 之后，并只对可信人员建号
- 生产部署建议删掉 compose 里 MySQL / Qdrant 的端口映射（两者均无鉴权），只保留 app 的 18099
- `ENCRYPTION_KEY` 一经设定不要轮换，否则已存的 api_key 全部作废

## License

[MIT](LICENSE)
