# services 层收包 TODO

接着上一台机器上的活。今天（2026-10-11）已经落地两刀，这份文档记下：已经做完的、
下一刀的实测尺寸、每刀必须走的验证流程、以及卡住的项目。**动手前先跑一次
`git log --oneline -3` 和 `git status --short`，确认工作树是干净的**（本仓库常有两个
会话同时在改，脏树别硬来）。

## 已完成的基线

| 提交 | 内容 |
| --- | --- |
| `042913d` | refactor(knowledge)：`services/knowledge/` = `service.py` + `vectorstore.py` + `__init__.py` 门面 |
| `056702c` | test(knowledge)：89 个用例，这个域的第一张网 |
| `5a395a0` | refactor(workflow)：`services/workflows/` = `__init__.py`（词汇表）+ 七个实现子模块 |

`backend/app/services/` 现在是 **32 个平铺模块 + 2 个包**。全量测试 **627 passed**
（约 4.2 分钟，`.venv/Scripts/python.exe -m pytest -q`，venv 在仓库根、不在 backend 下）。

## 试点定下来的三条形状规则

1. **包名 = 被吸收的那个模块名**，所以调用方的 `from app.services.X import ...` 一行都不用改。
   knowledge 那刀是 `knowledge.py → knowledge/`，workflow 那刀是 `workflows.py → workflows/__init__.py`。
2. **门面只在有唯一入口类时做**：`knowledge/__init__.py` 按 `app/models/__init__.py` 的先例用
   绝对 import 重导出公开名（`# noqa: F401`）；`workflows/__init__.py` 只当词汇表，**不做转发**，
   调用方直接子模块取名字。别为了统一形式硬造门面。
3. **跨域共用的工具留在平铺层**，不许塞进包里：`chunking.py`（知识库 router + 简历 router +
   `services/resume.py` 都在用）、`uploads.py`（三个 router 在用）。
   一个类仍占一个模块，不再往下拆。

## 下一刀（按性价比排）

### 1. slides：三个模块，1318 行

`slides.py`(496) + `slides_html.py`(648) + `slides_store.py`(174)。**真 import 只有三行**
（`routers/slides.py:38` 引 store、`services/slides.py:42` 与 `:48` 引 html 和 store），
另有两处 `:mod:`（`routers/slides.py:306`、`services/slides.py:15`）和五处散文里写死的路径
（`main.py:245`、`entities.py:855/861/882`、`schemas.py:1630`）要一起跟。`test_slides.py`
（52 个用例）是 `from app.services import slides_html` 加 `slides_html.X` 属性访问的形态，
改一行 import 就全保住。**最小的一刀，适合热机。**

### 2. ai_gateway：两个模块，1173 行

`ai_gateway.py`(612) + `ai_gateway_proxy.py`(561)。**要改的只有六处**：
`routers/ai_gateway.py:40`、`routers/ai_gateway_openai.py:24`、`services/ai_gateway.py:3` 的
`:mod:` 引用、`test_ai_gateway.py:23` 与 `:41`、`test_ai_gateway_proxy.py:20`。
坑：其中三处是 `from app.services import ai_gateway_proxy as proxy` 的**别名形态**——包名正好是
`ai_gateway`、子模块叫 `proxy`，改成 `from app.services.ai_gateway import proxy` 就行，连 `as`
都不用留。**别动 logger 名**：`ai_gateway_proxy.py:38` 的 `logging.getLogger("yangvis.ai_gateway_proxy")`
是日志通道名、不是模块路径，knowledge 那刀同理保住了 `yangvis.vectorstore`。

### 3. mcp：两个模块，790 行

`mcp_servers.py`(526) + `mcp_tools.py`(264)。要改七处：`routers/mcp.py:24,25`、
包内的 `workflows/exec.py:33` 与 `workflows/rerun.py:31`、`test_mcp_config_io.py:22`、
`test_mcp_tools.py:19,24`。**这簇有一个真陷阱**：`mcp_tools` / `mcp_servers` 这两个词在别处也出现
——`routers/mcp.py:40` 的函数名 `list_mcp_servers`，以及 `test_mcp_tools.py:279,290` 从
`langchain_mcp_adapters.tools` 引的 `load_mcp_tools`（第三方，不是本仓库模块）。
所以模式必须带全前缀 `app\.services\.mcp_`，裸词替换会把外部依赖一起改掉。

### 4. ops：十个模块，4163 行 —— **建议缓，先别动**

引用面 102 行，是最大的一族，但两个理由压着：

