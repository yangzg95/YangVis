# 知识库 · 代码仓库接入（设计方案）

> 状态：P0 已实施（2026-11），P1 已交付「仓库源文件带行号只读预览」与「HTTPS + 个人访问令牌认证」。本文是在现有知识库（见 [knowledge-base.md](knowledge-base.md)）上增加「代码仓库」
> 这一内容来源的设计，不改写既有链路。目标是让对话智能体可以基于某个 Git 仓库的真实源码回答
> 「这个功能在哪实现的」「这段逻辑什么意思」「改这里会影响谁」一类问题，并且引用能落到
> `文件路径:行号`。
>
> 实施与设计的两处偏差：① 密钥生成/校验用 **asyncssh 库**（运维域已有依赖）而不是 shell out
> `ssh-keygen`，传输仍是本地 git 子进程 + openssh-client 的 `ssh` 二进制；② 同步锁占用时
> `POST /repos/{id}/sync` 返回 HTTP 200 + `already_running=true`（业务口径，非 409）。

## 1. 背景与目标

现有知识库的模型是 **项目 → 知识类型 → 上传文档**：文本抽取后全文落 `kb_document.content`，
后台按 700/100 的散文切分器切片、embedding 写入按用户隔离的 Qdrant collection，对话时模型调
`search_knowledge_base` 检索并带 `[n]` 编号引用。把代码接进来有三个绕不开的差异：

1. **来源是活的**。代码在 Git 远端持续演进，需要 clone、增量同步、按文件增删改索引，而不是
   一次上传；用户不该为一次 rebase 重新传一遍文件夹。
2. **散文切分器不适合代码**。它按中日韩句读和段落切，会把一个函数从中间腰斩；代码切片必须沿
   语法结构（函数 / 类 / 方法）边界，并且每个切片要回答得清「我是哪个文件的哪个符号」。
3. **引用必须能定位**。`README.md 第 3 片` 对文档够用，对代码必须是
   `src/services/foo.py:120-148（ClassName.method）`，否则模型给了答案人也没法核对。

设计目标：仓库作为知识库的**第三种文档来源**（`source=repo`）接入，复用现有的项目/类型隔离、
embedding 闸门、模型指纹、全量重建、引用编号与计费口径，而不是另起一套平行系统。

已确认的范围决策：

- **传输走 SSH 或 HTTPS**：支持 `ssh://` 与 scp-like（`git@host:group/repo.git`）地址，
  每个仓库一对独立 deploy key；http(s) 地址支持**匿名公开/内网只读**与**个人访问令牌
  （PAT）**两种认证。认证方式由地址协议决定：SSH 只能用 deploy key，http(s) 只能匿名或
  令牌，互斥组合在创建/更新时直接拒绝。
- **同步仅手动触发**：接入时首同步 + 仓库信息条上的「立即同步」；定时轮询与 webhook 放 P1。
- **仓库与知识类型 1:1 绑定**。
- 切片器 P0 用语言族规则识别结构边界，tree-sitter AST 放 P1。

非目标（P0 不做）：定时 / webhook 自动同步、多分支与 tag、AST 级切片、
跨仓库联合检索的独立过滤维度、本地服务器目录作为来源。这些列入 §12 后续批次。

## 2. 用户可见行为

- 知识库页「知识类型」卡片的操作区新增 **接入仓库**：选择项目（沿用当前选中项目），填
  Git SSH 地址（`git@host:group/repo.git` 或 `ssh://git@host/group/repo.git`）或
  http(s) 地址、分支（可空，默认探测远端默认分支）、可选排除 glob。**认证方式由地址
  协议决定**：SSH 地址只出现 deploy key 单选项；http(s) 地址出现「匿名访问 / HTTPS 令牌」
  两个单选项。编辑时地址不可改（改地址等于换来源，走移除后重新接入）。
  提交时自动在该项目下创建一个与仓库同名的知识类型，仓库与类型 **1:1 绑定**，该类型的
  文档列表就是仓库里被索引的文件列表。
- **Deploy key 两种给法**（SSH）：① 粘贴现有私钥；② 点「生成密钥对」，后端临时生成
  ed25519 对，弹窗展示**公钥**（一键复制，附「去 GitLab/GitHub 添加 deploy key」的操作
  提示），私钥只随本次提交加密落库。仓库详情可随时再查看公钥（公钥不是秘密），私钥任何
  接口都不回读。
- **HTTPS 令牌**（http(s)）：粘贴平台个人访问令牌（PAT），可附带用户名（缺省
  `x-access-token`，GitHub/GitLab 不挑用户名；Gitee 等平台要求填真实账号用户名）。
  令牌与私钥同一保密级别：Fernet 加密落库、任何接口不回读，编辑弹窗里令牌框永远空白，
  留空表示「不更换」，切到「匿名访问」保存即清除旧令牌。http(s) 没有主机指纹概念，
  TOFU 信息条对令牌/匿名仓库不出现。
- 接入前可用「测试连接」：后端对远端做一次 `git ls-remote`，返回默认分支与 HEAD 短 SHA；
  这是主机指纹的**首次记录时机**（TOFU，见下）。接入弹窗明确提示：会把仓库的受跟踪源码
  文件切片并调用向量模型，产生费用（与「上传文件夹」同一口径）。
- **主机指纹 TOFU**：测试连接/首次同步时自动接受并记录主机 host key 到仓库行；之后每次
  连接严格比对，指纹变化（重装系统、中间人）直接报「主机指纹不一致」，同步失败并在仓库
  信息条给出「查看详情 / 确认变更新指纹」入口，不静默放行。
- 接入后立即在后台执行首次同步，仓库类型顶部出现**仓库信息条**：URL、分支、HEAD 短 SHA、
  状态（同步中/就绪/失败）、文件数与分片数、上次同步时间、本次同步「新增 N / 修改 N /
  删除 N / 跳过 N」，以及 **立即同步**、**编辑**、**移除仓库**。
