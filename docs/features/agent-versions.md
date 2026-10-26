# 智能体配置版本化与运行绑定

工作流每一步都引用一个智能体 slug。以前智能体是**原地改写**的：改完 system
prompt，上一次运行回看时「当时模型收到的人设」已经对不上了——prompt 快照只存了
渲染后的用户模板，system prompt 与温度一直是现取现用。现在每次保存人设都追加一个
不可变版本，步骤开跑时绑定版本号，历史运行永远对得上当时的尺子。

## 用户可见行为

- 智能体卡片多一个「版本」入口：左列版本号 + 时间，右列那一版的人设快照
  （system prompt、温度、知识库/运维/记忆开关）。
- 新建即 v1；之后**只有影响模型输出的字段**被保存时才追加新版本：
  `system_prompt`、`temperature`、`use_knowledge`、`use_ops`、`use_memory`。
  改名、改描述、藏出对话选择器、调排序不产生新版本（快照里仍记当时的名字，
  当版本列表的辨认标签用）。
- 内置智能体随代码升级：启动 seed 时把代码里的定义与最新快照对账，prompt 变了
  就追加 v2、v3；没变就是幂等空转。全新安装每个内置智能体从 v1 开始。
- 运行现场每一步的标题上显示 `vN`（悬浮解释「绑的是当时那一版」）；
  双运行对比页里同一步两侧版本不同，给橙色「人设版本不同」标记，并可各自展开
  「当时的人设」懒加载看快照——输出差异先归因到人设有没有改版。
- 删除智能体**不删版本行**：旧运行步骤上的版本号仍取得到快照（接口对已删除
  智能体目前返回 404，对比页按 404 提示处理；版本行在库里保留）。

## 关键取舍

### 为什么是「每次保存自动追加」而不是手动发布

llm-space 用显式版本，但本项目所有「现场」语义都走快照（run.inputs、step.prompt、
评测的 rubric_snapshot），没有草稿/发布两套状态。自动追加让「运行绑版本」不需要
新增一条「先发布才能跑」的使用规矩；噪音用字段白名单压掉——改名与开关不产生版本。

### 为什么绑在步骤行而不是运行行

一条流程的不同步骤可能挂不同智能体，一次运行里混着多个版本；单步重跑还会让
同一步在不同时刻对着不同版本。绑在 `workflow_run_step`（`agent_id` +
`agent_version` 两列）才能精确到「这一步这一次执行」。单步重跑时按当时最新版本
重新绑定，与「重跑即用当前定义」的既有语义一致。

### 为什么连 agent_id 一起存

slug 在智能体删除后可以被新建复用，只存 slug + version 会张冠李戴；id 是永久
身份。两列都允许 NULL：功能上线前的老运行没有版本可对，界面不显示版本标。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `AgentVersion` | 表 `agent_version`，唯一约束 `(agent_id, version)`，`snapshot` JSON 存全字段 |
| `backend/app/services/agent_versions.py` | 快照/指纹、`append` / `append_if_changed`、`current_version_no`、内置对账 `reconcile_builtins`、存量 `ensure_versions` |
| `backend/app/services/agents.py` | create/duplicate 写 v1；update 前后按指纹决定追加；seed 末尾对账内置 |
| `backend/app/services/workflows/exec.py` `_execute_step` | 首发与单步重跑的唯一汇聚点：解析智能体、把 id + 当前版本号写到步骤行，agent 实例复用给 `run_agent` |
| `backend/app/bootstrap.py` | `workflow_run_step` 两列幂等 ALTER；seed 后为存量智能体补 v1 |
| `backend/app/routers/agents.py` | `GET /agents/{id}/versions`、`.../versions/{version}`（内置人人可读，私有按 owner） |
| `frontend/src/views/Agents.vue` + `AgentCard.vue` | 「版本」动作与版本史弹窗 |
| `frontend/src/components/workflow/RunScene.vue` | 步骤标题的 `vN` 标 |
| `frontend/src/views/WorkflowRunCompare.vue` | 版本差异标记 + 「当时的人设」懒加载快照（按 `id:version` 缓存） |

## 升级行为

- 新表 `agent_version` 由 `create_all` 建立；两个新列走 `migrate_schema` 的
  幂等 ALTER（SQLite 测试库直接建出新列）。
- 启动 `seed_agents()`：内置对账追加；随后为**所有没有版本行的智能体**按当前值
  补 v1（「功能上线时的人设」），只补一次。
- 步骤行在执行器里「查得到智能体才绑」：查不到不报错，仍交给 `run_agent` 里的
  `require_by_slug` 走原有缺失语义，错误行为与旧版完全一致。

## 测试

- `backend/tests/test_agent_versions.py`：v1/追加/开关与改名不产生版本/温度往返、
  旧快照不可变、副本独立 v1、删除保留版本、内置 seed 对账（幂等 + 模拟代码升级）、
  存量补 v1 且只补一次
- `backend/tests/test_agent_versions_api.py`：执行端到端绑定（改人设后新运行绑
  v2、旧运行仍指 v1）、版本接口可见性（内置公开、私有 404）、404 形状
