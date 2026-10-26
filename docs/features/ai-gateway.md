# AI 网关：统一密钥与多通道路由

网关让外部系统拿一把本站签发的 `sk-yv-*` 密钥，按标准 OpenAI 协议访问背后挂着的
多家厂商：管理员配置「上游通道」（base_url + 上游密钥）和「模型路由」（对外模型名
→ 某通道上的上游模型），同一个对外名可以绑多条通道做优先级故障转移；不想让调用方
关心模型名时可以点虚拟模型 `auto`，网关按请求特征和路由上声明的能力标签现场挑一条。
每次对外调用都留一条审计日志（通道、模型、token、耗时、首字延迟、IP、重试轨迹），
通道与日志都是**平台级资产**，不像「模型与集成」那样按用户隔离——整个模块只有
管理员能进。

## 用户可见行为

### 管理员侧（系统设置 → AI 网关，五个页签）

- **概览**：可复制的接入地址 `<origin>/v1`（配了 `AI_GATEWAY_PUBLIC_ORIGIN` 以它为准）
  与 curl 示例；1/7/30 天窗口的调用量、成功率（含失败数）、token 合计、平均/最长
  耗时、按天柱状趋势，以及按通道 / 模型 / 密钥三张分布表；正文留存关闭时页顶明示。
- **上游通道**：名称（唯一）、OpenAI 兼容根地址（通常以 `/v1` 结尾）、上游 api_key
  （Fernet 加密落库、列表只回掩码）、支持的模型名清单（仅作配路由时的候选提示，
  不参与转发判定）、启用开关、备注；「测试」按钮真发一条最短 prompt，结果与时间
  留在行上。删除通道会连带删掉指向它的路由（有二次确认）。
- **模型路由**：对外模型名、通道、上游模型名（留空表示与对外名一致）、优先级
  （0–9999，小者先试，默认 100）、启用开关；「参与 Auto 路由」开关展开后是
  **能力**（chat / embedding / vision / tools，至少勾一个）与**擅长的任务**
  （reasoning / code / writing / translation / general，选填）两组标签。
- **统一密钥**：新建即生成 `sk-yv-` + 43 字符随机串，**明文只在创建成功的弹窗里
  出现一次**，列表只有前 12 位 + `****`；行上展示调用次数与最近调用时间，可停用
  （立即生效）/ 删除。忘了明文只能新建一把。
- **调用日志**：按时间区间、密钥、通道、成败、关键字（request id / 模型 / 密钥名 /
  通道名 / 错误）过滤分页；点行开抽屉看完整字段、Auto 决策标签、故障转移轨迹表与
  截断后的请求/响应正文（紧凑 JSON 自动缩进展示）；可清理 N 天前历史。

### 调用方侧（OpenAI 兼容，挂在站点根的 `/v1`，不带 `/api`）

- `POST /v1/chat/completions`：`stream:true` 时 SSE 原样透传（`X-Accel-Buffering:
  no`，帧边界插心跳），非流式返回上游原响应；请求/响应都不套本站统一信封。
- `POST /v1/embeddings`：只支持非流式（客户端传 `stream` 会被剥掉）。
- `GET /v1/models`：列出当前「启用路由 + 启用通道」上的全部对外模型名（去重排序），
  只要存在一条启用的「参与 Auto」路由，列表里就有 `auto`；一条候选都没有时不列。
- 鉴权只认 `Authorization: Bearer sk-yv-*`，与控制台 JWT 互不通用。错误体一律
  OpenAI 结构 `{"error": {"message", "type", "param", "code"}}`。凡产生日志行的
  响应都带 `X-Yangvis-Request-Id` 头（值为 `uuid4().hex`），报障凭它定位日志；
  认证阶段被拒、`GET /v1/models` 没有这个头。

### 错误码（全部与代码一致）

