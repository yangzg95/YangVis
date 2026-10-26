# 知识库（RAG）

「知识库」页是一套自建的检索增强问答：用户把纯文本文件传到「项目 → 知识类型」两级
目录下，后台任务把文档切成带重叠的片段、调 embedding 模型向量化后写进 Qdrant；对话时
模型自己决定调 `search_knowledge_base` 工具检索，命中片段带编号进上下文，回答里标
`[1][2]`，界面再把编号还原成「哪篇文档的第几个片段」。整条链路（LangChain `OpenAIEmbeddings`
+ Qdrant，不用 langchain 的向量库封装）全部按用户隔离：MySQL 行带 `owner_id`，
Qdrant 里每个用户一个独立 collection。

## 用户可见行为

- 三级组织：**项目 → 知识类型 → 文档**。项目是检索边界：对话时先选项目（可再勾选类型），
  模型只能决定「查什么」，决定不了「查哪儿」。不同项目下可以有同名类型，文档互不可见。
- 上传仅收 `.txt / .md / .markdown / .csv / .json / .log / .yaml / .yml`，单文件上限
  10 MB；编码按 utf-8 / utf-8-sig / gb18030 / utf-16 依次尝试。支持「上传文件夹」：
  递归带入子目录，保留相对路径作为文档名（不同目录下同名 `README.md` 仍可区分），
  不支持的格式自动跳过并计数，开传前弹确认（会产生向量调用费用）。
- 上传请求只做「有界读取 + 文本落库」，切片与向量化在后台跑：文档状态为
  排队中（pending）→ 索引中（indexing）→ 就绪（ready，带分片数）/ 失败（error，
  悬停看原因）。页面在有任务时每 2.5 秒轮询，全部落定立即停表。
- **未配置可用向量模型时整页阻断**：顶部黄色警告条 + 「去配置」按钮，上传按钮直接禁用，
  而不是收下文件再报错。已建索引但当前默认向量模型与建索引时不一致时，红色条提示
  模型漂移，引导全量重建。
- 「检索预览」：在当前项目范围内试一句话，返回的分片（编号、文件名、第几片、cosine
  相关度、正文）就是对话时模型会读到的上下文；低于阈值的分片在向量库那一层就被丢弃，
  空结果直接说明「相关度低于阈值的分片会被丢弃」。
- 单篇「重新索引」、勾选后的批量重建/批量删除（一次请求最多 500 个 id，查无此文档的
  id 跳过，返回「请求了几个、成了几个」）、工具栏「重建索引」对全量文档删库重建
  （有二次确认，提示费用与「期间检索不可用」；库里没有文档时不启动）。
- 文档表格服务端分页（每页 8 篇，总数以后端为准）；工具栏常驻索引状态标签、文档数 /
  分片数 / 向量维度。失败的文档可以随时单篇或勾选中批量重试。
- 对话侧：项目（可选再加类型）随会话选定，开了知识库开关但本轮没选项目时，请求在进
  模型循环之前就被拒为「请先选择项目」（业务码 4004），不会退化成搜遍全库。模型自己
  决定检索几轮（ReAct 步数上限 8，约 3 轮「检索 → 再想」），引用处在句末标 `[n]`；
  编号在多轮检索间稳定，回答下方由界面自动展示来源片段，模型被明确要求不要自己附
  来源列表。检索不到时应直接回答「知识库中未找到相关内容」，不许推测。

模型配置（对话模型与向量模型各自独立、连通性测试实测向量维度）见 model-settings.md；
智能体上的知识库开关与对话侧校验见 agents.md。

## 关键取舍

### 为什么按用户分 collection

collection 的向量维度在创建时固定，而每个用户配置自己的 embedding 模型：两个用户维度
可能根本对不上；即使维度相同，不同模型的向量空间也互不兼容。共享 collection 意味着
任何人切换 provider 都要逼着全站重建。所以命名直接是 `kb_u{owner_id}`
（`vectorstore.collection_name()`），一个用户的索引生灭与他人无关。隔离的第二道、第三道
防线仍然保留：查询 filter 里照带 `owner_id`（今天冗余，是为了哪天改共享 collection 时
过滤条件早就在那儿），所有 MySQL 读写走绑定了 owner 的 `KnowledgeService._scope()`，
handler 拿不到裸 session，越权读一律返回 404 而不是 403（确认别人那行存在本身就是泄露）。

### 切片与重建口径：MySQL 文本是唯一事实来源