- 同步中：文件以仓库相对路径出现在文档表格（如 `src/services/knowledge/service.py`），
  状态沿用 pending → indexing → ready / error 四态，页面沿用现有的「有任务时 2.5 s 轮询、
  落定即停」机制；单文件失败（超限、解码失败等）只记该文件 error 并计入「跳过」，不拖垮整次
  同步。
- 仓库类型下**不显示上传按钮**（内容只能来自同步）；仓库文件不提供单篇删除（删了下次同步
  还会回来），但允许单篇「重新索引」。
- 「立即同步」做增量：只对 blob 发生变化的文件重新 embedding，删除的文件连同其向量一起清掉；
  同一仓库同时只能跑一次同步，重复点击直接返回「同步进行中」。
- 检索预览与对话引用：命中代码分片时，来源渲染为
  `src/foo/bar.py:120-148 · handle_sync`，回答中的 `[n]` 编号机制不变。
- 移除仓库：二次确认后删除其类型、全部文档与向量，加密私钥/令牌/指纹随仓库行一起删除，
  并尽力清理本地目录（工作克隆与临时密钥、凭据文件）；删除类型/项目的既有级联路径会连带
  完成仓库清理。
- 既有约束原样生效：没配好向量模型时「接入仓库」「立即同步」被同一道
  `require_embedding_ready` 闸挡住；切换 embedding 模型后的「重建索引」对仓库文件同样有效
  （因为全文仍在 MySQL）。

## 3. 总体方案：仓库物化成文档，不另立检索链路

核心取舍一句话：**一次同步 = 把远端仓库在某个 commit 下的受跟踪文本文件，物化成一批
`kb_document` 行；之后的一切（切分、embedding、检索、引用、重建、隔离）全部走既有链路。**

```
git 远端（SSH + 每仓库独立 deploy key，或 HTTPS + PAT 凭据文件；host key TOFU 钉死）
   │  clone --depth 1 / fetch + reset（后台任务，subprocess git，GIT_SSH_COMMAND 注入）
   ▼
本地工作区 {DATA_DIR}/repos/{owner_id}/{repo_id}/
   │  git ls-tree -r HEAD → (mode, blob_sha, path)
   │  与 kb_document.blob_hash 三路对账：added / changed / removed
   ▼
kb_document(source='repo', repo_id, filename=相对路径, content=文件文本, blob_hash)
   │  代码切片器（结构边界 + 行号 + 符号）
   ▼
kb_chunk(line_start, line_end, symbol) ── embedding ──▶ Qdrant（payload 增补 source/repo_id/行号）
```

为什么这么做，而不是为仓库单独建一套表和检索端点：

- **检索范围零改动**。项目在检索时被解析成一组 type id（`_resolve_type_scope`），仓库绑定在
  类型上，对话侧的闭包过滤、Qdrant 的 `type_id` MatchAny、过期 id 交集校验全部原样成立。
  聊天工具、智能体开关、4004「请先选择项目」没有一行需要改。
- **模型切换重建免费继承**。`rebuild_all` 从 MySQL 全文重建；代码文件的全文同样落库，换
  embedding 模型不需要重新 clone。
- **引用编号机制零改动**。`_CitationRegistry` 按 chunk_id 发稳定编号，代码分片只是多带
  路径、行号、符号这几个展示字段。
- **隔离与级联零改动**。行级 `owner_id`、越权 404、删类型/项目级联文档、向量后台收尸全部
  沿用。

代价是仓库与类型强制 1:1：不能把多个仓库塞进一个类型，也不能在一个类型里混上传文档和仓库。
这是刻意的——混源会让「这个文件从哪来、能不能删、下次同步会不会复活」全变成运行时判断，而
「一个类型 = 一个检索桶」的用户心智反而更清晰。需要多个仓库就建多个类型，项目下类型数量
没有上限。

## 4. 数据模型变更

### 新表 `kb_repo`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `owner_id` | BIGINT, index | 全站隔离口径不变 |
| `type_id` | BIGINT, **UNIQUE** | 1:1 绑定的知识类型；删类型即删仓库 |
| `name` | VARCHAR(128) | 展示名，默认取 URL 末段 |
| `auth_type` | VARCHAR(16) NOT NULL DEFAULT `'ssh_key'` | `ssh_key` / `none`（匿名 http(s)）/ `http_token`（HTTPS 个人访问令牌） |
| `remote_url` | VARCHAR(512) | 规范化后的地址，**永不包含凭据**；SSH 走外部密钥文件，令牌不拼 URL |
| `branch` | VARCHAR(255) | 跟踪分支；首次连接留空时由 `ls-remote` 探测后回填 |
| `private_key_enc` | LARGEBINARY(8192) NULL | deploy 私钥（OpenSSH 文本），`app.crypto.encrypt()` 加密；任何 API 不回读，只回 `has_credential` 与可公开的公钥 |
| `http_token_enc` | LARGEBINARY(2048) NULL | HTTPS 个人访问令牌密文（明文 ≤512），与私钥同一加密/不回读口径；只随 `auth_type='http_token'` 使用 |
| `http_username` | VARCHAR(128) NULL | 令牌配套用户名（≤128）；NULL 表示用缺省 `x-access-token`；只随令牌认证存在，切匿名/SSH 时清空 |
| `public_key` | TEXT NULL | 由私钥导出或随生成接口一并存下的公钥行，详情页可随时查看/复制 |
| `known_hosts` | TEXT NULL | TOFU 记录的 host key 行（known_hosts 格式，可能多行）；同步前写入专用 known_hosts 文件 |
| `host_fingerprint` | VARCHAR(255) NULL | 同一把 host key 的 SHA256 指纹（多把时逗号分隔），仅用于变更报错时给人看 |
| `exclude_globs` | TEXT NULL | 用户自定义排除规则，换行分隔；与内置默认排除取并集 |
| `status` | VARCHAR(16) | `pending` / `syncing` / `ready` / `error` |
| `error_msg` | VARCHAR(512) NULL | 同步失败原因（截断口径同文档） |
| `head_commit` | VARCHAR(40) NULL | 上次同步到的 commit SHA |
| `last_sync_at` | DATETIME NULL | |
| `last_sync_result` | JSON NULL | `{added, changed, removed, skipped, indexed_chunks, duration_ms, commit?}` |
| `created_at` / `updated_at` | DATETIME | 同站口径 |