| 场景 | HTTP | error.type | error.code |
| --- | --- | --- | --- |
| 缺 Authorization / 格式不对 | 401 | `authentication_error` | `invalid_api_key` |
| 密钥不存在（与缺失给同样笼统口径，防试探） | 401 | `authentication_error` | `invalid_api_key` |
| 密钥已停用 | 403 | `permission_error` | `key_disabled` |
| 请求体不是合法 JSON / 不是对象 / 缺 `model` | 400 | `invalid_request_error` | `missing_model`（缺 model 时） |
| 点名模型没有启用路由 | 404 | `model_not_found` | `model_not_found` |
| auto 没有满足硬要求的候选 | 404 | `model_not_found` | `auto_model_not_found` |
| 入站带着网关戳（回环） | 508 | `server_error` | `gateway_loop_detected` |
| 上游全部连不上 | 502 | `upstream_error` | —（网关生成的错误体） |
| 上游返回 4xx/5xx | 原状态码 | 原样透传上游错误体，不重写 |

## 关键取舍

### 为什么故障转移只在首字节发出前切换

SSE 一旦开始往客户端写，就没法把已经吐出去的半截回答收回来换一家重讲。所以两条转发
路径都把转移判定严格限制在「上游响应头阶段」：非流式（`forward_json`）整响应读完才
返回，换通道对调用方完全透明；流式（`open_stream`）只有在响应状态码 ≥400、一个字节
都还没透传时才可能换通道，一旦 200 建立成功就交给 `relay_stream` 原样透传到底，中途
上游断流只在日志里记下 `上游流中断`，不会再换通道重试（客户端可能已经渲染了一半）。

值得换通道的状态码是白名单 `FAILOVER_STATUS = {401, 403, 404, 408, 429, 500, 502,
503, 504}`：鉴权失败、限流、网关类错误说明「这条通道现在不行」；连接异常
（`httpx.HTTPError`，attempts 里记 `status_code: 0`）同样换下一条。400/422 不在其中：
参数非法换谁都一样，重试只是成倍拖长失败。所有候选都失败时，把**最后一次**的状态码
与错误体回给调用方。每次失败的尝试（通道、状态码、错误前 512 字、本次耗时）都进
日志的 `attempts`，最终应答的通道才写主字段；候选总数还受 `AI_GATEWAY_MAX_ATTEMPTS`
（默认 3）截断，防止给一个名字绑十几条通道时雪崩式连打。

流式还有一个内嵌的降级：请求本身没带 `stream_options` 时，网关自动注入
`{"include_usage": true}` 好从收尾帧里拿 token 数；个别上游不认这个字段回 400/422 且
错误体提到 `stream_options`，就在**同一条通道**上撤掉该字段重发一次（不算通道故障、
不进 attempts），而不是误判整条通道不可用。

### 为什么分类失败退回规则，而不是新增一个故障点

`auto` 的判定分两层，确定性不同（见 `auto.py` 模块头）：硬要求用规则（零成本、可
复述、可单测），任务类型才问模型。分类这一步在调用方的关键路径上，任何失败——
连接/超时（独立的 8 秒超时）、上游 ≥400、回答不是合法 JSON、回答里认不出词表中的
任何一个标签——统一退回「按 priority 选」，并把退回原因写进决策快照的 `source`
（`llm_failed` / `llm_unclear`），绝不把整次调用拖死。刻意不做「最接近的任务」这类
模糊匹配：判错类型会把请求分给更不相干的模型，而退回优先级是零风险的。这一步还可以
用 `AI_GATEWAY_AUTO_LLM=false` 整体关掉，auto 退化为纯规则 + 优先级。

三道闸门保证「不多问、不乱问」：只在 chat 端点问（embeddings 没有任务类型一说）；
候选池里没有任何路由声明任务标签时不问（问了也排不出先后，白烧一次钱）；取最后一条
用户消息（多轮里前面的内容上一轮已分类过），且截断到 1200 字。探针固定问候选池里
优先级最高的那条——任务类型还没定，排序无从谈起，而它反正必须能接 chat
（`max_tokens=8, temperature=0, stream:false`）。这次问答是网关替调用方做的决定，
**不写调用日志**，否则 SDK 每次拉模型列表/分类请求会把真正要审计的调用淹掉。

### 两层防自环各自防什么

通道 base_url 指回网关自己时，请求打到 `/v1` 又转发回 `/v1`，会无限递归到超时并刷爆
日志。两层防的是两类不同的配错：