抽取出来的全文存在 `kb_document.content`（LONG TEXT），索引完也不删——换 embedding
模型时维度可变、同维度空间也不兼容，唯一可靠的迁移办法就是拿原文重新切、重新 embed。
Qdrant 里只存向量和精简 payload（`owner_id/doc_id/chunk_id/seq/filename/type_id`），
连分片正文都不存：检索命中后凭 `chunk_id` 回 MySQL 读文本，文档不存两份。单篇重建先删
旧 point 再写新的（point id 每次重新生成 UUID，不删会永久残留）；全量重建直接
drop 后 recreate collection（维度不可变），中途失败时没跑完的文档在 MySQL 里虽然还是
READY、向量其实已经没了，会被统一标成 ERROR「索引重建中断」，避免「就绪但检索不到」。

切分自己实现而不用 langchain-text-splitters：对中文真正要紧的规则很具体——按
`段落 → 中日韩句读（。！？；）→ ASCII 句末（". " 等）→ 空格` 的优先级在自然边界断开，
按字符数而非 token 计大小（CJK 字符大致一字符一 token，不值得为切片全量 tokenise）。
目标 700 字符、重叠 100（夹到不超过一半），重叠是为了让横跨边界的答案仍能被召回；
一堵没有分隔符的文字墙才硬切。

### 模型指纹：同维度不同模型也必须拦住

光比对维度不够：两个 1024 维的不同模型，Qdrant 会心安理得地拿 A 的 query 去和 B 的
point 打分，只返回一批看似自信、语义无关的结果。`kb_index_state` 存
`embed_model_sig = "{base_url}::{model_name}"`，每次检索前
`assert_index_usable()` 比对当前默认模型的指纹，不一致直接抛业务错误（code 4002）
要求重建；状态接口额外返回 `model_mismatch` 给前端亮条。embedding 返回维度与建 collection
时不符同样拒绝写入，防止把整个 collection 写坏。

### 向量模型为什么必须「先测试通过」

`get_embedding_config()` 认默认 embedding 配置，但有三个并列条件：存在、`last_test_ok`、
已探测到 `vector_size`。维度是模型配置页连通性测试时发一条 probe 实测出来的，不是
填配置的人手填、也不是按模型名猜的——维度猜错，建好的 collection 不做一次全量重建就
无法挽回。因此：没配/没测通模型时，上传、单篇与批量重建、全量重建、检索五个端点被
`require_embedding_ready` 依赖整体挡掉（FastAPI 依赖而不是每个 handler 各查一遍，新端点
必须显式 opt-out 才可能漏检），后台任务拿不到配置同样把文档落成 error。对话模型与
向量模型是两条独立的用途配置，互不影响；embedding 客户端用 LangChain 的
`OpenAIEmbeddings`（任意 OpenAI 兼容端点），并显式关闭 `check_embedding_ctx_length`：
部分兼容服务不接受它默认的 base64 encoding_format，而它用来预切分文本的 tokeniser
对非 OpenAI 模型也没有意义。

### 阈值过滤放在哪一层

低分丢弃下推给 Qdrant 的 `score_threshold`（cosine 距离），而不是取回 top_k 再在
Python 里过滤——否则阈值可能把名额全部吃光，实际拿到的有效结果远少于 top_k。默认
阈值 `KB_SCORE_THRESHOLD=0.3`（弱相关文本凑出的回答比不回答更糟），检索预览请求可以
带 0–1 的 `score_threshold` 临时覆盖；对话里的工具检索走默认值。检索范围解析也在
service 层：项目先换算成它名下的 type id 列表再下推（payload 里只有 `type_id`，且
项目概念上线前的旧 point 没有 project_id），显式传入的类型与项目范围取交集，过期 id
伸不到项目之外；范围解析为空列表时直接短路返回——空 MatchAny 反而会命中一切。

### 引用编号为什么要登记而不是现编

模型一轮回答里可能检索好几次（规则要求问题涉及多个方面时分开查），同一个 chunk 会被
重复命中。`_CitationRegistry` 按 `chunk_id` 给命中片段发全局稳定编号：第一次见到分配
下一个序号，重复命中沿用旧号——模型一旦在句子里写下 `[2]`，后面出现的 `[2]` 必须还是
同一份资料。喂给模型的段落渲染成 `[n] 来源：文件名\n正文`，编号由系统给，规则明确
禁止模型自行重编号、禁止末尾附来源列表（界面统一渲染）。回答结束后整份引用清单
（含 200 字摘录与分数）随助手消息落库，历史回放时来源还在，并通过 SSE `citations`
事件推给前端。

### 为什么入库放后台任务，而不是在上传请求里做完