本地布局不入库，全部由 id 确定性推出（改配置迁移目录也不留脏路径）：

```
{KB_REPO_DATA_DIR}/{owner_id}/{repo_id}/
├── work/         # bare-ish 工作克隆（--depth 1），可随时 rmtree 重建
├── id_key        # 当次同步解密写出的私钥，0600，同步结束（或异常）立即删除
├── http_cred     # 当次同步解密写出的令牌凭据文件（credential-store 一行 URL 形态），0600，收尾即删
└── known_hosts   # 从 known_hosts_line 生成，0600；首次连接前允许 accept-new
```

私钥/令牌文件放在 `work/` **之外**：工作区会被 `git reset --hard`、rmtree 重建，凭据
不能与仓库内容同目录。clone 支持的协议为 `ssh` 与 `http(s)`，`file://`、`ext::` 等
一律拒绝；URL 本身永远不含凭据（含 user:pass@ 的 http(s) 地址直接拒绝）。

### `kb_document` 加列（均为幂等 ALTER）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `source` | VARCHAR(16) NOT NULL DEFAULT `'upload'` | `upload` / `repo`，老行自动归类 |
| `repo_id` | BIGINT NULL, index | 仓库文件非空；上传文档恒 NULL |
| `blob_hash` | VARCHAR(64) NULL | Git blob SHA（40 位 sha1；列留 64 兼容将来 sha256 仓库），增量同步的对账依据，**省掉自己算内容哈希** |
| `language` | VARCHAR(32) NULL | 按扩展名归一的语言标识（`py`/`ts`/`java`…），切片器与展示用 |

另把 `filename` 从 VARCHAR(255) 放宽到 VARCHAR(512)：Java 深包路径 + 长类名经常超过 255
（现在「上传文件夹」靠从前面截断硬扛，仓库路径截断会破坏对账与定位）。放宽为幂等
`MODIFY`，迁移时查 `information_schema` 守卫。

### `kb_chunk` 加列

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `line_start` | INT NULL | 分片在源文件中的起始行（1-based），散文分片恒 NULL |
| `line_end` | INT NULL | 结束行 |
| `symbol` | VARCHAR(255) NULL | 所属符号，如 `KnowledgeService.index_document` |

### Qdrant payload 增补（只加不过滤）

point payload 从六个字段增补为：`owner_id / doc_id / chunk_id / seq / filename / type_id`
+ `source / repo_id / language / line_start / line_end / symbol`。

**检索的范围过滤仍只认 `type_id`**，与当年项目上线时不依赖 `project_id` 是同一个道理：
旧 point 没有这些新字段，一旦拿它们做 must 过滤，全量重建之前所有代码之外的老分片都会
一声不响地消失。新字段只用于命中后的展示；payload 仍不放正文。

### `SearchHit` 增补

`source`、`repo_id`、`language`、`line_start`、`line_end`、`symbol` 全部可选，老分片
缺省为 None。对话侧 `_format_hits` 渲染改为：

```
[n] 来源：src/services/knowledge/service.py:576-640（KnowledgeService.index_document）
{chunk.content}
```

无行号时退化为今天的 `来源：文件名`。随助手消息落库的引用清单与 SSE `citations` 事件
同步带上这些字段，前端引用卡对代码分片渲染语言标签与行号区间。

## 5. 代码切片器：`services/knowledge/code_chunk.py`

不引第三方解析依赖（tree-sitter 有原生构建，与 slim 镜像和 uv 锁都不友好，列入 P1）。
P0 用「语言族规则 + 行扫描」，覆盖代码问答里最要紧的结构边界。

### 收录规则

- 文件集合直接来自 `git ls-tree`：**只索引受跟踪的 blob**。`.gitignore` 由此免费生效，
  `.git/` 根本不会被遍历；符号链接（mode `120000`）与 gitlink 子模块（mode `160000`）跳过。
- 扩展名白名单（约 30 种）：`py pyx, js jsx mjs cjs, ts tsx, vue, java kt, go, rs,
  c h cpp cc hpp cxx, cs, rb, php, swift, scala, sh bash zsh, sql, html css scss less,
  xml, proto, gradle, dockerfile, properties, conf ini, tf`。`.md` 仍走散文切片器；
  `.json/.yaml/.csv/.log` 默认不进仓库索引（压缩/配置类文件召回价值低，锁文件另列排除）。
- 内置默认排除路径段：`node_modules/ dist/ build/ out/ target/ .next/ .nuxt/ coverage/
  __pycache__/ .idea/ .gradle/`，文件名：`*.min.js *.min.css *.map`，常见锁文件
  `package-lock.json yarn.lock pnpm-lock.yaml uv.lock poetry.lock Cargo.lock`。
  与用户的 `exclude_globs`（支持 `**/` globstar，标准库 `fnmatch` 按路径段匹配，不引依赖）
  取并集。
