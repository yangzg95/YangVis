# 站内自用模型设置（对话/向量独立配置、Fernet 凭据、连通性测试、思考三态）

菜单路径「系统设置 → 模型与集成 → 模型管理」。这里配的是**站内自用模型**：控制台的
对话、简历、幻灯片、知识库检索都从这里取模型。配置按用户保存（`model_config.owner_id`），
并按用途拆成 `chat` / `embedding` 两档，各自独立配置、各有一个默认。本站对服务商
不做任何假定，唯一的约定就是一套 OpenAI 兼容 HTTP API——一份配置无非是
`base_url` + `model_name` + `api_key`。把本站当成 OpenAI 接口对外供给是 AI 网关
（平台级、仅管理员）的职责，见 ai-gateway.md。

## 用户可见行为

- 页首信息条明确「这一页配的是站内自用模型……每个账号各配各的」，管理员可见
  「前往 AI 网关」按钮（`/system/ai-gateway`）；AI 网关页有同样的反向指路回
  `/system/settings`——两个入口一个站内自用、一个对外供给，互相免得找错。
- 新增时选用途：**对话**模型负责生成回答，**向量**模型负责知识库检索，两档互不可代用；
  某一档的第一份配置自动成为该档默认，之后同一用途只保留一个默认。列表默认在前、
  其余按 id 倒序，可用「全部 / 对话 / 向量」过滤。
- 表单字段：标题、模型名称、模型地址（存前剥尾斜杠）、密钥、备注。对话模型另有四项：
  思考过程三态、额外请求参数（JSON）、采样温度（0–100 整数）、图片理解勾选。
- 密钥在任何列表与响应里都只有掩码 `sk-****` 加尾四位（短于四位连尾四位都不给）；
  编辑时密钥框留空表示「不改已保存的密钥」。密钥解不开（加密密钥变更或数据损坏）时
  密钥列显示红色「密钥无法解密」，悬停给出「请重新填写」的错误，而不是假装没填过。
- 「测试」对真实服务商发一次请求，配错当场暴露：对话模型发一条最短 ping，
  向量模型发一句探测文本并**实测向量维度**（Qdrant collection 按它创建、建后改不了）。
  成功弹成功提示（对话模型还展示回了什么、向量模型展示维度），失败用长弹窗展示
  翻译过的人话错误（401→认证失败、404→检查模型名/部署 ID、超时、无法连接、429）。
- 状态列三态：可用（绿色，向量附「N 维」，悬停看最近测试时间）/ 测试失败（红色，
  悬停看失败原因）/ 未测试。「设为默认」必须先通过测试，否则业务拒绝。
- 换了 api_key、换了 base_url、或**真的改动了额外请求参数**，上一次测试结论立即作废：
  清测试态、清失败原因、清实测维度；若这份本来是默认，连默认位一起撤（但不替你顶上
  别的配置）。只挪「思考过程」开关、改图片理解声明、改标题备注，不会让测试失效。
- 删除配置时，引用它的会话（`chat_conversation`）与个人工作流步骤（`workflow_step`）
  的 `model_config_id` 一并清空，退回跟随默认；`workflow_run_step` 那一列是「当时
  实际用了哪份」的留痕，不清。删掉的若是默认，自动顶上同用途另一份**测试通过**的
  配置（按 id 倒序），没有就空着——宁可可读地「还没配模型」。
- 页面加载时同时拉就绪状态 `GET /settings/models/status`，对话/向量任一档没有测试
  通过的默认模型，顶部出警告条并带「立即添加」。真正发起对话时后端还有一道硬门：
  指定的配置必须是本人的、对话用途、且 `last_test_ok`，否则直接业务报错引导去配置。

### 思考过程三态（仅对话模型）

| 模式 | 线上行为 | 展示与落库 |
| --- | --- | --- |
| 自动（`auto`，默认） | 不发任何私有参数 | 装 `ReasoningChatOpenAI`：服务商在流式 `delta.reasoning_content` 里主动返回思维链，才把增量抬进 `additional_kwargs` 并展示；普通模型什么都不显示 |
| 开启（`on`） | 把「额外请求参数」原样并入请求体（langchain `extra_body`）索要思维链 | 同样经 `ReasoningChatOpenAI` 解析展示 |
| 关闭（`off`） | 不发私有参数 | 直接用原生 `ChatOpenAI`，非标准的 `reasoning_content` 在解析层就被丢弃，界面与落库 timeline 都不留思考节点 |

