# services 层收包 TODO

接着上一台机器上的活。2026-10-11 落地两刀，10-12 再落 slides、ai_gateway、mcp 三刀——五刀之后
「有同名模块可吸收」那一档走完了。这份文档记下：已经做完的基线、五刀攒下的形状规则、每刀必须
走的验证流程、以及还卡着的两簇。**动手前先跑一次 `git log --oneline -3` 和
`git status --short`，确认工作树是干净的**（本仓库常有两个会话同时在改，脏树别硬来）。

## 已完成的基线

| 提交 | 内容 |
| --- | --- |
| `042913d` | refactor(knowledge)：`services/knowledge/` = `service.py` + `vectorstore.py` + `__init__.py` 门面 |
| `056702c` | test(knowledge)：89 个用例，这个域的第一张网 |
| `5a395a0` | refactor(workflow)：`services/workflows/` = `__init__.py`（词汇表）+ 七个实现子模块 |
| `1d21019` | refactor(slides)：`services/slides/` = `service.py` + `html.py` + `store.py` + `__init__.py` 门面 |
| `230cc24` | refactor(ai_gateway)：`services/ai_gateway/` = `service.py` + `proxy.py` + `__init__.py` 门面 |
| `6f97f5a` | refactor(mcp)：`services/mcp/` = `servers.py` + `tools.py` + `__init__.py` 门面 |

`backend/app/services/` 现在是 **25 个平铺模块 + 5 个包**。全量测试 **627 passed**
（这台机器三次都是 2 分钟上下：2 分 06 秒、1 分 57 秒、1 分 54 秒；上一台记的 4.2 分钟别再引用。
`.venv/Scripts/python.exe -m pytest -q`，venv 在仓库根、不在 backend 下）。

## 试点定下来的四条形状规则

1. **包名 = 被吸收的那个模块名**，所以调用方的 `from app.services.X import ...` 一行都不用改。
   knowledge 那刀是 `knowledge.py → knowledge/`，workflow 那刀是 `workflows.py → workflows/__init__.py`。
   **但这一条只在真有一个同名模块可吸收时成立**（mcp 那刀学的）：`mcp_servers.py` +
   `mcp_tools.py` 谁都不叫 `mcp.py`，包名是新起的，于是调用方**全部**要改——mcp 那刀八行
   （router 两行、workflows 的 exec 与 rerun 各一行、包内 tools 接 servers 一行、
   两个测试文件三行）。这种包里，「从包名取名字」的清单就成了
   门面宽度的唯一依据：先 grep 出外面到底取哪些名字，再决定转发什么、让谁走子模块路径，
   别照抄上一个包的形式。
2. **门面只在有唯一入口类时做**：`knowledge/__init__.py` 按 `app/models/__init__.py` 的先例用
   绝对 import 重导出公开名（`# noqa: F401`）；`workflows/__init__.py` 只当词汇表，**不做转发**，
   调用方直接子模块取名字。别为了统一形式硬造门面。slides 那刀照 knowledge 做：只重导出
   `SlideService` 加 `html`/`store` 两个**子模块名**，router 的上传图片接口直接
   `from app.services.slides.store import ...`——门面不替子模块转发函数。
   **转发的宽度按调用方实际取什么定**（ai_gateway 那刀学的）：入口类之外若还有一把平铺
   公开函数被 router/测试直接取，就把这串公开名整串重导出（16 个），让
   `from app.services.X import 函数名` 保持零改动；只露子模块名会让调用方改指子模块，
   反而多了改动面。判据是先 grep 出「从包名取名字」的清单，而不是照抄上一个包的形式。
3. **跨域共用的工具留在平铺层**，不许塞进包里：`chunking.py`（知识库 router + 简历 router +
   `services/resume.py` 都在用）、`uploads.py`（三个 router 在用）。
   一个类仍占一个模块，不再往下拆。
4. **数引用要把测试的属性访问形态一起数**：`from app.services import X` 之后写 `X.attr` 的
   那些，其中私有函数与模块级 `settings` 快照**门面取不到**，必须改指子模块
   （`from app.services.slides import service as slides_module`）。slides 那刀因此改了四行
   而不是文档里估的一行；`test_knowledge.py` 本来就是这形态，照它抄就行。

## 还剩哪些簇

到 `6f97f5a` 为止，「同名模块可吸收」那一档已经走完（knowledge、workflow、slides、
ai_gateway、mcp 五刀）。**性价比队列空了**：剩下两簇一条被别人的文档钉死、一条有回边，
都不是收包该继续烧时间的地方——要接着做，回到「测试缺口」那一节。

### 1. ops：十个模块，4163 行 —— **建议缓，先别动**

引用面 102 行，是最大的一族，但两个理由压着：

- 另一个会话正在 ops 上活动（刚提交 `5bf72b1 test(ops)` 与设计文档）。
- `docs/ops-chat-confirm-flow.md` 把 ops 的路径**连行号**钉死在文里
  （`ops_safety.py:46`、`ops_actions.py:89`、`ops_server.py:355`、以及四处 `services/ops_*.py`）。
  收包会让这份文档全线对不上，而改它等于替别人改他刚写完的稿子。要动就得连带把那份文档的
  路径与行号一起核一遍。

### 2. agents / 对话域：先别收，有一条回边

`agents.py`(708) + `agent_run.py`(263) 看着是天然一簇，但 `agent_run` 的引用方散布在
workflows 包的五个实现子模块加 `workflows/__init__.py` 的文档行，还有 interview、resume
两个平铺 service 加 `slides/service.py`，以及 `test_mcp_tools.py`、`test_slides.py` 两个
测试。更麻烦的是**chat 与 memory 互引**：
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
   - 有门面的包跑 pytest 前先把测试里 `from app.services import X as 别名` 的**属性访问**
     核一遍（规则 4）：私有名和模块级 `settings` 快照门面给不出来，会在 collection
     阶段就 ImportError，改指子模块即可。slides 那刀就是先撞了这个才补的。
3. 残留核查要连散文路径一起扫：`grep -rn "app\.services\.X_\|services/X_[a-z]*\.py" backend/app backend/tests docs README.md`
   （`.md` 与 `frontend/` 也要，别只 grep `.py`）。mcp 那刀这条抓出来的是零残留，而替换
   模式带全前缀才让 `routers/mcp.py` 的函数名 `list_mcp_servers` 和第三方
   `langchain_mcp_adapters.tools.load_mcp_tools` 全身而退。
4. 行为中性证明，逐文件：`diff <(git show HEAD:旧路径 | tr -d '\r') <(tr -d '\r' < 新路径)`。
   期望差异**只有 import 行和文档里的 `:mod:`/路径引用**。`workflows/runs.py` 与
   `mcp/servers.py` 都是 0 差异纯挪。
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