- 单文件硬门槛：NUL 字节嗅探（二进制）、按 utf-8 解码失败（含 BOM 处理）、单行超过
  2000 字符（压缩/生成文件）、大小超过 `KB_REPO_MAX_FILE_BYTES`（默认 300 KB）——
  命中任一即跳过并计数，不报错。
- 总量门槛：受跟踪候选文件超过 `KB_REPO_MAX_FILES`（默认 20000）时，超出部分跳过，
  同步结果里明确给出「跳过 N 个：超出文件数上限，请配置排除规则后重试」，而不是默默
  截断。

### 切片算法

```
1. 读入行数组，保留换行；统计 1-based 行号。
2. 摘出模块前缀（module docstring / package & import / shebang，上限 ~25 行）。
3. 按语言族找顶层定义边界：
   - Python：列 0 的 def/class（含 async），按缩进跟踪块结束；方法归并到所属 class，
     符号名 ClassName.method。
   - 大括号族（C/C++/Java/JS/TS/Go/Rust/C#…）：列 0 定义行正则
     （func/fn/pub fn/type/struct/class/interface/impl/namespace…）配合花括号深度归约块。
   - shell/sql/声明式语言：退化为「段落 + 行预算」，符号取最近的注释/函数头。
4. 一个符号块就是一个候选分片；块超过上限（CODE_CHUNK_CHARS=1800 字符 或
   CODE_CHUNK_LINES=80 行）时在块内按空行/花括号再切，这种二次切分带 10 行重叠；
   符号之间不重叠（函数边界被重叠切开没有收益）。
5. 每个分片拼一个两行情报头再送 embedding：
       // src/foo/bar.py · KnowledgeService.index_document (L576-L640)
   模块前缀只附在该文件的第一个分片上（成本与上下文的折中）。
   line_start/line_end 永远指向原始代码行，不含情报头。
6. 空文件 / 纯注释文件：切成至多一个分片（保留文件可被检索到），或直接跳过——P0 跳过
   纯空白文件，注释文件保留。
```

为什么目标 1800 字符而不是散文的 700：代码的自描述密度低（缩进、括号、签名），embedding
模型窗口普遍 8k token 起步；但仍远小于整文件，保证一个函数级问题能被单个分片完整回答。
常量与散文的 `CHUNK_SIZE` 一样先做代码常量、不进环境变量。

`KnowledgeService.index_document` 现在直接调 `split_text(doc.content)`，改为按
`doc.source` 分派：`repo` → `split_code(content, language, path)` 返回
`(text, line_start, line_end, symbol)` 列表；`replace_chunks` 扩展为接收带元组的分片
（散文调用方传全 None），其余写库、批量 embedding、point upsert 流程不动。

## 6. Git 同步引擎：`services/knowledge/repo_sync.py`

### 为什么是 subprocess git 而不是 GitPython / Dulwich

- 不加 Python 依赖；后端镜像 `python:3.12-slim` 里 `apt-get install git openssh-client`
  即可（已加在 Dockerfile 现有 USTC 镜像源那一段），本机开发通常已自带 git 2.x 与 ssh。
- clone/fetch/ls-tree 是管道式命令，`asyncio.create_subprocess_exec`（**参数数组、不经
  shell**）天然契合后台任务；GitPython 自身也是包 CLI 命令，Dulwich 对 smart HTTP / 各类
  自建 Git 服务兼容性是长期维护负担。

### SSH 凭据与进程加固

- 私钥以 OpenSSH PEM 文本存库（Fernet 加密），同步开始时解密写到
  `…/{repo_id}/id_key`（`0600`，当前进程用户独占），通过 `GIT_SSH_COMMAND` 注入：
  `ssh -i …/id_key -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=…/known_hosts
  -o StrictHostKeyChecking=yes -o ConnectTimeout=10`。**不放进 URL、不写 ssh-agent、
  不依赖部署机的 ~/.ssh**；finally 里无论成败删除密钥文件。
- `BatchMode=yes` + `GIT_TERMINAL_PROMPT=0` 双保险：密钥有 passphrase、host key 未记录
  等任何需要交互的情况都直接失败，绝不挂起。`GIT_LFS_SKIP_SMUDGE=1` 不拖 LFS 大文件。
- 密钥生成与校验走 **asyncssh**（运维域已在 requirements 里钉了 `asyncssh==2.18.0`，
  不引入新依赖）：`generate_private_key("ssh-ed25519")` 进程内生成、`import_private_key`
  校验粘贴的私钥并导出公钥落 `public_key`；拒绝带口令私钥（`BatchMode=yes` 下必然挂起）。
  选库函数而不是 shell out `ssh-keygen`，是因为测试环境与 Windows 开发机不保证装有
  openssh-client；镜像里 `apt-get install git openssh-client` 仍然必须——**传输用的
  `ssh` 二进制来自 openssh-client**，密钥操作不依赖它。
- **TOFU 指纹**：`known_hosts` 为空（首次）时用 `StrictHostKeyChecking=accept-new`
  跑一次探测，随后直接读回临时 known_hosts 文件，用 Python（base64 解码 key 后算
  SHA256，口径同 `ssh-keygen -lf`）得到指纹落库；之后固定
  `StrictHostKeyChecking=yes`。指纹变化时 ssh 以 exit 255 失败，任务把 stderr 写进
  仓库 `error_msg`，状态 error，**不自动覆盖**；前端识别 `Host key verification
  failed` / `REMOTE HOST IDENTIFICATION` 措辞给出「确认变更指纹」入口，用户确认后走
  `POST /repos/{id}/retrust` 重新探测并更新。
- 进程统一 `asyncio.create_subprocess_exec`（参数数组、不经 shell），环境白名单式构造
  （只透传 PATH/HOME/LANG 等必要变量 + 上面的 GIT_* 覆盖项），命令输出里出现私钥路径的
  行在落 error_msg 前脱敏。