几百 KB 文本就是几十次 embedding 供应商往返，同步做必然同时撑破前端的 axios 超时和
Gunicorn 的 worker 超时。上传端点只负责有界读取、抽取文本、落一行 pending 文档，然后
`background.add_task(index_document_task, ...)` 立即返回；前端没有推送通道，就靠
「还有 pending/indexing 文档时每 2.5 秒轮询、全部落定立即停表」跟进进度，keep-alive
切走标签页停表、回来先补一轮。批量重建同样排队进 BackgroundTasks——同一请求里的后台
任务串行执行，N 篇不会并发打满供应商；前端上传文件夹也刻意串行发请求，理由相同。

### 删除：行先删，向量后台收尸

删文档/类型/项目时 MySQL 行与 chunk 在请求内删除，Qdrant 的 point 由 BackgroundTask
按 `(owner_id, doc_id)` 过滤异步删除：行已经没了，Qdrant 抖动不该让一次成功的删除
变成失败；真有漏网的 point，检索时因对应 chunk 行不存在会被当作孤儿跳过。同一篇文档的
索引有进程内 `asyncio.Lock`（上传排队、手动重建、全量重建可能撞在一起），文档删除时
锁对象从表里摘除，避免只增不减。

### 后台任务的失败必须看得见

`index_document_task` 自开 session 运行（请求 session 早已关闭），业务拒绝和未知异常
都落成文档行的 error 状态与截断 512 字的原因，绝不让任务无声死掉。服务重启会把正在
跑的 BackgroundTask 一起带走，启动时 `reset_stale_indexing()` 把所有卡在 indexing 的
文档标成 ERROR「索引任务在服务重启时中断，请重新索引」——把永不结束的转圈变成可重试
的错误。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/knowledge/service.py` | `KnowledgeService`（绑定 owner）：项目/类型/文档 CRUD、索引状态与模型指纹、切片入库、单篇/全量重建、检索范围解析与检索；`index_document_task`、`reset_stale_indexing` |
| `backend/app/services/knowledge/vectorstore.py` | Qdrant 唯一访问层：collection 命名与创建（cosine）、point upsert/按 doc 删除/检索/计数/健康探测；AsyncQdrantClient 每次新建（避开 fork 后失效的 event loop） |
| `backend/app/services/chunking.py` | 跨域共用工具：`extract_text`（扩展名白名单 + 编码探测 + 归一化）、`split_text`（700/100、自然边界切分） |
| `backend/app/services/uploads.py` | 跨域共用工具：Content-Length 提前拒绝 + 256 KB 分块有界读取，超限即停 |
| `backend/app/routers/knowledge.py` | `/knowledge` 全部接口；`require_embedding_ready` 依赖统一给「离开向量模型就不能工作」的端点加闸 |
| `backend/app/services/providers.py` | `build_embeddings()`（LangChain `OpenAIEmbeddings`，关闭 ctx-length 预切分）、`probe_embedding_dimension()`（连通性测试实测维度） |
| `backend/app/models/entities.py` | `KbProject` / `KnowledgeType` / `KbDocument` / `KbChunk` / `KbIndexState` 五张表 |
| `backend/app/services/chat/service.py` | `_CITATION_RULES`、`_CitationRegistry`（跨轮次稳定编号）、`search_knowledge_base` 工具、进循环前的索引可用性校验、引用落库与 SSE `citations` 事件 |
| `backend/app/bootstrap.py` | `migrate_schema()` 补 `knowledge_type.project_id`、`_backfill_default_projects()`、启动调用 `reset_stale_indexing()` |
| `frontend/src/views/Knowledge.vue` | 知识库整页：三道状态条、项目/类型/文档表格、文件夹批量上传、轮询、检索预览 |
| `frontend/src/api/knowledge.ts` | `knowledgeApi`、上传扩展名白名单、业务码 4001/4002 |

### 表与向量负载

- `kb_project`：项目，唯一约束 `(owner_id, name)`。
- `knowledge_type`：类型，挂在项目下，索引 `(owner_id, project_id)`；`project_id`
  以 DEFAULT 0 上线是为老表无痛加列，启动回填后 0 不再出现。
- `kb_document`：文件名、mime、字节数、抽取全文（事实来源）、状态四态、错误信息、
  `chunk_count`；另预留 `scope`（恒 private）。
- `kb_chunk`：`(doc_id, seq)` 定位片段，`char_count` 与 Qdrant point 的 UUID 字符串
  `point_id` 都落库，单删 chunk / 单篇清向量都不用扫 collection。
- `kb_index_state`：主键即 `owner_id`，记 collection 名、embedding 模型 id 与
  `base_url::model_name` 指纹、向量维度、文档/分片计数、五态状态。
- Qdrant point payload 只放 `owner_id / doc_id / chunk_id / seq / filename / type_id`
  六个字段，不放正文；距离度量 COSINE。

### 数据流

```
上传：multipart → Content-Length 预检 → 有界读取(≤10MB) → extract_text 解码归一化
      → kb_document(pending，全文落库) → BackgroundTask
