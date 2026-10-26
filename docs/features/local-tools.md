# 对话工具：本地工具与 MCP 服务

对话页「选择工具」里能挂给模型的工具有两个来源：**本地工具**是后端代码里的 Python
函数，用 `@local_tool` 注册、启动时自动发现与校验，代码是唯一事实源，不入库、没有
后台 CRUD；**MCP 服务**是用户在「AI 构建 → MCP 服务」页自己配置的外部 Model Context
Protocol 服务（stdio / SSE / Streamable HTTP 三种传输），配置入库、按用户隔离。
两类选择都在会话级勾选并记住，发问时本地函数直接在后端进程里执行，MCP 工具则随每次
回答现连现关。

## 用户可见行为

- 对话输入框上方一个「选择工具」多选（可搜索），分「本地工具」「MCP 服务」两个分组：
  本地工具显示代码里注册的中文标题，MCP 分组只列出**启用中**且属于本人的服务。
- 勾选随会话记住，存 `chat_conversation` 的 `local_tool_keys` / `mcp_server_ids`
  两个 JSON 列，与智能体、模型、项目同口径：重开会话自动恢复，后续追问沿用；恢复时按
  现存目录裁剪，已下线的本地工具、被删除或停用的 MCP 服务自动从选择器消失。
- 后台「AI 构建 → 本地工具」是只读目录：标题、工具名、描述、参数（类型/必填/默认值）、
  同步异步、源码位置（`services/tools/builtins/xxx.py:行号`），没有任何写操作。
- 「AI 构建 → MCP 服务」是完整管理页：新建/编辑/删除/启停，**连通测试**（真正建连并
  `initialize` + `tools/list`，成功后可展开「发现的工具」抽屉看原始参数 schema，并对
  单个工具做**方法测试**——填 JSON 入参现场调用，参数与结果不落库）；支持导入/导出
  Claude Desktop / Cursor 通用的 `mcpServers` JSON（重名策略 rename/skip/overwrite），
  导出前提示文档内含明文密钥。
- 主对话**不预选、不默认挂任何工具**，尤其 `current_datetime` 本身也是普通本地工具：
  不勾选它的会话（含所有老会话）里，模型不知道当前时间。例外是运维嵌入面板（OpsChat）：
  它没有工具选择器，时间工具随运维工具箱隐式挂载（见下文取舍）。
- 简历咨询深链会自动预勾选 `get_resume_doc`（工具目录里存在该 key 时），用户可手动摘掉。
- 发问时的报错均可操作：本地工具被改名/删除报「本地工具已不可用：xxx」；MCP 服务被删、
  停用或连不上报对应引导语；本地工具与 MCP 工具撞名报「工具名冲突」——按提示在选择器里
  移除或改名即可。

## 关键取舍

### 为什么本地工具不入库

工具的行为就是函数实现本身：再建一张表存名字和描述，只会制造一份必然与代码漂移的副本。
所以本地工具以 `services/tools/builtins/` 下的代码为唯一事实源——丢一个文件进去、重启
即被发现；代价是坏注册（非法命名/重名/撞保留名/缺注解/docstring 不合规）在**启动时**
就由 `validate_local_tools()` 拒绝，而不是等用户勾选时才暴露。函数名即给模型的工具名
（`^[a-z0-9_]+$`，≤64 字符），另有 11 个对话内置工具名进了 `RESERVED_TOOL_NAMES`
（知识库检索、运维服务器/数据库/Redis 工具箱的工具名），撞名同样拒绝注册。

### 为什么 MCP 是会话级短连接

对话侧 MCP 连接的生命周期等于**一次回答**：首条模型事件之前按会话勾选的服务逐个建连
（stdio 起子进程、远端建 HTTP/SSE 会话），全部 `initialize` 成功才进 agent 循环；
回答结束（含报错、取消、撞步数上限）在 `finally` 里 LIFO 关束，stdio 子进程随之回收。
不维护常驻连接池的原因：stdio 配置等于授权以后端进程权限执行任意命令，连接开得越久
风险面越大；短连接也让轮换后的密钥、重启后的远端服务下一轮自然生效。工作流侧口径不同
——同组并发步骤共享一个贯穿整次运行的 `McpToolBundle`（JSON-RPC 按请求 id 多路复用），
见 `services/mcp/tools.py`。

