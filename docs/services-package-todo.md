# services 层收包 TODO

接着上一台机器上的活。2026-10-11 落地两刀，10-12 再落 slides、ai_gateway、mcp、ops、chat 五刀——
**七刀之后收包这一档走完了**：还有得收的簇没有了，剩下的 13 个平铺模块按规则 3 都该留着。
这份文档记下：已经做完的基线、七刀攒下的形状规则、每刀必须走的验证流程。**动手前先跑一次
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
| `77200ed` | refactor(chat)：收尾任务从 memory 挪到对话侧，解掉 `memory → chat` 那条回边 |
| `b92c59a` | refactor(chat)：`services/chat/` = `service.py` + `memory.py` + `__init__.py` 门面（转发四个名字） |

`backend/app/services/` 现在是 **13 个平铺模块 + 7 个包**。收包那几刀落定时全量 **758 passed**
（627 基线 + 对话那两面网连着搬出去的那条共 126 条：`tests/test_chat_service.py` 82、
`tests/test_chat_memory.py` 43、`tests/test_background.py` 1——最后那条是 ④ 把 `fire_and_forget`
收平时跟着搬出对话套件的；再加 `tests/test_model_config.py` 5 条，钉模型配置的删除语义）。
2026-10-13 四个测试缺口补完，全量钉在 **912**：`tests/test_netdisk.py` 53、
`tests/test_auth_users_api.py` 27、`tests/test_agents.py` 30、`tests/test_resume_service.py` 44；
10-14 把 `model_config.py` 那一域也铺满（`tests/test_model_config.py` 5 → 36），现在是 **943**。
这台机器计时跟着用例数走：670 条 1 分 45 秒、752 条 2 分 14 秒 / 3 分 00 秒、758 条 2 分 10 秒、
912 条 2 分 50 秒、943 条 3 分 04 秒，还是 2 到 3 分钟量级；
上一台记的 4.2 分钟别再引用。（`.venv/Scripts/python.exe -m pytest -q`，venv 在仓库根、不在 backend 下。）

## 试点定下来的五条形状规则

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
   **chat 那刀回到规则 1 的正例**：`chat.py → chat/` 有同名可吸收，所以 `routers/chat.py:28`
   那行一个字母都没动，整刀的外部引用只改了 4 行（routers/memory.py 走 `chat.memory`、
   测试指子模块、entities.py 一处散文、包内一行导入）。这就是「先找同名模块」值钱的实测数。
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
   chat 那刀是同一件事的 patch 版本：`test_ops_confirm.py` 要 patch `build_chat_model`、
   `fire_and_forget` 这些模块全局，`from app.services import chat as chat_module` 收包之后
   拿到的是**包**，patch 到包上作用不到 `chat.service` 里真正执行的那份名字——测试会照过，
   但兜住的是空气。所以这种行必须改成 `from app.services.chat import service as chat_module`，
   而且这是**唯一一类必须改指向的测试引用**，别顺手把别的名也指到子模块。
   **同一条规则的第二次实测**（补对话用例时撞的，比 patch 名字更隐蔽）：`SessionLocal` 这类
   「模块顶部 import 进来的名字」在进程里有两份，patch `app.database.SessionLocal` 动不到
   `chat/service.py` 手里那把，于是 `after_answer_task` 绕过临时库去连了 `.env` 里的开发库——
   用例照样全绿，只是读到的是真库的 id。仓库其余后台任务（`routers/interview.py:170`、
   `resume.py:302`、`slides.py:359`、`workflows/exec.py:135`）都在函数体内延迟 import，
   就是为了让那一处 patch 够用；顶层 import 的模块得连消费方一起换（`test_knowledge.py:271`
   早就这么写）。**用例读到了不该存在的行，先怀疑它连的是别的库**，别改断言。
   **这笔形状债 2026-10-13 已还**：`chat/service.py` 的顶层 `SessionLocal` 改成 `after_answer_task`
   函数体内导入，对话套件的 patch 面从两处收成一处（`db` fixture 与那个 spied 用例都只改
   `app.database.SessionLocal`）。仓库里还剩两处顶层形态，都是**有意保留**、别再照着改：
   `knowledge/service.py:18`（所以 `test_knowledge.py` 非 patch 两处不可）与
   `ai_gateway/proxy.py:29`（`test_ai_gateway_proxy.py:51` 直接 patch 模块那把，本来就够用）。