后台：split_text(700/100) → replace_chunks（旧 chunk 整批替换）
      → 删旧 point → ensure_collection(kb_u{id}) → 按 KB_EMBED_BATCH_SIZE 批量
        aembed_documents → upsert（payload 六字段）→ ready + 刷新索引计数
检索：aembed_query → Qdrant search（owner_id + type_ids filter + score_threshold）
      → 按 chunk_id 回 MySQL 读正文 → 跳过孤儿 point → SearchHit[]
对话：模型调工具 → service.search（范围闭包捕获，top_k=8）→ _CitationRegistry 编号
      → 编号片段喂回模型 → 回答中的 [n] → 引用随助手消息落库 + SSE citations → 界面渲染
```

### 接口（前缀 `/knowledge`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/projects`、`/projects/{id}` PUT/DELETE | 项目 CRUD；删项目级联删其下类型、文档，并后台清向量 |
| GET/POST | `/types`、`/types/{id}` PUT/DELETE | 类型 CRUD；GET 必带 `project_id`，不允许跨项目列表 |
| GET | `/types/{type_id}/documents` | 类型下文档，`page/page_size`（≤200）可选，total 恒为真实总数 |
| GET | `/documents` | 全量/按类型文档（兼容旧调用方） |
| POST | `/types/{type_id}/documents` | 上传（multipart `file` + 可选 `name`）；超限/不支持/空文本走 `code=-1` 信封；**有向量模型闸** |
| POST | `/documents/{id}/reindex` | 单篇重新排队索引；**有闸** |
| DELETE | `/documents/{id}` | 删除行并后台清向量 |
| POST | `/documents/batch-delete` `/documents/batch-reindex` | 批量（ids 1–500）；重建有闸，不存在的 id 跳过，返回 requested/done |
| GET | `/index/state` | 索引状态含 `model_mismatch`；**故意不加闸**，前端靠它解释为什么其余功能不可用 |
| POST | `/index/rebuild` | 全量重建（无文档时 `started=false`）；**有闸** |
| POST | `/search` | 检索预览：`query`（1–2000）、`top_k`（1–50，默认 8）、`project_id`、`type_ids`、`score_threshold` |

「有没有可用向量模型」的另一处来源是 `GET /settings/models/status`（`embedding.ready`），
知识库页挂载前先取它决定是否亮阻断条。业务错误码：4001 未配置可用向量模型、
4002 索引与当前模型不一致。

## 前端要点

- 三道状态条按优先级互斥：未配向量模型（黄，带「去配置」跳 `/system/settings`）→
  模型漂移（红，带重建按钮，文案显示文档/分片数并提示费用）→ 索引 error（红）；
  就绪与否取 `GET /settings/models/status` 的 `embedding.ready`，而 `/knowledge/index/state`
  不加闸，是为了在模型不可用时仍能拿到状态来解释原因。
- 轮询只在「存在 pending/indexing 文档」时挂 2.5 s 定时器，落定即摘；keep-alive
  `deactivated` 停表、`activated` 先静默补一轮再决定挂不挂，卸载也停。
- 列表请求有竞态守卫：切项目期间回来的旧响应整包丢弃；文档列表用自增序号
  `docsLoadSeq` 防止快速连点类型时慢响应覆盖新选择，被顶掉的请求也不许误关 loading；
  删空当前页时自动回退一页再拉。切项目/类型不带旧勾选与页码。
