# services 层收包 TODO

接着上一台机器上的活。2026-10-11 落地两刀，10-12 再落 slides、ai_gateway、mcp、ops 四刀——
**六刀之后收包这一档基本走完了**：只剩 chat+memory 一簇有回边要先解。这份文档记下：已经做完的
基线、六刀攒下的形状规则、每刀必须走的验证流程、以及那一簇还卡着的。**动手前先跑一次
`git log --oneline -3` 和 `git status --short`，确认工作树是干净的**（本仓库常有两个会话同时在改，
脏树别硬来；ops 那刀就是等另一个会话收工才动的）。

## 已完成的基线

| 提交 | 内容 |
| --- | --- |
| `042913d` | refactor(knowledge)：`services/knowledge/` = `service.py` + `vectorstore.py` + `__init__.py` 门面 |
| `056702c` | test(knowledge)：89 个用例，这个域的第一张网 |
| `5a395a0` | refactor(workflow)：`services/workflows/` = `__init__.py`（词汇表）+ 七个实现子模块 |
| `1d21019` | refactor(slides)：`services/slides/` = `service.py` + `html.py` + `store.py` + `__init__.py` 门面 |
| `230cc24` | refactor(ai_gateway)：`services/ai_gateway/` = `service.py` + `proxy.py` + `__init__.py` 门面 |
| `6f97f5a` | refactor(mcp)：`services/mcp/` = `servers.py` + `tools.py` + `__init__.py` 门面 |
| `2aed1a0` | refactor(ops)：`services/ops/` = 十个子模块 + **只做包文档的 `__init__.py`**（无门面） |

`backend/app/services/` 现在是 **15 个平铺模块 + 6 个包**。全量测试 **627 passed**
（这台机器四次都是 2 分钟上下：2 分 06 秒、1 分 57 秒、1 分 54 秒、ops 这刀 1 分 48 秒；
上一台记的 4.2 分钟别再引用。`.venv/Scripts/python.exe -m pytest -q`，venv 在仓库根、不在 backend 下）。

## 试点定下来的四条形状规则

1. **包名 = 被吸收的那个模块名**，所以调用方的 `from app.services.X import ...` 一行都不用改。
   knowledge 那刀是 `knowledge.py → knowledge/`，workflow 那刀是 `workflows.py → workflows/__init__.py`。
   **但这一条只在真有一个同名模块可吸收时成立**（mcp 那刀学的）：`mcp_servers.py` +
   `mcp_tools.py` 谁都不叫 `mcp.py`，包名是新起的，于是调用方**全部**要改——mcp 那刀八行
   （router 两行、workflows 的 exec 与 rerun 各一行、包内 tools 接 servers 一行、
   两个测试文件三行）。这种包里，「从包名取名字」的清单就成了
   门面宽度的唯一依据：先 grep 出外面到底取哪些名字，再决定转发什么、让谁走子模块路径，
   别照抄上一个包的形式。**ops 那刀是这一条的极端情形**：十个模块全带 `ops_` 前缀、
   没一个同名可吸收，包名照样新起，于是 51 行 import 全部要改（包内互引 19 + 外部 32），
   加上 `docs/ops-chat-confirm-flow.md` 里 5 处散文路径——这一刀实测的引用面是 **51+5 行**，
   此前文档里估的「102 行」把同一行的两种写法和注释重复计了，别再引用那个数。
2. **门面只在有唯一入口类时做**：`knowledge/__init__.py` 按 `app/models/__init__.py` 的先例用
   绝对 import 重导出公开名（`# noqa: F401`）；`workflows/__init__.py` 只当词汇表，**不做转发**，
   调用方直接子模块取名字。别为了统一形式硬造门面。slides 那刀照 knowledge 做：只重导出
   `SlideService` 加 `html`/`store` 两个**子模块名**，router 的上传图片接口直接
   `from app.services.slides.store import ...`——门面不替子模块转发函数。
   **转发的宽度按调用方实际取什么定**（ai_gateway 那刀学的）：入口类之外若还有一把平铺
   公开函数被 router/测试直接取，就把这串公开名整串重导出（16 个），让
   `from app.services.X import 函数名` 保持零改动；只露子模块名会让调用方改指子模块，
   反而多了改动面。判据是先 grep 出「从包名取名字」的清单，而不是照抄上一个包的形式。
   **ops 那刀把「不做转发」这一支钉实了**：十个模块各管一件事、没有唯一入口类，所以
   `ops/__init__.py` 只有 39 行包文档（服务器侧/数据库侧分组 + 一条依赖方向的说明：
   safety、actions、database、server、sftp 互不相干，sftp 只收 server 建好的连接对象），
   **零转发**，调用方一律 `from app.services.ops import database as db_ops` 这种子模块形态。
   没有入口类时别硬凑一个门面，凑出来的转发清单只会变成第二个要维护的谎言。