各家「开启思考」的字段名互不相同（`enable_thinking`、`thinking.type`、
`reasoning_effort` 等），本站不维护厂商字典、不猜字段：额外参数只留一个透传口，
由配置的人照服务商文档原样填 JSON 对象（占位例子 `{"thinking": {"type": "enabled"}}`），
前端先校验必须是 JSON 对象，填错会让这份配置直接调用失败，所以保存后要用「测试」验证。
参数只在 `on` 时上线；`auto`/`off` 即使库里留着参数也绝不发出去——发给不认识它的
服务商就是一句 400。

## 关键取舍

### 为什么只认 OpenAI 兼容、不做厂商字典

与某一家强相关的东西（向量维度、`model` 到底填模型名还是部署/接入点 ID）一律靠
真请求探测得出，而不是写死在代码里。这样新接入一家服务商不需要改一行代码；代价是
「模型名称」一栏可能要求填部署 ID，表单提示与连通性测试（含 404 的错误翻译）负责
把这种配错当场暴露。客户端每次调用现建而不缓存：配置按用户存、随时可改，长命客户端
会心安理得地继续用用户早已改掉的凭证。

### api_key 为什么用 Fernet，而且密钥不能轮换

密文落 `model_config.api_key_enc`（`LargeBinary`），`ENCRYPTION_KEY` 只存在于
环境变量、永不入库。没有默认值、不是合法 Fernet key 都拒启动（与 JWT 密钥同规则）：
一旦悄悄轮换，所有已存 key 都解不开，这比「启动失败」危险得多。解密失败时
`to_item`（API 往外递配置的唯一口子）不返回空串了事，而是带上 `api_key_error`
显式报出来——静默成空串和「用户从来没填过 key」长得一模一样，而两者的处置正好相反。

### 连通性为什么是一次真请求，探测还特意不带 temperature

正则/握手校验证明不了「地址 + 密钥 + 模型名放在一起能用」，把部署 ID 填成模型名
这类错误只有真发一次才暴露。探测不规定采样温度（传 `None`，langchain 会把
`temperature` 整个从请求体剔除）：Kimi 部分模型只接受 `temperature=1`、OpenAI o 系列
干脆不接受该参数，带 `0.0` 去探会把「其实能用」误判成连不通。向量探测的副产物
维度被持久化，建 Qdrant collection 与后续索引都以它为准。

### 改凭据为什么撤默认，删配置却顶上另一份

一份没重新验证过的配置顶着默认，坏掉的是这个用户接下来的所有对话，所以改 key/URL/
透传参数后宁可让默认位空着，由界面读出「还没配模型」，也不替用户挑一份没验证的。
删除则不同：用户的意图是「这份不要了」，此时顶上另一份**已验证**的配置最符合预期；
没有验证过的候选时仍然留空。

### 温度为什么模型级优先于智能体

「Kimi 只接受 1」是模型本身的属性，只该在模型配置设一次；让每个使用它的智能体各自
留空等于把模型约束摊给所有消费者。合并顺序是模型配置温度 → 智能体温度 → 都没有则
不传（交服务商默认）。详见 agents.md。图片理解（`supports_vision`）反过来由人按文档
声明、不做自动探测：把图片发给纯文本模型会被上游整条拒绝，而失败发生在用户按下
发送之后。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `ModelConfig` | 表 `model_config`：`owner_id` 索引、`purpose`、`api_key_enc` 密文、默认位、`vector_size`、`last_test_*`、`reasoning_mode`、`request_extra` JSON、`temperature`、`supports_vision` |
| `backend/app/models/schemas.py` | `ModelPurpose`（chat/embedding）、`ReasoningMode`（auto/on/off）、`ModelConfigCreate/Update/Item`、`TestResult`、`PurposeStatus`/`KnowledgeStatus`/`ReadinessStatus` |
| `backend/app/crypto.py` | Fernet `encrypt`/`decrypt`（解密失败抛 `DecryptionError`）、`mask`/`is_masked`（`sk-****` 掩码口径） |
| `backend/app/services/model_config.py` | `ModelConfigService`：所有读经过 `_scope(owner_id)`，越权读作 404；create/update/delete/set_default/record_test_result、`to_item` 打码；模块函数 `default_model_config` |
| `backend/app/services/providers.py` | `build_chat_model`（三态选客户端与 `extra_body`）、`build_embeddings`、`ReasoningChatOpenAI`、`resolve_temperature`、`test_chat_model`、`probe_embedding_dimension`、`_friendly_error` |
| `backend/app/routers/settings.py` | `/settings/models` 一组路由，service 在依赖里绑定调用方；`/status` 就绪汇总 |
| `backend/app/services/chat/service.py` | `require_chat_config`：对话发起前的硬门（本人/对话用途/测试通过，否则业务报错）；流式侧从 `additional_kwargs.reasoning_content` 取思考增量 |
| `backend/app/bootstrap.py` | `model_config` 四个新列的幂等 ALTER |
| `frontend/src/views/Settings.vue` | 模型管理页签：警告条、表格、新增/编辑弹窗、JSON 校验、测试与默认操作；顶部与网关互跳的信息条 |
| `frontend/src/api/settings.ts` | `settingsApi` 与类型；测试请求前端超时放宽到 60 秒 |
| `frontend/src/views/AiGateway.vue` | 网关页反向指路「控制台自用模型在模型与集成页」 |