- clone/fetch 套 `asyncio.wait_for`（`KB_REPO_CLONE_TIMEOUT` 默认 300 s）；整体同步任务
  有 `KB_REPO_SYNC_TIMEOUT` 上限，超时把仓库状态落成 error。
- URL 校验：只接受 `ssh://[user@]host[:port]/path`、scp-like `[user@]host:path` 与
  http(s) 形式，必须能解析出主机名；拒绝嵌入换行、`-` 开头的主机段（防 option 注入）、
  `file://` 与 `ext::` 协议。本工具是内网部署形态，默认不做私网封禁（内网 GitLab 本就在
  私网），预留 `KB_REPO_HOST_ALLOWLIST`（逗号分隔后缀，空=不限）做可选收紧。
- `ls-tree` 给的路径在拼进本地路径前仍做一次校验（拒绝绝对路径与 `..` 段、跳过符号链接），
  纵深防御不依赖「Git 不会这么返回」。

### HTTPS 令牌凭据：credential-store 文件注入

令牌绝不拼进 URL——git 失败时 stderr 会原样回显 URL，`error_msg` 就会泄密；也不进 argv。
做法是把令牌写成 git credential-store 的凭据文件（`http_cred`，0600，与 id_key 同生共死，
finally 必删），再用 `GIT_CONFIG_*` 环境变量把它注册成**唯一**的 credential helper：

```
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=credential.helper
GIT_CONFIG_VALUE_0=store --file=<posix 绝对路径>/http_cred
GIT_CONFIG_SYSTEM=<os.devnull>     # 压住系统级 helper（Linux 包管理器可能预置 store/cache）
GIT_CONFIG_GLOBAL=<os.devnull>     # 压住 Windows GCM（manager）与部署机 ~/.gitconfig
```

凭据文件内容是**一行 URL 形态**（实测 git 2.55 的 credential-store 对文档里的
`protocol=...\npassword=...` 键值块静默不认，只解析它自己 approve 写出的 URL 行）：

```
https://<urlencode(username)>:<urlencode(token)>@<host>[%3a<port>]\n
```

- 用户名/令牌全部 `urllib.parse.quote(safe="")` 百分号编码（JWT 风令牌里的 `+/=@:` 都要
  编码）；非默认端口写成 `host%3a8443`（小写），字面冒号该版本也能匹配但统一编码；
  用户名缺省 `x-access-token`。
- 文件必须 **LF**：用 `write_bytes` 写死，Windows 文本模式翻成 CRLF 会让主机名带尾部
  `\r`，整行失配后 git 直接走到「could not read Username」。
- helper 只按查询的协议+主机回吐凭据，错主机拿不到令牌；主机段在 URL 入口已过校验
  （无换行、无 `..`/option 注入）。令牌入库前先在**原始字符串**上拒控制字符再 strip，
  防 `tok\npassword=evil` 一类行注入。
- 探测（test-connection / ls-remote）走同一机制：凭据写在 TemporaryDirectory 里，
  probe 结束目录即销；http(s) 不产生主机指纹。SSH 地址带令牌、http(s) 地址带私钥、
  匿名却给令牌等互斥组合在 create/update 两处统一拒绝。
- 编辑语义：`http_token` 缺省=不变；非空串=更换（同时 auth 翻到 http_token）；显式空串=
  清除（配合切到 none）。用户名只在令牌仍在时更新；任何响应只给 `has_credential`，
  令牌与用户名（用户名虽不算秘密，也一并省掉）都不回读。

### 同步流程（后台任务 `repo_sync_task(owner_id, repo_id)`）

```
0. 进程内 _repo_sync_locks[repo_id] 加锁（与 _doc_index_locks 同形态、同单 worker 口径）。
   仓库行 pending/syncing，自开 SessionLocal（BackgroundTasks 的既有纪律）。
   准备 {repo_id}/known_hosts（从库里的 known_hosts 列生成）；auth_type=ssh_key 时
   解密私钥写 id_key(0600)，组装 GIT_SSH_COMMAND；auth_type=http_token 时解密令牌写
   http_cred(0600)，按上节组装 GIT_CONFIG_* 环境；finally 中 id_key / http_cred 必删。
1. 工作区不存在 → git clone --depth 1 --single-branch [--branch {branch}]（首次且未记录
   指纹时 GIT_SSH_COMMAND 临时换 accept-new，成功后回写指纹）
   已存在 → git fetch --depth 1 origin {branch} && git reset --hard FETCH_HEAD
   URL/分支被编辑过 → 先 rmtree 工作区重新 clone（指纹记录保留：主机通常没变；若主机随
   URL 一起换了，按 TOFU 对新主机重新记录）。
2. head = git rev-parse HEAD；git ls-tree -r -z HEAD 解析 (mode, type, sha, path)。
3. 一次查询取 SELECT path, blob_hash, id FROM kb_document WHERE repo_id=? 建账。
4. 三路对账：
   removed：账上有、树上没有 → delete_document（删 chunk 行 + 直接 await
            delete_by_doc 清向量；同步任务本身就在后台，不再套一层 BackgroundTask）
   added：树上有、账上没有 → 建 kb_document(pending) → 立即 index_document
   changed：两边 sha 不同 → 更新 content/size/blob_hash/language → index_document
   unchanged：跳过（这是省钱的关键：rebase 没碰到的文件零 embedding 调用）
   顺序执行、逐文件状态落库，复用 KB_EMBED_BATCH_SIZE 的批量节奏，不并发打供应商。
5. 写回 head_commit、last_sync_at、last_sync_result（计数），状态 ready。
   任一步未捕获异常：状态 error + error_msg，已经处理的文件保持各自状态（下次同步自愈）。
```