3. **跨域共用的工具留在平铺层**，不许塞进包里：`chunking.py`（知识库 router + 简历 router +
   `services/resume.py` 都在用）、`uploads.py`（三个 router 在用）。
   一个类仍占一个模块，不再往下拆。
4. **数引用要把测试的属性访问形态一起数**：`from app.services import X` 之后写 `X.attr` 的
   那些，其中私有函数与模块级 `settings` 快照**门面取不到**，必须改指子模块
   （`from app.services.slides import service as slides_module`）。slides 那刀因此改了四行
   而不是文档里估的一行；`test_knowledge.py` 本来就是这形态，照它抄就行。

## 还剩哪些簇

到 `2aed1a0` 为止，**收包这一档只剩一簇有得做**，而且它先要还一笔技术债。其余的
`agent_run.py` / `chat_ops.py` / `captcha.py` / `login_guard.py` / `providers.py` 都是
跨域共用的工具，按规则 3 就该留在平铺层——**不是每个模块都非进包不可**。

### ops 那刀做完了（2026-10-12）

当初压着不动的两个理由都解了：另一个会话收工了；`docs/ops-chat-confirm-flow.md` 的路径
连行号一起核过——全是就地改（in-place 替换 import 行与散文路径），行数一行没变，所以
它钉死的 `ops/safety.py:46`（`_SHELL_METACHARS`）、`ops/actions.py:89`（CAS 那条 UPDATE）、
`ops/server.py:355`（`OPS_OUTPUT_LIMIT`）改完仍对得上。**收包前把钉行号的文档列出来，改完逐条
sed 一遍确认那行还在**，这比重新核一遍整份文档便宜得多。

### 唯一还剩的一簇：chat + memory，但有一条回边

`chat.py`(997) + `memory.py`(464) 是对话域仅剩的两个大块，看着该收，但**互引方向没解**：
`chat.py:39` 顶层引 `app.services.memory`，而 `memory.py:429` 在函数内部延迟引
`app.services.chat`——这条回边是靠「函数内 import」才没炸的，收包时别把它提到顶层。
要动就先定谁是底层：把 memory 改成不回调 chat（把那段依赖的参数化进去），再收；
否则这一簇就继续平铺。

`agents.py`(708) + `agent_run.py`(263) 看着是天然一簇，但 `agent_run` 的引用方散布在
workflows 包的五个实现子模块加 `workflows/__init__.py` 的文档行，还有 interview、resume
两个平铺 service 加 `slides/service.py`，以及 `test_mcp_tools.py`、`test_slides.py` 两个
测试——它是规则 3 说的共用工具，不收。

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
   `langchain_mcp_adapters.tools.load_mcp_tools` 全身而退。**ops 这刀补一条**：`__pycache__` 里
   旧模块的 `.pyc` 会命中 grep，别去追——Python 3 不会从 pycache 无源码导入，它是噪声不是残留。
4. 行为中性证明，逐文件：`diff <(git show HEAD:旧路径 | tr -d '\r') <(tr -d '\r' < 新路径)`。
   期望差异**只有 import 行和文档里的 `:mod:`/路径引用**。`workflows/runs.py` 与
   `mcp/servers.py` 都是 0 差异纯挪；ops 那刀有四个 0 差异（actions / safety / server / sftp），
   剩下六个只差 import 与 `:mod:`。
5. `python -c "import app.main"`，再全量 pytest（期望 627 passed）。
6. 换行按字节核：`echo "lines=$(wc -l < f) cr=$(tr -dc '\r' < f | wc -c)"`，两数相等才算 CRLF。
   `Write` 工具新建的文件落成 LF，得自己转（ops 那刀的 `__init__.py` 就是转完才 39/39）。
   **反过来也成立**：仓库里本来就有 LF 的文件别顺手转成 CRLF——`tests/test_ops_sftp_paste.py`
   在 HEAD 上就是 159 行 / 0 CR，脚本按原样写回才对，所以核完要拿 `git show HEAD:同路径` 比一下。
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