- 另一个会话正在 ops 上活动（刚提交 `5bf72b1 test(ops)` 与设计文档）。
- `docs/ops-chat-confirm-flow.md` 把 ops 的路径**连行号**钉死在文里
  （`ops_safety.py:46`、`ops_actions.py:89`、`ops_server.py:355`、以及四处 `services/ops_*.py`）。
  收包会让这份文档全线对不上，而改它等于替别人改他刚写完的稿子。要动就得连带把那份文档的
  路径与行号一起核一遍。

### 5. agents / 对话域：先别收，有一条回边

`agents.py`(708) + `agent_run.py`(263) 看着是天然一簇，但 `agent_run` 的引用方散布在
workflows 包的五个实现子模块加 `workflows/__init__.py` 的文档行，还有 interview、resume、slides
三个 service 和 `test_mcp_tools.py`、`test_slides.py` 两个测试。更麻烦的是**chat 与 memory 互引**：
`chat.py:39` 顶层引 `app.services.memory`，而 `memory.py:429` 在函数内部延迟引
`app.services.chat`——这条回边是靠「函数内 import」才没炸的，收包时别把它提到顶层。
这一簇要么先把 chat/memory 的方向解掉，要么就让它继续平铺。

## 每一刀照抄的流程

1. `git status --short` 确认干净；`mkdir` 包目录，`git mv` 逐个搬（文件名去掉域前缀）。
2. 一次性 Python 脚本改引用，跑完就删。**两个坑**：
   - 仓库工作树是 **CRLF**（`core.autocrlf=true`，无 `.gitattributes`）。匹配模式里**别带 `\n`**，
     否则一条都匹配不上（workflow 那刀第一遍就漏了 6 行别名形态）。脚本读写用 bytes，
     检测 `b"\r\n" in raw` 后按原样写回。
   - 两种 import 形态都要覆盖：`from app.services.workflow_x import n` 和
     `from app.services import workflow_x [as 别名]`。后者只改这一行、保留别名。
3. 残留核查要连散文路径一起扫：`grep -rn "app\.services\.X_\|services/X_[a-z]*\.py" backend/app backend/tests docs README.md`
   （`.md` 与 `frontend/` 也要，别只 grep `.py`）。
4. 行为中性证明，逐文件：`diff <(git show HEAD:旧路径 | tr -d '\r') <(tr -d '\r' < 新路径)`。
   期望差异**只有 import 行和文档里的 `:mod:`/路径引用**。上一刀里 `workflows/runs.py` 是 0 差异纯挪。
5. `python -c "import app.main"`，再全量 pytest（期望 627 passed）。
6. 换行按字节核：`echo "lines=$(wc -l < f) cr=$(tr -dc '\r' < f | wc -c)"`，两数相等才算 CRLF。
   `Write` 工具新建的文件落成 LF，得自己转。
7. 等用户明说再提交；`type(scope): 中文短句` + 一段说明白「为什么这么切、改了哪些引用、
   什么没动」。

## 不是收包、但还挂着的事

- **镜像构建没实跑**：Docker daemon 起不来。3.12 那刀的替代证据是 AST 扫（0/77 问题）+
  `pip --dry-run --only-binary=:all:`（84 包全有 wheel，0 sdist）。起来之后补一次 `docker build`。
- **MySQL 连接池上限**这条要生产库的 `max_connections` 才能定，还欠着。
- **UI 那条**（一键复制相关）等用户截图验收，不要猜像素。
- **测试缺口**：`chat.py`(997 行) / `memory.py`(464) / `netdisk.py`(433) / `agents.py`(708) /
  `resume.py` 主体(843，现只有 `test_resume_workflow.py` 13 条) / auth·users 路由，都没有本域用例。
  已知的：knowledge 89、workflows 四件 139、ops 八件 183、slides 52、ai_gateway 39、mcp 40。
- **`models/schemas.py`(2074) 与 `routers/ops.py`(1401) 不动**——它们的长是同类声明/路由的
  重复，不是职责混杂，拆开只是搬运（用户 2026-10-11 明确过）。

## 规矩（别忘）

- 不擅自 commit / push，等明确说；信息用中文 conventional-commit。
- 测试一律文件型 SQLite 的假连接，不连共享 MySQL、不开端口做浏览器验收。
- 时间只走 `app/timeutil.py:db_now()`，`tests/test_time_convention.py` 会扫源码钉住。
- 站点没正式对外，路由/字段直接改，不留兼容层、不加 feature flag。