首次接入与手动同步走同一个任务函数；API 层只负责建行 + `background.add_task`。同步期间
前端能看到的进度就是文件行的 pending/indexing/ready 变化与仓库信息条的「同步中」，
不引入新的推送通道（没有 SSE，沿用轮询）。

### 崩溃恢复与删除

- 启动时新增 `reset_stale_repo_syncs(db)`，与 `reset_stale_indexing()` 同一点调用：
  卡在 `syncing` 的仓库行转 `error`「同步任务在服务重启时中断，请重新同步」；卡在
  `indexing` 的文件行沿用既有重置。blob 对账使得重跑天然幂等、可续跑。
- `KnowledgeService.delete_type` / `delete_project` 在级联删文档之外按 `type_id`
  删 `kb_repo` 行，但**不碰文件系统**（service 层没有 BackgroundTasks，也不该直接做
  磁盘 IO）。本地 `{repo_id}/` 目录的收尸走两条路：
  1. `DELETE /repos/{id}` 在 `service.delete`（等价删类型，复用同一套级联，避免两套
     逻辑漂移）之后，后台任务逐文档清向量 + `rmtree_repo_dir` 立即删目录；
  2. 启动时 `gc_orphan_repo_dirs(db)` 扫描数据目录，凡库里没有对应仓库行的一律 rmtree
     ——删类型/删项目路径与异常残留都靠它兜底。MySQL 行是事实来源，工作区只是缓存。
- Windows 上 `.git` 里的 pack/对象文件常带只读位，普通 `shutil.rmtree` 会留下半个目录
  导致重克隆撞 "destination already exists"；`_force_rmtree` 在错误回调里去只读位再试。

## 7. 接口设计（前缀 `/knowledge/repos`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/repos/keypair` | asyncssh 进程内生成一对 ed25519 deploy key，返回 `{public_key, private_key}`；**不落库**，仅供接入弹窗使用，私钥随创建请求提交后才加密存储 |
| POST | `/test-connection` | `{remote_url, branch?, private_key?, http_token?, http_username?}` → SSH 写临时 known_hosts（首次 accept-new）、http(s) 写临时 credential-store，跑 `ls-remote`，返回默认分支、HEAD SHA、主机名与**首次见到的 host key 指纹**（http(s) 指纹为空）；私钥/令牌只用于本次探测；**有向量模型闸**（探测本身不需要，但入口统一） |
| GET | `/repos?project_id=` | 项目下仓库列表（含状态、HEAD、`has_credential`、host 指纹、last_sync_result） |
| POST | `/repos` | `{project_id, remote_url, branch?, auth_type, private_key?, http_token?, http_username?, known_hosts?, host_fingerprint?, name?, exclude_globs?}`：按协议校验认证组合（SSH 只收私钥、http(s) 在 none/http_token 中二选一），asyncssh 导入私钥（拒绝带口令密钥）或 Fernet 加密令牌，建同名类型（重名加 `-2`）、建仓库行（连同 known_hosts 首记录）、排队首次同步；**有闸** |
| GET | `/repos/{id}` | 详情；404 口径同其余资源（不属本人 = 不存在）；另含可公开的 `ssh_public_key` |
| PUT | `/repos/{id}` | 可改 `name / branch / exclude_globs / private_key / http_token / http_username / auth_type`（密钥与令牌缺省=不变；令牌显式空串=清除；匿名/令牌互改时后端同步清理另一套凭据）；改分支 → 下次同步重建工作区；URL 不可编辑 |
| POST | `/repos/{id}/sync` | 增量同步；锁占用时返回 HTTP 200 + `{started:false, already_running:true}`（业务口径不排队，避免页面重试刷出一串任务） |
| POST | `/repos/{id}/retrust` | 主机指纹变更后的人工确认：重新 accept-new 探测并覆盖 `known_hosts_line`；记录操作者日志 |
| DELETE | `/repos/{id}` | 删类型/文档/向量/仓库行，best-effort rmtree 整个 `{repo_id}/`（含密钥文件） |

仓库文件只新增一个只读端点 `GET /documents/{id}/content`（全文 + `line_count`，
P1 源文件预览用）；列表、单篇重新索引、全量重建、检索预览全部复用既有接口，文档
视图里以 `source`/仓库名区分。批量删除接口对 `source='repo'` 的 id 跳过并在 `done`
口径里体现（仓库文件只能通过移除仓库或同步删除回收）。

## 8. 前端改动