1. **保存时校验（`normalize_base_url`）防手滑**：只接受 http/https + 有主机名；
   拒绝三类「自己」——管理请求的 Host 头（概览页显示给管理员复制的正是本站地址，
   粘回来是最容易犯的错，带端口也认）、回环集合 `localhost / 127.0.0.1 / ::1 /
   0.0.0.0`、`socket.gethostname()` 本机名。同主域的另一个子域不算自己。
2. **运行时戳记（hop 头）防认不出来的别名**：compose 服务名、内网 IP、公网 IP 指回
   本站这类配置，保存时的 host 校验无从知晓。每个出站请求都盖
   `X-Yangvis-Gateway: <request_id>`，入站再看到这个头，立刻在转发之前回 508
   `gateway_loop_detected`。508 刻意不在 `FAILOVER_STATUS` 里：换一条通道照样绕
   回来。被掐断的这一圈**不写日志行**（触发它的外层调用会把这个错误原样记下来）。

### 密钥为什么只存摘要

网关密钥是交给外部系统长期持有的凭据，泄露面比控制台密码大（可能落在对方的配置文件、
CI 日志里）。库存 SHA-256 摘要（`hash_api_key`，对 strip 后的明文取散列，header 里
多个空格不影响校验），数据库被读到也无法反推明文，「数据库泄露」不会直接升级成
「对外接口被冒用」；代价就是明文无法找回，只能新建/停用。列表里保存明文前 12 位
（`sk-yv-` 加 6 个字符）仅是为了让管理员认出「这是哪把」。上游通道的密钥性质不同
（网关自己要拿去用，必须可还原），所以走 Fernet 加密 + 接口回掩码，与 model_config
同一套约定；空串或掩码回传都表示「不修改」，只有换成新明文才会作废上一次连通性测试
结论。两条凭据在转发时严格隔离：出站 Authorization 永远换成通道自己的密钥（解密为空
时用 `not-needed` 兜底内网无鉴权上游），调用方的网关密钥绝不透传。

### 日志写失败为什么吞掉