5. **域里有环就先解环，解环那一刀单独提交**（chat 那刀学的）：`chat.py` 顶层引 memory、
   `memory.py` 里延迟引 chat，这种「靠函数内 import 才没炸」的方向不能带进包里。判函数归谁
   看两点——谁调它（`after_answer_task` 只有 `chat.py:976` 调），以及它依赖的是谁的语义
   （它要的 `get_conversation` / `require_chat_config` 是带 owner 过滤、用 404 掩盖存在性的
   加载器，搬到记忆侧等于复制归属逻辑）。于是整节「后台任务」搬去对话侧，memory 只剩
   `MemoryService` 与其四个常量，方向变单向。搬完顺手清掉 memory 因此不再需要的
   `asyncio`/`SessionLocal`/`BusinessError`/`AgentService` 四个 import。
   **日志通道跟着职责不跟着文件**：那两条 `logger.exception` 仍要走 `yangvis.memory`，
   所以在对话侧显式写一个 `_memory_logger = logging.getLogger("yangvis.memory")`——
   不然记忆失败的日志会悄悄跑到 `yangvis.chat`，而没人会为此写测试。

## 这一档走完了

到 `b92c59a` 为止，七个包落地（knowledge、workflows、slides、ai_gateway、mcp、ops、chat），
**没有还能收的簇了**。剩下的 13 个平铺模块分两类，两类都不该进包：

- **跨域共用工具**（规则 3）：`chunking.py`、`uploads.py`、`providers.py`、`agent_run.py`
  （引用方散布在 workflows 包的五个子模块 + interview、resume、slides/service 加两个测试）、
  `chat_ops.py`（`routers/ops.py` 也在用它的 `Auditor`）、`model_config.py`。
- **一个域就一个类**（规则 3 后半句，一个类不再往下拆）：`agents.py`(708)、`resume.py`(843)、
  `netdisk.py`(433)、`interview.py`、`dashboard.py`、`captcha.py`、`login_guard.py`。

要接着做，回到「测试缺口」那一节——那里的性价比现在高于继续 reshuffle。
（收包之后确实还剩跨模块的重复，但那是**就地去重**、不是收包，见下面「又砍了四刀」那节。）

### 两刀留下的核行号手法（下次还要用）

`docs/ops-chat-confirm-flow.md` 把 ops 与对话的**路径连行号**钉死在文里。两刀都靠「就地改」
把位移做到零：ops 那刀只换 import 行与散文路径，行数一行没变；chat 那刀顶部 +2 个 import、
memory 引用块 −2 个名字，净零，函数追加在文件尾，于是 19 个钉死的服务侧行号全部原样对得上
（67=OPS_REACT_STEPS、88=STEP_LIMIT_SUFFIX、104=_OPS_RULES、890=citations 帧、908=confirm 帧、
931=`finally:`、991=done 帧）。**改完逐条 sed 一遍确认那行还是那句话**，比重新核整份文档便宜。
两个坑：批量换 `chat.py:NNN` 时 `routers/chat.py` 也在文里，用负向后视 `(?<!routers/)` 保护，
**先替 `services/chat.py` 再替裸名**；拿 placeholder 先盖 `routers/` 前缀不管用——`:NNN` 后缀
照样会被裸名规则改掉，还原时匹配不上（chat 那刀第一遍就撞在这）。
**下面那四刀把这 19 个号挪了**（②在顶部 +1、在 `require_chat_config` 里 −6，④删的段落在 998
之后）：≤104 的三个号 +1（67→68、88→89、104→105），其余全部 −5（890→885、908→903、931→926、
991→986…），`docs/ops-chat-confirm-flow.md` 已按新号重钉。重钉的做法是拿
`git show HEAD:` 那份逐号比对「映射后的那一行还是不是同一句话」，20 处全对上才写回——
别照算术改，算术会漏掉文档里没进映射表的号（`chat/service.py:835` 就是漏网的一条）。
**还债那一刀（`SessionLocal` 改函数内延迟导入，2026-10-13）是顶行 −1、尾行 +4**：删的是第 21 行
那个顶层 import，加的四行全在 1008 之后的 `after_answer_task` 里，于是 19 个号**齐 −1**
（67→66、104→103、986→985…），一次 `re.sub` 加一个 `(?<!routers/)` 就够，写回前照上面的办法
拿 `git show HEAD:` 比对 `HEAD[n] == WORK[n-1]`，19 个号全对上才动笔。

### 收包之后又砍了四刀：跨模块的重复，就地收（2026-10-12）

簇没有了，**重复还有**。四处都不建新包：`common/`/`support/` 违背规则 1（没有同名模块可吸收，
代价是所有 importer 改指向），而平铺层本来就在承担「跨域共用」这个角色。四处都走完
「每一刀照抄的流程」，全量测试仍是 **752 passed**（④把一个用例从对话套件搬进新的
`tests/test_background.py`，总数不动）。