- `Knowledge.vue`：
  - 「知识类型」卡片操作区加 **接入仓库**（禁用条件与上传一致：向量模型就绪 + 已选项目）。
  - 类型列表项：仓库类型加仓库图标/标签，描述行展示 `branch@shortsha` 与同步状态。
  - 选中仓库类型时，文档卡片上方渲染**仓库信息条**（URL、分支、SHA、状态、计数、上次
    同步结果的增删改跳），操作换成「立即同步 / 编辑 / 移除仓库」；上传与上传文件夹按钮
    对仓库类型隐藏；文档表格的单篇操作只留「重新索引」。
  - 接入弹窗：项目固定为当前项目；Git 地址、分支（占位「留空自动探测」）；认证单选项
    **由地址协议驱动**：SSH 地址只显示「Deploy Key」；http(s) 地址显示「匿名访问 /
    HTTPS 令牌」。SSH 下凭据二选一：粘贴私钥（多行 textarea，password 态）或「生成密钥
    对」（调 keypair 接口后展示公钥与复制按钮、提示去 Git 服务登记，私钥暂存在弹窗内存
    里随提交上传）；令牌下是 `a-input-password`（PAT，autocomplete=new-password，创建时
    必填）+ 可选用户名输入框（extra 说明缺省 `x-access-token`、Gitee 需真实用户名）。
    改地址协议时自动切到合法认证并清掉另一套凭据输入；「测试连接」成功后回填默认分支，
    SSH 还展示首次记录的主机指纹；排除规则；确认文案带费用提示。
  - 编辑弹窗：地址只读；令牌框永远空白（不回读），留空=不更换，切「匿名访问」保存即
    清令牌；信息条认证标签三态：Deploy Key（青）/ HTTPS 令牌（蓝）/ 匿名访问（默认灰）。
  - 主机指纹变更：同步失败的错误条在识别到 `Host key verification failed` /
    `REMOTE HOST IDENTIFICATION` 措辞时给出「确认变更指纹」二次确认（retrust，
    Popconfirm 文案提示先确认变更来自自己的 Git 服务器），成功后自动排队一次同步；
    编辑弹窗与接入弹窗均可复制公钥。
  - 轮询：`hasRunningJobs` 增加「当前项目存在 syncing 仓库」，复用现有 pollLoop；同步中
    同时刷仓库信息与文档表格。
  - 检索预览命中：标题渲染 `path:lineStart-lineEnd · symbol`，正文按语言做等宽/基础高亮
    （P0 只等宽字体，不引高亮库）；标题本身是链接，点开带行号的原文预览并滚动高亮命中行。
  - 源文件预览（P1）：文档表格里 `source='repo'` 的文件名渲染为链接，弹窗等宽展示全文，
    左侧行号 gutter（sticky，横向滚动不遮行号），头部显示语言/行数/体积/只读，无编辑下载入口。
- 对话引用卡（`MessageBubble.vue`）：代码来源显示路径 + 行号区间 + 「代码」标签 +
  符号名，等宽字体；无行号的老分片退化为文件名。
- `api/knowledge.ts` 增加 `repoApi`（`RepoAuthType = 'ssh_key' | 'none' | 'http_token'`）
  与 `RepoItem / RepoStatus / RepoSyncResult /
  RepoKeypair / RepoTestConnectionResult / RepoCreatePayload / RepoUpdatePayload` 类型
  （后两者含 `http_token / http_username`，注释标明加密存储、不回读）；
  `SearchHit`、`KnowledgeDocument` 增补可选字段；`api/chat.ts` 的 `Citation` 同样增补；
  P1 增加 `DocumentContent` 与 `knowledgeApi.documentContent`；`DocumentStatus` 不变。

## 9. 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `KB_REPO_DATA_DIR` | `./data/repos` | 本地根目录，布局 `{owner_id}/{repo_id}/{work,id_key,http_cred,known_hosts}`；含解密后的临时私钥/令牌，目录权限按 0700 创建；compose 挂卷且不与静态目录共享 |
| `KB_REPO_MAX_FILES` | 20000 | 单次同步收录的受跟踪文件数上限，超出跳过并计入报告 |
| `KB_REPO_MAX_FILE_BYTES` | 300000（约 300 KB） | 单源文件大小上限，超出跳过 |
| `KB_REPO_CLONE_TIMEOUT` | 300 | 单次 clone/fetch 子进程超时（秒） |
| `KB_REPO_SYNC_TIMEOUT` | 1800 | 整次同步上限（秒） |
| `KB_REPO_HOST_ALLOWLIST` | 空（不限） | 可选主机后缀白名单，逗号分隔，对 SSH 与 http(s) 同等生效 |

代码切片常量 `CODE_CHUNK_CHARS=1800`、`CODE_CHUNK_LINES=80`、块内重叠 10 行，
先做代码常量，沿用 `chunking.py` 不把切分参数环境化的取舍。Dockerfile 后端阶段
`apt-get install -y --no-install-recommends git openssh-client`（git 走子进程，
`ssh` 二进制由 openssh-client 提供；国内 apt 源沿用现有 USTC 配置）；asyncssh 已在
requirements 中（运维域在用），无新增 Python 依赖；docker-compose 给 `data/repos`
挂命名卷。

## 10. 升级行为

- `kb_repo` 由 `Base.metadata.create_all` 建新表；`docs/schema.sql` 同步补可审阅 DDL。
- `migrate_schema()` 幂等补列：`kb_repo` 的 `http_token_enc BLOB` / `http_username
  VARCHAR(128)`（HTTPS 令牌认证上线批次，inspector 查列守卫）；`kb_document` 的
  `source/repo_id/blob_hash/language`
  （`source` 带 DEFAULT 'upload'，老行即上传文档）、`kb_chunk` 的
  `line_start/line_end/symbol`，并在列宽不足时 `MODIFY filename VARCHAR(512)`。
  重复执行全部为空操作，SQLite 测试库由 `create_all` 直接得到新结构。
- 启动在现有 `reset_stale_indexing(db)` 之旁调用 `reset_stale_repo_syncs(db)`，并执行
  `gc_orphan_repo_dirs(db)` 清理无主工作区（删类型/项目路径的磁盘收尸）。
- 升级不触发任何向量重建：老 point 缺新 payload 字段时检索照常（新字段展示期缺省）。

## 11. 测试计划

新建 `backend/tests/test_knowledge_repo.py`（service + HTTP 两层，全程不依赖真实 SSH
服务端，59 例）：用临时目录里真实 `git init` 的非裸仓库（含多次 commit 模拟
added/changed/removed 与分支改名）模拟远端，monkeypatch `parse_git_url` 让它返回
canonical 为该仓库本地 posix 路径的 `GitTarget`（绕开 file:// 禁令与 Windows 盘符的
scp-like 解析问题），clone/fetch/ls-tree/rev-parse 全部跑真实 git 二进制；
`GIT_SSH_COMMAND` 拼装从 `env` 里断言，known_hosts/密钥文件的写入与清理、TOFU 状态机
用夹具与假 probe 断言。标记 `requires_git`，环境没有 git 时整组跳过：