- 上传走组件自己的请求（`beforeUpload` 恒返回 false，不经 a-upload 通道）；文件夹在
  `beforeUpload` 的最后一次回调才合并成一批，先在前端按扩展名/大小/非空筛一遍，
  保留 `webkitRelativePath` 作文档名、超过 255 字符从前面截断；上传刻意串行，逐文件
  更新 `done/total` 进度，失败只前三条点名。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_TIMEOUT` | localhost / 6333 / 20.0 | Qdrant 连接；compose 内服务名为 `qdrant` |
| `KB_MAX_UPLOAD_BYTES` | 10 MB | 单文件大小上限；Content-Length 预检另留 1 MB multipart 余量 |
| `KB_EMBED_BATCH_SIZE` | 16 | 每批向量化的分片数，按批循环调 embedding 供应商（小于 1 归一为 1） |
| `KB_SCORE_THRESHOLD` | 0.3 | cosine 低分阈值，下推 Qdrant；检索接口可逐请求覆盖 |

切分常量 `CHUNK_SIZE=700`、`CHUNK_OVERLAP=100` 目前是 `chunking.py` 内的代码常量，
不经环境变量配置。embedding 客户端的 HTTP 超时与重试跟随模型配置的通用设置
（`MODEL_HTTP_TIMEOUT`、`max_retries=1`），没有知识库专属开关。

## 升级行为

- 已存在的 `knowledge_type` 表缺 `project_id` 时，启动幂等执行
  `ADD COLUMN project_id BIGINT NOT NULL DEFAULT 0`，随后 `_backfill_default_projects()`
  按 owner 各建一个名为「默认项目」（描述「迁移时自动创建，用于存放已有的知识类型」）的
  项目，把仍为 0 的类型归进去——不共用全局项目，因为跨用户的检索边界正是隔离要防的东西。
  重复执行是空操作。
- 每次启动调用 `reset_stale_indexing()`：上次进程被重启/崩溃带走、卡在 indexing 的
  文档统一转成可重试的 error（全站口径，不按 owner 收敛）。

## 已知边界

- 只收无需额外解析依赖的纯文本格式（扩展名白名单），PDF / Word 等即使在栈里有
  `pypdf` / `python-docx`（简历等域使用）也不放进知识库：在后台任务里才解析失败，
  等于用户早离开页面后才把错误报出来。上传文件夹时这类文件在前端就被跳过。
- 文档级索引锁是模块级 `asyncio.Lock`、索引跑在 FastAPI BackgroundTasks 里，两者都是
  单 worker 口径（当前单进程部署）；多 worker 下锁不互斥、任务不跨进程，水平扩展前
  需要外置队列。重启丢任务有启动重置兜底（见上），但不会自动续跑。
- `kb_document.scope` 是为未来的全组织知识库预留的列，当前恒为 `private`；Qdrant
  本身无鉴权，生产部署应按 README 建议删掉 compose 里它与 MySQL 的端口映射。
- 检索 top_k 对话内固定为 8（`RETRIEVAL_TOP_K`），只有检索预览接口可调；阈值可在
  预览时逐请求覆盖，对话工具始终走 `KB_SCORE_THRESHOLD` 默认值。

## 测试

`backend/tests/test_knowledge.py` 一个文件覆盖 service 与 HTTP 两层（Qdrant 与
embedding 全部以内存替身注入）：

- **隔离**：service 绑定单个 owner，列表/getter/删除计数全部只见本人行，他人行读作
  404、删除不影响他人行，类型不可能挂到别人的项目下，项目文档计数经类型 join 得出。
- **向量模型闸**：默认模型必须存在、`last_test_ok` 且探测到维度；缺失抛 4001；
  配置本身按 owner 解析。
- **模型指纹**：空索引允许任何模型；有向量后签名不同拒绝检索（4002）；漂移标记只在
  有向量且有可用模型时才亮起。
- **索引构建**：先写 chunk 再写向量、批大小跟 `KB_EMBED_BATCH_SIZE` 设置走、空文本
  跳过向量库、未探测到维度拒绝执行、provider 中途改变返回维度立即中止。
- **重建**：drop 重建 collection 并逐篇索引；中途失败把未完成文档标 ERROR；空库重建
  落成 ready。
- **后台任务**：正常重建、文档已删则静默返回、业务拒绝/未知异常都落 error 状态、
  等得到同文档的互斥锁；`reset_stale_indexing` 只动 indexing 行、不按 owner 收敛、
  无积压时 no-op。
- **检索**：无模型/模型漂移拒绝；无项目不过滤、项目解析成 type ids、显式类型去重并
  与项目取交集、空范围短路；阈值默认取设置、可被覆盖；正文从 MySQL 回读；孤儿向量
  跳过；collection 不存在返回空；上游异常照常抛出。
- **HTTP**：项目/类型 CRUD 与 404；上传落库排队、不支持扩展名/空文本/Content-Length
  超限/读超上限停止、无模型返回引导码；分页与真实 total；单篇与批量重建（他人 id 跳过）、
  批量删除只计数真实删除；Qdrant 宕机时删除接口仍成功；删类型/项目触发后台向量清理；
  重建仅在有文档时启动、无模型返回引导码；非法请求体保持 `code=-1` 信封。

切片函数本身在这些用例里以 monkeypatch 替身出现（测的是编排不是切分算法）；共用
`chunking` 的格式拒绝在 `test_resume_service.py` 另有断言。