1. **① `routers/ops_files.py` 接进 `services/uploads.py`**：SFTP 上传的提前拒绝自己抄了一份
   文案和一份 Content-Length 判定，而 uploads 里就有 `oversize_message()` 与
   `content_length_exceeds()`（那里的注释原话是「与 ops_files 的提前拒绝口径一致」——本来就要
   共享）。默认 500MB 是整除的，两版文案输出一样，但 `//` 与 `:.0f` 遇上半 MB 的限额会分家；
   换共享件之后 `int(header)` 撞见非数字不再一路走到 500。
2. **② 默认模型配置的查询收进 `model_config.py:default_model_config(db, owner_id, purpose)`**：
   同一条 `owner + purpose + is_default` 的 select 抄了三份（`agent_run.require_chat_model`、
   `chat/service.require_chat_config`、`knowledge/service.get_embedding_config`），连
   `ModelConfigService.get_default` 自己也是同一句，现在它成了那一个定义。**只抽「查」**——
   判「能不能用」留在调用方（对话看 `last_test_ok`，embedding 还要 `vector_size`），所以异常
   与文案一概没动。**它留的那个文案问题后来按用户点的单收了**（见下面「四刀之后补的两处」）。
3. **③ 剥 ```json 栅栏与截括号跨度收成两个原语**（`agent_run.strip_json_fence` / `json_span`）：
   栅栏正则在 agent_run 与 slides 里是一模一样的一行，跨度截取的三行逻辑三处各写一遍。
   **刻意没把三个解析函数并成一个带 flag 的大函数**——三处契约不同（只认 dict 抛 ValueError /
   对象与数组谁先出现取谁 / 失败返回 None 跳过本轮），slides 那段「先 `[` 就按数组截，反了会
   掐出第一个元素、整份结构白丢」的血书进了函数签名就没人再读了。三个调用点各自保留错误处理，
   memory 那份顺带白拿栅栏处理（它此前靠松正则碰巧也能吃栅栏，行为等价）。
4. **④ `fire_and_forget` 从 `chat/service.py` 收平到 `app/background.py`**：它是纯 asyncio 件
   （强引用 set + 完成回调摘除），性质与 `sse.py` 同类，不该住在域包里；`workflows/stream.py`
   的 `_DETACHED_RUNS` 是同一 idiom 的第二份（除那三行没有别的用处，确认过不是接流要查的注册表），
   删掉改调它。**这一刀给规则 4 补了一个正例**：`test_ops_confirm.py:920`、`test_chat_service.py:259`
   patch 的是 `chat_module.fire_and_forget`，收平之后 `chat/service.py` 的模块全局里仍有这个名字
   （import 进来的），patch 照样打得中，一行都不用改；要跟着搬家的只有那个直接翻私有
   `_BACKGROUND_TASKS` 的用例。

### 四刀之后补的两处（2026-10-12，用户按编号点的 1、3）

1. **同一个 `CODE_CHAT_NOT_READY` 的两种文案收成一种**：取 agent_run 那句「系统设置 → 模型管理」
   （菜单实为「系统设置 → 模型与集成」，页内 tab 叫「模型管理」，这句够用），对话侧的「设置」是旧名。
   `chat/service.py:212`、`:221` 就地换字、**净零行**，所以流程文档里 20 个钉死行号一个都不用重钉；
   agent_run 里「没有默认配置」那条也补成同一句（「请先到…添加并通过连通性测试」→「请前往…添加对话模型
   并通过连通性测试」），于是两个调用点的四句现在两两相同（用途不对、404 那两句本来就一致）。
   两处 docstring 里的「退回『设置』」也跟着换成全名。**这是改用户可见文本，不是去重**，
   动它的前提是用户点了单。
2. **删模型配置不再留悬空引用**：`model_config.delete()` 先把指向它的 `chat_conversation.model_config_id`
   与 `workflow_step.model_config_id` 置空再删行（那些表没有外键，不手动清就永远悬着）；
   `after_answer_task` 同时补捕 `LookupError`，旧数据里已经悬空的那批才有兜底。**`workflow_run_step`
   那一列不清**——它是「这一步当时实际用了哪份」的留痕，不是「以后用哪份」。新增
   `tests/test_model_config.py` 5 条钉删除语义（引用清理、默认顶上、跨 owner 拒绝），
   `tests/test_chat_service.py` 第九节加 1 条钉「悬空 id 下收尾任务安静返回」，全量因此 758。
   模型配置模块其余部分（列表、掩码、凭据轮换）仍是测试缺口。

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
   剩下六个只差 import 与 `:mod:`；chat 那刀 `chat/memory.py` 是 0 差异，`chat/service.py`
   只差包内那一行导入。
   **还了技术债那一刀（解环、合并分节）没法用「只差 import 行」自证**，改用逐行比对函数体：
   从 HEAD 抠出旧节、从新文件抠出搬过去的那节，把刻意差异（删掉的延迟导入、`_memory_logger`
   改名与随之多出的换行）归零后 diff 应当为空——`git diff --numstat` 那时只用来确认改动范围。
5. `python -c "import app.main"`，再全量 pytest（收包那几刀的基线是 758 passed；测试缺口
   一个个补完之后钉在 **943**，2026-10-14）。
6. 换行按字节核：`echo "lines=$(wc -l < f) cr=$(tr -dc '\r' < f | wc -c)"`，两数相等才算 CRLF。
   `Write` 工具新建的文件落成 LF，得自己转（ops 那刀的 `__init__.py` 就是转完才 39/39）。
   **反过来也成立**：仓库里本来就有 LF 的文件别顺手转成 CRLF——`tests/test_ops_sftp_paste.py`
   在 HEAD 上就是 159 行 / 0 CR，脚本按原样写回才对，所以核完要拿 `git show HEAD:同路径` 比一下。
   **但 `git show` 比不出工作树的换行**（autocrlf 存的是 LF，任何文件都显示 0 CR）——要看
   工作树就直接对文件跑 `tr -dc '\r' | wc -c`，拿 `lines == cr` 判 CRLF。chat 那刀的实际形态是
   一个仓库里两种都有：`chat/service.py` 1063/1063 是 CRLF，`chat/memory.py` 397/0 是 LF（它
   本来就是 LF，`git mv` 原样带走），新建的 `chat/__init__.py` 用 Write 落成 LF 后转成了 23/23。
7. 等用户明说再提交；`type(scope): 中文短句` + 一段说明白「为什么这么切、改了哪些引用、
   什么没动」。

## 不是收包、但还挂着的事

- **镜像构建已实跑**（2026-10-13，daemon 通了）：`docker build` 全绿，出的镜像里
  SQLAlchemy 2.0.35 带编译扩展（`cext` 为真）、`app.main` 导入成功、200 条路由——这正是
  3.12 那刀要护住的东西。**但有一处要说清**：依赖层命中的是缓存，`COPY backend/requirements.txt`
  那层的 key 跟当前文件一致，所以「pip 装得上」是被缓存推断出来的、不是当场观察到的；
  补进镜像的那次实测就是拿来抵这一层的。首次无缓存构建仍未见过。
- **MySQL 连接池上限**这条要生产库的 `max_connections` 才能定，还欠着。
- **UI 那条**（一键复制相关）等用户截图验收，不要猜像素。
- **五个测试缺口全补完了**（2026-10-13、10-14）：`netdisk.py` 53 条、auth·users 路由 27 条
  （`test_auth_users_api.py`，鉴权链全程是真的、只覆盖 `get_db`）、`agents.py` 30 条
  （`test_agents.py`：内置表形状与交叉引用 6、seed 3、可见性与隔离 5、写路径 5、复制 3、
  路由层 8）、`resume.py` 主体 44 条（`test_resume_service.py`，和既有的
  `test_resume_workflow.py` 13 条分开）、`model_config.py` 从 5 条扩到 36 条
  （`test_model_config.py`：列表与默认查询 6、掩码与解密失败 8、凭据轮换 8、测试结论与
  默认切换 4、路由层 5，原有的引用清理 3 与默认顶上 2 不动）。全量 **943 passed**。
  **`services/` 这一层的本域用例到此铺满**，剩下的缺口是别的性质的：见上面 MySQL 池上限
  与 UI 那两条，以及 `routers/` 只有 auth·users·agents·settings 四处有真请求用例。
  已知的：knowledge 89、workflows 四件 139、ops 八件 183、slides 52、ai_gateway 39、mcp 40、
  chat 125（`test_chat_service.py` 82 + `test_chat_memory.py` 43，2026-10-12 补上）加
  `test_background.py` 1。
- **对话用例补出来的三处，全收完了**（2026-10-12、10-13）：① `chat/service.py` 顶层
  `SessionLocal` 已于 10-13 改成 `after_answer_task` 函数内延迟导入，patch 面收成一处；
  ② 删模型配置留悬空引用、收尾任务不接 `LookupError`；③ 同一个 `CODE_CHAT_NOT_READY`
  两种文案。②③ 见上面「四刀之后补的两处」。
- **`models/schemas.py`(2074) 与 `routers/ops.py`(1401) 不动**——它们的长是同类声明/路由的
  重复，不是职责混杂，拆开只是搬运（用户 2026-10-11 明确过）。

## 规矩（别忘）

- 不擅自 commit / push，等明确说；信息用中文 conventional-commit。
- 测试一律文件型 SQLite 的假连接，不连共享 MySQL、不开端口做浏览器验收。
- 时间只走 `app/timeutil.py:db_now()`，`tests/test_time_convention.py` 会扫源码钉住。
- 站点没正式对外，路由/字段直接改，不留兼容层、不加 feature flag。