- **切片器**：各语言族在函数/类边界切开且行号精确；长函数块内二次切分带重叠；情报头不计
  行号；二进制/NUL/超长行/超限文件跳过；glob 排除与默认排除（含锁文件、min 文件）；
  模块前缀只进首片。
- **对账**：added/changed/removed/unchanged 四路的索引调用次数（unchanged 零 embedding）；
  rename 表现为删旧增新；blob sha 直接取自 ls-tree。
- **同步任务**：首次 clone、fetch+reset 增量、URL/分支变更触发重 clone；单文件失败只标记
  该文件且仓库最终 ready；全局异常落仓库 error；同仓库并发同步第二把拿 409；重启重置
  syncing 行。
- **级联**：删仓库/类型/项目三条路径都删行、清向量、rmtree；只清本人数据，他人仓库 404。
- **SSH 凭据与指纹**：私钥 Fernet 加密落库、任何详情接口只见 `has_credential` 与公钥；
  同步时密钥落盘 0600 且 finally 必删（成功/异常/超时三条路径各断言一次）；
  `GIT_SSH_COMMAND` 含 IdentitiesOnly/BatchMode/专用 known_hosts，私钥不进 URL、不进
  agent；私钥格式非法（asyncssh `import_private_key` 抛错）在创建/更新时被拒。TOFU：首次 accept-new
  并回写指纹，之后指纹不匹配同步失败且不覆盖，retrust 显式更新；URL 主机段注入 `-o`、
  `file://`、`ext::`、换行均被拒绝；编辑仓库时私钥缺省=不变。
- **密钥对生成**：keypair 接口经 asyncssh 进程内返回 ed25519 公私钥且不落库；粘贴的私钥
  能自动导出公钥存入 `public_key`；全程不依赖环境里存在 `ssh-keygen` 二进制。
- **HTTPS 令牌**：令牌校验拒空白与原始串里的控制字符（`"t\npassword=evil"` 行注入）；
  凭据文件断言为一行 URL 形态（端口 `%3a` 编码、特殊字符百分号编码、LF 而非 CRLF），
  env 里只有 `store --file=` 路径，令牌不进任何 argv/env 值；真实 `git credential fill`
  能从 store 取回令牌且错主机拿不到；probe 的 env 间谍断言临时凭据随探测销毁；
  create/update 的协议×认证组合（SSH+令牌、http+私钥、匿名却带令牌、令牌缺失）五态拒绝、
  令牌替换/翻认证/清空生命周期；解密回读往返与 `has_credential` 口径，任何列表/详情/
  repr 不回显令牌；真跑本地 http 目标的令牌同步 e2e；clone 失败路径凭据文件仍被删除。
- **HTTP**：接入建同名类型并重名避让；无向量模型被闸挡；同步锁占用返回
  `already_running`（非 409）；分页/列表口径；
  批量删除跳过 repo 文档；keypair/test-connection/retrust 三端点的入参校验（非法协议、
  私钥不可读、非本人仓库 404），响应体任何位置不回传私钥。
- **检索与引用**：代码命中带行号与符号、`_format_hits` 新渲染、行号缺失退化老格式、
  scope 解析仍只靠 type_id、老 point（无新 payload）不受影响。

## 12. 已知边界与后续批次

- **单进程口径不变**：仓库同步锁是模块级 `asyncio.Lock`，同步跑在 FastAPI
  BackgroundTasks 中，与现有索引锁同一限制；多 worker 水平扩展前需要外置任务队列。
- **浅克隆单分支**：`--depth 1` 拿不到历史，无法回答「这段代码上个月改过什么」类问题
  （那需要 commit 图谱索引，是另一种产品形态）；tag/多分支、PR diff 预览留 P1。
- **切片器是规则级不是语法级**：宏、嵌套匿名函数、装饰器把一个顶层块变得很巨型时切分质量
  受限；P1 评估 tree-sitter（wheel 已基本无原生构建问题）替换边界识别，切片元数据结构不变。
- **同步是手动触发**：P0 不做定时与 webhook；后台轮询/GitLab 推送事件触发列入 P1，届时
  仓库表加 `sync_interval` / webhook 校验字段即可，同步引擎本身不用动。
- **认证覆盖「SSH 独立 deploy key」「匿名 http(s)」「HTTPS 个人访问令牌」三态**：
  不支持带 passphrase 的私钥（`BatchMode=yes` 下必然挂起，创建时直接拒绝并提示用无口令
  密钥）；不支持 OAuth 刷新令牌（PAT 过期后由用户手工更换）、自建 CA 证书的双向 TLS、
  SSH 跳板机（非标端口可在 URL 中表达，跳板配置不做）、本地服务器目录（运维域已有
  SFTP/服务器能力，可复用其凭据体系）——这些留后续批次。
- **TOFU 不是强身份认证**：它挡住「记录指纹之后」的中间人，挡不住首次连接即被劫持；
  安全要求高的环境后续提供「接入时粘贴指纹」模式（数据列已具备，只差表单）。
- **不做 LFS、不做子模块递归、不解析 svn:externals 类形态**；大文件靠浅克隆 + LFS
  跳过 + 文件上限三层兜底，clone 体积没有硬上限（Git 协议不支持先问大小），仅靠超时与
  同步后工作区体积告警。
- 仓库文件不能被单独删除（只能随仓库移除或 glob 规则变化在同步时对账删除）。「点开读原文件」
  已实施（2026-11，P1）：`GET /knowledge/documents/{id}/content` 返回只读全文与后端算好的
  `line_count`（不加向量模型闸门，读库不花 embedding 调用）；文档表格里仓库文件名与检索预览
  的代码命中位置都是链接，弹窗以行号 gutter 等宽渲染，检索命中跳转会滚动并高亮对应行。
  仍然不提供下载/编辑入口。