网关已经在业务请求的同步路径上，审计日志写不进去是运维问题（磁盘满、库抖动），把它
变成调用方看到的 5xx 等于让监控故障扩大成业务故障。`write_call_log` 捕获一切异常、
rollback 后只打 error 日志。流式响应更特殊：落库发生在 `relay_stream` 的 `finally`
（流结束时请求作用域的 session 早已关闭），所以 `persist` 自己开一个
`SessionLocal`，日志写入与密钥 `call_count / last_used_at` 回填在同一事务里。客户端
提前断开也走 finally：已吐出文本则本次调用算成功（`success = bool(text)`），错误记为
「客户端提前断开连接」，已产生的内容与统计不丢。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/ai_gateway/service.py` | 「管」：密钥生成/散列/ Bearer 解析、`normalize_base_url` 自环校验、`resolve_candidates` / `auto_routes` / `list_public_models` 候选查询、`AiGatewayService`（通道/路由/密钥 CRUD、日志过滤分页与清理、stats/overview 聚合）、`truncate_text`、`write_call_log`；端点常量、`AUTO_MODEL_NAME` 也定义在此 |
| `backend/app/services/ai_gateway/proxy.py` | 「通」：`GatewayError`（OpenAI 错误体）、`CallRecord` 记账本、候选挑选与 auto 入口、`forward_json` / `open_stream` / `relay_stream`、故障转移白名单与 hop 头、SSE 旁路解析 `StreamAccumulator`、usage/缓存命中提取、`persist`、通道连通性 `test_channel` |
| `backend/app/services/ai_gateway/auto.py` | 「选」：请求特征规则（`has_image` / `wants_tools` / `required_tags`）、待分类文本与分类 prompt、回答解析 `parse_task_kind`、`_feasible` 严格筛选、`resolve_auto` 稳定排序与决策快照；本模块不发网络请求，分类器由 proxy 注入 |
| `backend/app/routers/ai_gateway.py` | 管理端 `/api/ai-gateway/*`：路由级 `Depends(require_admin)`；ValueError→400、LookupError→404；概览地址按 `X-Forwarded-Proto`/Host 推导 |
| `backend/app/routers/ai_gateway_openai.py` | 对外 `/v1` 三端点：`_reject_loop` / `_authenticate` / `_read_body` 前置；OpenAI 错误结构、`X-Yangvis-Request-Id`；models 与分类请求不落日志 |
| `backend/app/models/entities.py` | `AiChannel`（api_key_enc Fernet、protocol 预留、测试结论三字段）、`AiModelRoute`（唯一约束 `(model_name, channel_id)`、priority、auto_eligible/auto_tags）、`AiApiKey`（key_hash 唯一、key_prefix、计数）、`AiCallLog`（名称快照、auto_route、attempts、token/缓存/耗时/首字/IP/正文） |
| `backend/app/models/schemas.py` | `AiModelCapability` / `AiModelTaskKind` 枚举与 `AI_AUTO_TAGS` 词表（拼写错标签直接 422，避免「永远选不上且无方向排查」）、各 Create/Update/Item  schema、`AiCallLogItem`（无正文大字段）/ `AiCallLogDetail`、`AiStats*`、`AiGatewayOverview` |
| `backend/app/config.py` | 9 个 `AI_GATEWAY_*` 设置（见配置节） |
| `docs/schema.sql` | 四张表的 MySQL DDL（`ai_channel` / `ai_model_route` / `ai_api_key` / `ai_call_log`） |
| `backend/app/services/dashboard.py` | 首页管理员卡片：通道总数、今日调用数、今日 token（自然日零点起，全局口径，仅管理员） |
| `frontend/src/views/AiGateway.vue` | 五个页签整页；非管理员显示 403 结果页；能力/任务两个多选底下绑同一个 `auto_tags` 数组；前端先拦一次保留名 `auto`；日志抽屉渲染决策标签与 attempts 表、缓存命中率 |
| `frontend/src/api/ai-gateway.ts` | `aiGatewayApi`：管理端全部接口与 TS 类型（含 `AiAutoTag`） |

### 一次转发的生命周期（chat/completions）

1. **前置**：`_reject_loop` 查 hop 头 → `_authenticate` 解析 Bearer、按 SHA-256 查
   `ai_api_key`、查停用 → `_read_body` 解析 JSON 对象。这三步任何一步失败直接回
   OpenAI 错误体，**不建 record、不写日志、不带 request id**。
2. **建账**：`_prepare` 生成 `request_id`，记 `client_ip`、model、stream，请求体以
   紧凑 JSON（`ensure_ascii=False`）存进 `request_body`。
3. **选路**：点名模型走 `resolve_candidates`（join 通道，路由与通道都启用，按
   priority、id 升序），截断到 `MAX_ATTEMPTS`，空则 404 `model_not_found`；
   `auto` 走下一节的流程，空候选 404 `auto_model_not_found`。网关自身的拒绝也落
   日志并带 request id。
4. **转发**：请求体的 `model` 改写成 `upstream_model`；出站头只有
   `Authorization: Bearer <通道密钥>`、`Content-Type` 和 hop 头，调用方其余头不透传。
   非流式逐个候选尝试；流式先建流再透传（成功响应头阶段仍可换通道）。
5. **透传/记账**：非流式从响应里取 `usage`（兼容三种缓存字段写法，没报缓存留 NULL，
   与「报了 0 命中」区分；只给分项时总数用加法兜底）与助手文本（多模态分段拼文本）；
   流式由 `StreamAccumulator` 旁路逐行解析 SSE（按 `\n` 分包容忍 TCP 切断与非 JSON
   噪声行），首个 chunk 到达记 `first_token_ms`，透传字节本身不重新序列化。
   embeddings 只存 `{count, dimensions, model}` 摘要，几万维向量不入库。
6. **落库**：`persist` 用独立 session 回填密钥计数并写 `ai_call_log`；正文按
   `AI_GATEWAY_LOG_MAX_CHARS` 截断并追加 `…[已截断]`，`AI_GATEWAY_LOG_PAYLOAD=false`
   时两个正文字段直接为 NULL。上游错误体（非流式/建流失败）原样回给调用方。

### auto 决策流程

1. `is_auto_model`：大小写、首尾空格不计较（`" Auto "` 也算）。
2. **硬要求（规则，严格子集匹配）**：embeddings 端点要求 `embedding`；chat 一定要求
   `chat`，消息分段里出现 `image_url`/`image` 加 `vision`，请求带非空 `tools`、
   历史里有 `role:"tool"` 或 assistant 的 `tool_calls` 加 `tools`（空 `tools` 数组
   不算，很多客户端无脑带这个键）。畸形 body 不会让规则崩。
3. **可行候选**：`auto_routes()` 的口径与 `resolve_candidates` 完全一致（同样要求
   路由+通道双启用、同样 priority/id 排序），再用「auto_tags 包含全部硬要求」筛；
   缺任一硬要求的路由不许「大概也行」地混进来。
4. **任务排序（可选一问）**：条件全满足时问优先级第一的候选一个词，回答按词表包含
   匹配（reasoning/code/translation/writing/general 的顺序认）；认出来则用 Python
   稳定排序把「声明了该任务标签」的候选整组提前，组内仍是 priority 说话。
5. 决策快照 `{required, task, source, matched}` 在判空之前就挂到 record 上——404 的
   日志也带着「要什么标签、池子里有几条」；真正应答（含故障转移后改道）时补
   `resolved_model`，日志记的是「实际打了谁」。随后候选同样受 `MAX_ATTEMPTS` 截断、
   同样在池内故障转移。

## 配置

均为后端环境变量（`backend/app/config.py` Settings）：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AI_GATEWAY_CONNECT_TIMEOUT` | `10.0` | 连上游的连接超时（秒），转发与分类请求共用 |
| `AI_GATEWAY_READ_TIMEOUT` | `300.0` | 转发读超时（秒）；httpx 口径是「两个字节之间」的间隔，不是整条流总时长，流式回答等几十秒正常 |
| `AI_GATEWAY_LOG_PAYLOAD` | `true` | 是否把请求/响应正文写进日志；false 时只剩状态码、token、耗时等元数据（正文可能含调用方业务数据） |
| `AI_GATEWAY_LOG_MAX_CHARS` | `8000` | 单个正文字段入库字符上限，超出截断并加 `…[已截断]`；0 表示不截断 |
| `AI_GATEWAY_MAX_ATTEMPTS` | `3` | 一次调用最多尝试几条通道；1 等于关闭故障转移，auto 候选同样受限 |
| `AI_GATEWAY_PUBLIC_ORIGIN` | 空 | 对外公布的 origin（如 `https://ai.nanwa.xyz`），概览接入地址以它为准；留空按管理请求的 scheme/Host 推导，`/v1` 由程序拼 |
| `AI_GATEWAY_AUTO_LLM` | `true` | auto 是否允许发那一次任务分类问答；false 时纯规则 + priority |
| `AI_GATEWAY_AUTO_CLASSIFY_TIMEOUT` | `8.0` | 分类请求独立读超时（秒），比转发窄得多：宁可退回优先级也不拖住调用 |
| `AI_GATEWAY_AUTO_CLASSIFY_CHARS` | `1200` | 喂给分类器的最后一条用户消息截断长度 |

## 安全边界

- **管理面**：整个管理路由器挂 `require_admin`，普通用户前端直接显示 403 面板；
  通道/密钥/日志没有 `owner_id`，是全局口径，不存在「按用户隔离」这层语义。
- **凭据**：对外密钥库存 SHA-256、明文一次性；上游密钥 Fernet 加密、接口只回掩码；
  出站绝不携带调用方的 Authorization；`ENCRYPTION_KEY` 轮换会让全部上游密钥解不开
  （列表以 `api_key_error` 明示），与全站加密设置同生共死。
- **暴露面**：`/v1` 挂在站点根、无 JWT、只认 bearer 密钥，等于把上游模型额度开放给
  持钥方——需要按需签发、随时停用；网关自身不做按密钥的限流、配额或可访问模型范围
  约束，这些要靠上游额度与反代层补。
- **审计内容**：正文默认入库且可能含敏感业务数据，可用 `AI_GATEWAY_LOG_PAYLOAD`
  关闭或在日志页定期清理；`key_name`/`channel_name` 是写入时快照，密钥或通道删除后
  历史仍读得懂，外键 id 可以为空。
- **自环**：保存校验认不出内网别名/compose 服务名，这部分全靠 hop 头运行时兜底；
  部署文档要求 nginx 为 `/v1` 单独转发、关 SSE 缓冲、放宽读超时。

## 测试

- `backend/tests/test_ai_gateway.py`（纯函数 + SQLite 内存库的服务层）：
  - 密钥：生成唯一、前缀长度、散列可重算且忽略首尾空白；Bearer 解析认大小写 scheme、
    拒绝 Basic/空 token/无头；
  - 截断：追加 `…[已截断]`、上限 0 表示不截断；`CallRecord.to_entity` 对 5 万字符
    正文截断；
  - 自环校验：本站 Host（含带端口）、localhost/127.0.0.1/[::1] 全拒，真实上游与同主域
    其他子域放行，ftp/无 scheme 拒绝；
  - 响应解析：助手文本（字符串/多模态分段/畸形）、usage 加法兜底、三种缓存字段路径、
    非数字缓存值按未上报处理；`FAILOVER_STATUS` 不含 400/422；
  - SSE 旁路：在 UTF-8 字符与 JSON 中间硬切的分包也能拼出「你好」与 usage，
    keep-alive/坏 JSON 噪声行被忽略；
  - 服务层：通道密钥掩码不泄露、掩码回传不改凭据且保留测试结论、换新明文作废测试
    结论；自环拒绝时不改写原 base_url；路由必须挂在存在的通道上；候选按 priority
    排序并跳过停用路由/停用通道；upstream_model 映射与上游密钥解密；密钥生命周期；
    删通道级联删路由；日志过滤/关键字/分页倒序/统计四组聚合/清理；`write_call_log`
    吞掉 commit 异常；overview 计数；`public_base_url` 两种 origin 口径。
- `backend/tests/test_ai_gateway_proxy.py`（httpx.MockTransport 伪上游的端到端）：
  - 转发与留痕：打优先级首条、出站 Authorization 换成通道密钥且不含网关明文、
    hop 头值等于响应 request id、token 与缓存命中落库、密钥计数回填；
  - 模型名改写；502 自动转移且 attempts 留下失败行；400 只打一次且错误体原样返回；
  - SSE：`include_usage` 注入、字节原样透传、首字延迟、旁路攒出文本与 usage、
    `X-Accel-Buffering: no`；上游不认 `stream_options` 时同通道撤字段重试一次；
  - embeddings 转发与 `{count, dimensions, model}` 摘要；models 只列启用路由；
  - 404 `model_not_found` 也落日志且响应头能指回该行；缺头/错钥/JWT 三种 401 不写日志
    且不给 request id；停用密钥 403 `key_disabled`；带 hop 头入站回 508
    `gateway_loop_detected`、零出站、零日志；全部通道连接失败回 502 且两条尝试都在；
  - auto：带图只打 vision 路由且不发分类请求；先问最便宜路由再打 code 路由且全程只有
    一行日志；无 vision 候选回 404 `auto_model_not_found`（message 含缺失标签、
    matched=0）；池内 502 故障转移后 `resolved_model` 改写为实际应答者；有人勾选后
    auto 才进 models 列表；auto embeddings 直接选 embedding 路由、不分类。
- `backend/tests/test_ai_gateway_auto.py`（纯规则 + 内存库候选池，分类器一律假注入）：
  - auto 名宽松匹配；标签规范化/小写去重/词表外 422；参与 Auto 至少一个标签、关闭时
    标签归 NULL；
  - 硬要求：纯 chat、图片加 vision、tools/tool 角色/tool_calls 加 tools、空 tools
    不算、embeddings 要 embedding、垃圾 body 不崩；
  - 分类文本取最后一条用户消息（含分段消息）；回答只认词表、不做模糊归类；分类输入
    按 `AI_GATEWAY_AUTO_CLASSIFY_CHARS` 截断；
  - 候选池：保留名 `auto` 建路由被拒（创建与改名都拦）；auto 按候选有无进出 models
    列表；停用路由/通道出池；能力严格筛选；任务标签压过 priority 但只在匹配组内；
    池内无任务标签时一次探针都不发；探针固定看到 priority 第一条；分类抛错→
    `llm_failed`、答非所问→`llm_unclear`，均退回优先级；`AI_GATEWAY_AUTO_LLM`
    关闭后不发探针；embeddings 永不分类；编辑时只改开关也要保证「进池就有标签定义」；
    候选携带解密后的上游密钥。