### 接口（统一 `/api` 前缀，均需登录，全部限定本人配置）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/settings/models?purpose=` | 列出本人配置（可按用途过滤），密钥只回掩码 |
| POST | `/settings/models` | 新建；该用途第一份自动成为默认 |
| PUT | `/settings/models/{id}` | 部分更新；掩码/空串/未传的 api_key 都表示「不动它」；越权 404 |
| DELETE | `/settings/models/{id}` | 删除并清会话/步骤引用，必要时顶上验证过的默认 |
| POST | `/settings/models/{id}/default` | 设默认；未通过测试返回 `code=-1`，越权 404 |
| POST | `/settings/models/{id}/test` | 真发一次连通性探测并落测试结论；向量模型同时落实测维度 |
| GET | `/settings/models/status` | 对话/向量默认是否可用 + 知识库索引状态，供各页渲染准入门 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENCRYPTION_KEY` | 空（**必填**） | Fernet 密钥（32 字节 url-safe base64），非法拒启动；设定后不可轮换，否则全部已存 api_key 无法解密 |
| `MODEL_HTTP_TIMEOUT` | 30.0 | 调用用户自配服务商的出站超时（秒），对话/embedding/连通性测试共用；简历分析等一次性长输出的后台任务显式传更长的值 |

连通性测试的前端请求超时单独放宽到 60 秒（`settings.ts`），不受全局 30 秒限制；
思考三态与透传参数没有开关型环境变量，行为全部随每条配置走。

## 升级行为

- `migrate_schema` 对已存在的 `model_config` 表幂等补四列，缺哪列补哪列：
  `reasoning_mode VARCHAR(16) NOT NULL DEFAULT 'auto'`、`request_extra JSON NULL`、
  `temperature INT NULL`、`supports_vision BOOLEAN NOT NULL DEFAULT 0`。
- 存量行因此直接以「自动」模式复活：不发私有参数、服务商返回思考才显示，
  与老行为一致；SQLite 测试库由 `create_all` 直接建成新结构。

## 测试

`backend/tests/test_model_config.py`（文件型 SQLite，不连服务商）按八节钉住
「一份配置什么时候还算能用」：

1. 删除语义：会话引用清空后对话门退回默认；步骤定义清空而运行留痕保留；删别人的
   配置 404 且不顺手抹别人的行。
2. 默认顶上：删默认时只顶验证过的候选，没有就留空。
3. 隔离与排序：所有读带 owner + purpose；默认在前 + id 倒序；每 owner 每用途一个
   默认、切换不跨档传染；第一份自动默认；`default_model_config` 只回答查询不管可用性。
4. 掩码与密文：建时剥尾斜杠、视觉位往返；响应只有 `sk-****尾四位`、明文整个
   `model_dump` 里都不存在；库里是密文；短于四位不给尾号；解密失败必须带
   `api_key_error`。
5. 凭据轮换：掩码/空串/未传都不动密钥与测试态；换新 key 清测试态与维度；换 key 或
   换 URL 连默认位一起撤，同值（含尾斜杠差异）回传不误伤；用途可平移；越权更新 404。
6. 测试结论与默认：失败原因截断 512 字符、成功清错；维度只在实测出值时写；
   设默认必须先过测试。
7. 路由层：响应体只有掩码、明文与 `api_key_enc` 列都不上线；掩码经 HTTP 往返后
   原密钥存活；非法 purpose 是 422 统一信封；未验证配置设默认走 200 + `code=-1`；
   他人配置一律 404「模型配置不存在」。
8. 思考三态与温度：默认 auto 无参数；on 才装参数且参数真的进请求 payload
   （钉 langchain `_get_request_payload` 接缝）；auto/off 绝不发；off 用原生客户端、
   `reasoning_content` 在解析层被丢弃；只挪开关不使测试失效，参数真改动才失效，
   原样回传不算改；`request_extra` 的「未传保留 / null 清空 / 空对象等同清空」三分；
   温度 0–100 往返、null 清空、`resolve_temperature` 模型优先且 None 被请求体剔除。