错误隔离分两层：**选择器层互不连坐**，本地目录与 MCP 列表各自独立请求
（`Promise.allSettled`），一个加载失败不影响另一组可选；**运行时不做部分降级**，
建连阶段任一服务失败（不存在/停用/30 秒连接超时/协议错误），`open_bundle` 先关掉已建
连接再抛，整轮回答以错误帧结束并提示去选择器移除——静默摘掉一个服务会让模型在用户以为
工具可用的情况下作答。真正进到工具调用阶段后的失败（如 MCP 返回 JSON-RPC 错误）则与
本地工具 `raise ValueError` 同口径，包成 ToolMessage 回灌模型换参数自纠，不中断回答。

### current_datetime 为什么不默认挂

模型本身没有时钟，相对时间（今天/本周/最近）必须靠工具换算。旧实现把时间工具无条件挂
进每个会话，等于给所有纯文本问答强制要求 function calling 支持，也平白多一轮工具往返。
现在主对话空工具集合时 `create_agent` 跳过 `bind_tools`，纯文本对话对模型没有工具能力
要求；需要时间感知的会话显式勾选即可。OpsChat 保留旧行为：运维会话没有选择器，而
「运维 AI 永远知道现在几点」是排查场景的硬需求，所以 `agent.use_ops` 开启或会话带运维
目标时，工具箱之前先隐式挂 `current_datetime`；用户又手选同名工具时装配阶段去重，不会
触发末尾的重名报错。

### 输出截断：对话与工作流同一口径

