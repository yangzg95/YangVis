# 本地工具（对话可选工具）

对话页「选择工具」里的工具有两个来源：

- **本地工具**：后端代码里的 Python 函数，用 `@local_tool` 注册，启动时自动发现与校验。
  代码是唯一事实源，不入库、没有后台 CRUD；后台「AI 构建 → 本地工具」是一份只读目录。
- **MCP 服务**：在「AI 构建 → MCP 服务」页配置（stdio / SSE / Streamable HTTP），
  对话里勾选后随每次回答建立连接，结束即关闭。

两类选择都**会话级记住**（与智能体、模型、项目同口径）：重新打开会话自动恢复，
后续追问沿用。注意：「查询当前时间」本身也是一个普通本地工具（`current_datetime`），
**不预选、不默认挂载**——不勾选它的会话（含所有老会话）里模型无法查询当前时间。
例外是运维嵌入面板（OpsChat）：它没有工具选择器，时间工具随运维工具箱隐式挂载，
保持「运维 AI 永远知道现在几点」的旧行为。

相关代码：

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/tools/registry.py` | 装饰器、自动发现、参数 schema、装配、输出截断、重名防线 |
| `backend/app/services/tools/builtins/` | 内置本地工具，丢文件进来即被发现 |
| `backend/app/routers/tools.py` | `GET /api/tools/local` 只读目录 |
| `backend/app/bootstrap.py` | 启动时 `validate_local_tools()`，坏注册直接拒绝启动 |
| `backend/app/services/chat/service.py` | 对话装配本地工具 + MCP 连接束 |
| `frontend/src/views/LocalTools.vue` | 只读目录页 |

内置三个零依赖工具：`current_datetime`（查询当前时间，北京时间）、
`generate_uuid`（生成 UUID）、`random_integer`（随机整数）。

## 新增一个本地工具（4 步）

1. 在 `backend/app/services/tools/builtins/` 下新建（或在已有文件里追加）一个函数。
2. 函数名即给模型的工具名：只允许小写字母、数字、下划线（`^[a-z0-9_]+$`，≤64 字符），
   且不能与内置工具重名（见 `registry.py` 的 `RESERVED_TOOL_NAMES`）。
3. **每个模型可见参数都要有类型注解**（`int / float / bool / str / list / dict`），
   docstring 首段写给模型看的说明，`Args:` 段写每个参数的说明：

   ```python
   from app.services.tools import local_tool, ToolContext

   @local_tool(title="汇率换算")
   def convert_currency(amount: float, from_ccy: str = "CNY", to_ccy: str = "USD") -> str:
       """按给定汇率把金额从一种货币换算成另一种。

       需要做币种换算、报价对比时调用。

       Args:
           amount: 要换算的金额。
           from_ccy: 源币种三字母代码，默认 CNY。
           to_ccy: 目标币种三字母代码，默认 USD。
       """
       ...
       return f"{amount} {from_ccy} ≈ ..."
   ```

4. 重启后端：启动校验通过即在「本地工具」目录和对话选择器里出现。

同步函数和 `async def` 都支持。返回值不用自己转字符串——非字符串结果会被
`json.dumps(ensure_ascii=False)` 序列化，超过 8000 字符自动截断
（与工作流 MCP 通道同一口径，常量在 `services/agent_run.py`）。

## 参数与校验约定

- 注册表**不做参数级约束**（没有 ge/le 之类）。范围、枚举这类校验写在函数体里，
  不合法时直接 `raise ValueError(...)`：异常会被 LangGraph 的 ToolNode 包成
  ToolMessage 回灌给模型，让它换参数自纠，不会中断整条回答。
- 参数没有默认值即必填；有默认值目录页会展示「可选，默认 x」。
- docstring 解析只认最朴素的 Google 风格：首段为描述，`Args:`（或 `Arguments:`）
  段里每行 `参数名: 说明`。延续行与 `Returns:` 等段落一律忽略。

## 需要执行上下文或读写数据库

需要知道「是谁、在哪个会话里调用」时，把**第一个参数**声明为 `ToolContext`。
它按类型识别，不会出现在模型可见的参数表里，调用时由框架注入：

```python
@local_tool(title="我的待办数")
def count_my_todos(ctx: ToolContext) -> int:
    """统计我当前未完成的待办事项数量。"""
    # ctx.owner_id / ctx.conversation_id
```

工具里要访问数据库时：

- **绝不要复用请求级 session**（请求结束就关了，同步工具还跑在线程池里）。
- 同步工具：自己开 `app.database.SessionLocal()`，用完即关。
- 异步工具：`await asyncio.to_thread(...)` 包同步 DB 操作，session 同样自开。
- 时间一律走 `app/timeutil.py` 的 `db_now()`。

`ToolContext` 只给身份（`owner_id`、`conversation_id`），刻意不给 session。

## 与 MCP 工具的装配关系

每次回答的生效工具集合按这个顺序装配：（按智能体开关的知识库检索 /
运维工具箱——运维会话会在工具箱之前隐式挂上 `current_datetime`）→
会话勾选的本地工具 → 会话勾选的 MCP 服务工具。主对话不勾选任何东西时集合为空，
`create_agent` 因此跳过 bind_tools，纯文本对话不再要求模型支持 function calling。

- MCP 工具名由「服务名前缀 + 工具名」规范化而来（如服务 `calc` 的 `add` → `calc_add`）。
- MCP 工具会被套一层输出截断壳，工具名、描述、参数 schema 保持不变。
- 生效集合内**不许重名**：本地工具撞了 MCP 工具会在发送时收到明确报错，
  改名 MCP 服务或换本地工具函数名即可。
- MCP 服务在保存选择时只校验「存在且属于本人」，**停用放行**——允许先挂后启用；
  真正发问时停用/删除/连不上会得到可操作的错误帧，按提示在选择器里移除即可。
  对话选择器本身只列出「启用中」的服务。

## 排错

| 现象 | 原因与处理 |
| --- | --- |
| 后端启动失败，日志报本地工具校验错误 | 命名不合法 / 重名 / 撞保留名 / 参数缺注解 / docstring 不合规，按日志改 |
| 老会话发问报「本地工具已不可用：xxx」 | 工具代码被改名或删除，在对话「选择工具」里移除该工具 |
| 报「选择的 MCP 服务不可用 / 已停用 / 连接失败」 | 服务被删除、停用或连不上，重新启用或移除后重试 |
| 报「工具名冲突」 | 本地工具与某 MCP 工具同名，改名后重试 |
| 工具目录页看不到新工具 | 确认文件在 `services/tools/builtins/` 下、装饰器与 docstring 没漏、服务已重启 |