工具返回先统一字符串化（字符串原样、`None` 为空、其余 `json.dumps(ensure_ascii=False,
default=str)`），超过 `TOOL_OUTPUT_LIMIT = 8000` 字符截断并追加「…（工具输出过长，已
截断）」。常量定义在 `services/agent_run.py`，本地工具与 MCP 工具（`cap_tool_output`
套壳，工具名/描述/schema 不变）共用——同一个工具在对话与工作流里不该一边截断一边不
截断。MCP 侧另有两层容量保护：代抓网页 5MB 下载上限、PDF 最多抽前 30 页。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/tools/registry.py` | `@local_tool`、自动发现、docstring/注解解析、参数 schema（pydantic `create_model`）、装配、字符串化截断、保留名与重名防线 |
| `backend/app/services/tools/builtins/` | 内置工具：`datetime_tools` / `random_tools` / `web_tools` / `netdisk_tools` / `resume_tools`，丢文件进来即被发现 |
| `backend/app/routers/tools.py` | `GET /api/tools/local`：只读目录，需登录 |
| `backend/app/services/mcp/servers.py` | MCP 配置 CRUD、按 owner 隔离、Fernet 密钥袋掩码合并、`mcpServers` 导入解析/导出、连通测试状态 |
| `backend/app/services/mcp/tools.py` | 三种传输建连、`McpToolBundle` 会话级连接束、`load_mcp_tools` 适配、工具名前缀、错误拆包、连通测试/方法测试 |
| `backend/app/routers/mcp.py` | `/api/mcp` CRUD、`/test`、`/tools/call`、`/import`、`/export` |
| `backend/app/models/entities.py` `McpServer` | 表 `mcp_server`，唯一约束 `(owner_id, name)`；`env_enc`/`headers_enc` 两个 Fernet 密钥袋、`tools_snapshot` 与最近测试结果 |
| `backend/app/services/chat/service.py` | 会话选择的保存校验与回答时的工具装配（内置/知识库/运维箱 → 本地 → MCP → 末尾查重） |
| `backend/app/bootstrap.py` / `main.py` | `validate_local_tools()`，lifespan 启动步骤中表迁移之后执行，坏注册拒绝启动 |
| `frontend/src/views/LocalTools.vue` | 只读目录页 |
| `frontend/src/views/McpServers.vue` | MCP 管理页：表单/启停/连通测试/工具抽屉/方法测试/导入导出 |
| `frontend/src/views/Chat.vue` | 工具选择器（复合值 `local:<key>` / `mcp:<id>`）、恢复裁剪、简历深链预勾选 |
| `frontend/src/utils/chatTools.ts` | 工具调用时间线的中文标题（本地标题由目录动态登记，MCP 走默认兜底） |

MCP 三种传输的连接细节（`services/mcp/tools.py`）：

- `stdio`：`stdio_client(StdioServerParameters)` 起本地子进程；配置的 env 是在
  `os.environ` 基础上**合并覆盖**（SDK 语义是整体替换，不合并会丢 PATH 导致假启动）。
- `sse`：`sse_client(url, headers=...)`。
- `streamable_http`：经预配置的 `httpx.AsyncClient` 传认证头（mcp 1.30 的
  `streamable_http_client` 不再直接收 headers）；响应钩子在报错前缓存非 2xx 响应体，
  否则 SDK 关流后只能拿到状态码、拿不到网关返回的具体原因。
- 建连 30 秒超时（`CONNECT_TIMEOUT`，命令写错的假启动靠它快速失败），每次 JSON-RPC
  读超时 60 秒（`READ_TIMEOUT`）。

MCP 工具名由「服务名前缀 + 工具名」规范化：服务名中非 `[A-Za-z0-9_]` 的字符统一替换为
下划线（`server_prefix`，如服务 `calc` 的 `add` → `calc_add`），前缀为空时兜底
`mcp`。生效工具集合（知识库/运维内置 + 本地 + MCP）末尾由
`ensure_unique_tool_names()` 统一查重，撞名整轮拒绝并提示改 MCP 服务名或本地函数名。

## 内置工具清单

全部内置工具共 7 个（目录按 key 排序）：

| 工具名 | 标题 | 参数 | 要用户上下文 | 功能与边界 |
| --- | --- | --- | --- | --- |
| `current_datetime` | 查询当前时间 | 无 | 否 | 固定北京时间（UTC+8 固定偏移，不依赖机器时区与 tzdata），输出 `YYYY-MM-DD HH:MM:SS 星期X` |
| `generate_uuid` | 生成 UUID | `count: int=1`（可选，默认 1；函数体校验 1–100） | 否 | 生成 v4 UUID，多个用逗号分隔 |
| `random_integer` | 随机整数 | `min_value: int=0`、`max_value: int=100`（闭区间，函数体校验下限≤上限） | 否 | `random.randint` |
| `web_search` | 联网搜索 | `query: str`（必填）、`freshness: str="noLimit"`（`noLimit`/`oneDay`/`oneWeek`/`oneMonth`/`oneYear`，非法值回落不限） | 否 | 博查开放平台联网搜索，返回标题/链接/长摘要列表；未配 `BOCHA_API_KEY`、网络或业务错误都返回提示语而非抛异常 |
| `fetch_webpage` | 读取网页内容 | `url: str`（必填，仅 http/https） | 否 | 服务端代抓公网网页/PDF/文本正文：SSRF 红线——主机解析出的每个 IP（含 IPv4-mapped IPv6）都必须公网，环回/私网/链路本地/保留/组播全拒，**手动跟随重定向逐跳重校验**（最多 5 跳），禁带用户名密码，5MB 上限，PDF 抽前 30 页，GBK 老页面按 charset 头→meta→utf-8→gb18030 解码 |
| `save_text_to_netdisk` | 保存文件到百度网盘 | `filename: str`（必填，只留纯文件名）、`content: str`（必填，≤1MB） | 是 | 上传到本人绑定网盘应用目录 `chat/` 子目录；未配置/未绑定/BusinessError 均返回可操作提示，绝不假装成功 |
| `get_resume_doc` | 读取在线简历 | `doc_id: int`（必填） | 是 | 读本人指定简历的服务端最新全文；id 不存在或不属于本人返回澄清提示 |

`web_search` 与 `fetch_webpage` 就是「联网」能力本身：纯出站 HTTP、无用户上下文、无
数据库，相关设置为 `BOCHA_API_KEY` / `BOCHA_SEARCH_URL` / `BOCHA_TIMEOUT` /
`BOCHA_RESULT_COUNT` / `WEB_FETCH_TIMEOUT` / `WEB_FETCH_MAX_BYTES`（均在
`app/config.py`）。需要用户身份的工具把**第一个参数**声明为 `ToolContext`
（`owner_id`、`conversation_id`），按类型识别、不出现在模型可见参数表里，调用时注入；
工具内访问数据库一律自开 `SessionLocal()`，绝不用请求级 session。

## 新增一个本地工具

1. 在 `services/tools/builtins/` 下新建文件或追加函数，加 `@local_tool(title="中文名")`；
   函数名即工具名，不能撞已有本地工具和 `RESERVED_TOOL_NAMES`。
2. 每个模型可见参数写类型注解（`int/float/bool/str/list/dict`），无默认值即必填；
   docstring 首段写给模型看，`Args:` 段每行 `参数名: 说明`（最朴素 Google 风格，
   延续行与 `Returns:` 段忽略）。
3. 范围/枚举校验写在函数体里，不合法直接 `raise ValueError`——异常会被 ToolNode 包成
   ToolMessage 回灌模型自纠；「功能未启用/资源不属于本人」这类预期失败建议返回提示
   字符串而不是抛异常（内置网盘/简历/搜索工具同口径）。
4. 重启后端：启动校验通过即出现在只读目录与对话选择器。同步与 `async def` 都支持。

## 测试

- `backend/tests/test_local_tools.py`（注册表 + HTTP 目录）：发现结果稳定排序；spec
  带标题/docstring/参数/源码位置；args schema 暴露模型可见字段与默认值；
  `current_datetime` 北京时间格式；非法名/保留名/缺标题缺 docstring 缺注解拒绝；重名
  报错同时给出两处来源；JSON 类型映射与必填标记；`ToolContext` 首参对模型隐藏；装配
  去重保序并真跑同步工具；函数体异常原样传播；异步带 ctx 工具拿到身份且输出字符串化；
  未知 key 在保存校验与装配两处都拒；截断；重名检查；MCP 截断裂壳保持工具身份；
  `GET /api/tools/local` 需登录且返回信封结构。
- `backend/tests/test_tool_web.py`：搜索的无 key 提示、空 query、成功解析、freshness
  回落与周范围标注、HTTP/业务/网络三类错误转提示；代抓的坏 URL、私网环回拦截、
  IPv4-mapped IPv6、DNS 失败、userinfo 拒绝、重定向逐跳拦截、HTML 正文抽取丢脚本、
  无 charset GBK 解码、纯文本/JSON、不支持 MIME、超限中止、HTTP 错误、PDF 抽取与坏 PDF。
- `backend/tests/test_tool_netdisk.py`：注册表形态；空/纯路径文件名、空内容/超限拒绝；
  未配置/未绑定提示；成功落到 `chat/` 且路径段被清洗；BusinessError 转工具消息。
- `backend/tests/test_tool_resume.py`：注册表形态；读到最新内容；他人简历与缺失 id
  返回提示而非异常。
- `backend/tests/test_chat_tools.py`（选择持久化 + 回答时装配）：创建时去重保序、空
  列表归 NULL、未知本地 key 与不存在/他人 MCP 拒绝且不落库、**停用 MCP 保存放行**；
  更新的替换/清空/保持三态；会话列表携带选择；运行时本地工具真跑并计入 trace；MCP
  工具被挂截断壳且回答结束连接束关闭；运行时缺失/停用/连接失败均得到引导错误；本地与
  MCP 撞名拒绝；无选择会话不挂任何工具；运维会话无选择仍挂时间工具。
- `backend/tests/test_mcp_tools.py`：env/headers 密钥袋往返与掩码合并删键、换传输清掉
  另一密钥袋；连接字段变更作废旧测试结果与快照、非连接变更保留；`record_test` 成功存
  快照失败清空；传输跨字段必填；`resolve_servers` 去重保序/拒缺失拒他人拒停用；
  `server_prefix` 规范化；ExceptionGroup/HTTP 状态错误拆包；FastMCP 真服务的工具加载与
  调用契约；工具快照保留原始 inputSchema；CallToolResult 投影（含 isError）；工具调用
  错误码格式化；agent 工具循环（成功一轮收敛、未知工具/工具失败回灌不崩、超长截断、
  强制收敛、无工具单 LLM 事件、trace 配对与错误标记）；工作流 `plan_step_tools`
  的工作流级∪节点级并集、跨节点同名放行、同节点撞名拒绝、重复 id 去重。
- `backend/tests/test_mcp_config_io.py`：lark stdio 示例解析；裸 dict 与 type 别名
  （`http`/`streamableHttp` 等）归一；缺 type 按 URL 推断；单条错误不中断整批；顶层
  形态校验；导出往返、按 id 过滤保序、他人 id 拒绝、密钥不可读时保留连接字段并告警；
  跨用户导出再导入一致；rename（含批内重名）/skip/overwrite（保 id）三种冲突策略；
  导出导入 HTTP 整链路。
