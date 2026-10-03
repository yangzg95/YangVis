# 工作流待办（换机继续用）

## 当前状态

- 分支 `main`，HEAD `add582b`（那个提交只加了本文件，代码没动），**1/2/3 三项已实施完、尚未提交**。
- 门槛已重跑，两套环境都可用（`.venv` 与 `frontend/node_modules` 都在）：
  - `cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest -q` → **388 passed**（原 385 + 3 条新用例）
  - `cd frontend && npm run type-check` → 干净
- 已完成的相邻工作（不用再碰）：运行记录行上显示耗时合计与模型、运行现场每一步显示实际用的模型、编辑器变量/字段芯片、`{{var.field}}` 点号取值、后处理改取 `workflow_run_step.parsed`、分页与状态筛选、单步重跑、中途跟随 SSE、保留策略。

## 1. 工作流卡片「最近一次运行」 —— 已做

按计划实施，两处与实际代码对不上的地方按现场为准：

- `schemas.py` 里运行状态一直是裸 `str`（`WorkflowRunItem.status: str`），没有 `RunStatus` 枚举，所以 `WorkflowLastRun.status` 也写成 `str`，没新造枚举。
- `last_runs()` 只能是 `WorkflowService` 的方法（要用 `self._db` / `self._owner_id`），放在 `steps_of` 与 `to_item` 之间；DTO 组装另起了 `_to_last_run()` 静态方法，`at` 的 `finished_at or started_at or created_at` 回退顺序写在那里的注释上。
- `to_item(workflow, last_run=None)`：None 时自查一条；`routers/workflows.py` 列表端点先批量 `last_runs` 再逐行传入。
- 卡片：`wf-chain` 与 `wf-tags` 之间新增 `.wf-last-run` 行 —— 状态 `a-tag` + muted 的「时间 · 耗时」，无 `last_run` 整行 `v-if` 不渲染（不留「从未运行」占位）。
- 前端把 `elapsedText` 与状态文案/颜色表抽到了 `utils/format.ts`（`fmtElapsed` / `runStatusText` / `runStatusColor`），抽屉用 `import { runStatusColor as statusColor }` 别名保持模板不动 —— 卡片和抽屉共用一份口径，不留两份状态表。
- 测试：`test_workflows.py` 加 `test_last_runs_takes_the_newest_run_of_each_workflow`（跑两次取 id 最大、`status/elapsed_ms/at`、没跑过的 `last_run is None`）与 `test_to_item_looks_up_the_last_run_when_not_given`；`test_workflows_api.py` 加 `test_list_workflows_carries_each_workflows_own_last_run`（两条流程各带各的最近运行，第三条与内置那些为 null）。

## 2. 运行中的实时面板显示每步模型 —— 已做

- `_execute_step`：`model_name` 局部变量在第二个 `try` 前置 `None`，`require_chat_model` 成功后赋 `config.model_name`。这样「模型解析成功、调用失败」那条路也带得上名字，而 `require_chat_model` 自己挂了时仍是 `None` —— 原计划没写明这条路径，按「跑挂的那步也知道自己用的是谁」的既有口径（见 `test_run_model_names_skips_the_steps_that_never_ran`）取带名字。渲染 prompt 失败与「步骤记录不存在」两条保持 `None`。
- `_finish_step(..., model_name=None)` → `_step_event` 载荷加 `model_name`；`_follow_state` 改 `select(WorkflowRunStep, ModelConfig.model_name).outerjoin(...)`（与 `step_models` 同一写法，不给 `ModelConfig` 加 owner 过滤）；`follow_run` 的 `run_start` 步骤条目与 `step_end` 载荷各补同名键。
- 前端：`api/index.ts` 两处载荷 + `LiveStep` 加 `model_name`；live 面板在 `spacer` 之后、字数之前渲染 `.muted` 的模型名（与现场那处顺序一致）；`applyStepEnd` 与 `attachFollow` 的 `run_start` 都写回。
- 测试：`test_execute_run_streams_and_persists_each_step` 断两条 `step_end` 各带 `unit-test`；`test_follow_run_streams_only_what_the_client_missed` 断 `run_start` 未写模型的两步为 `[None, None]`、`step_end` 载荷整字典带 `"model_name": "unit-test"`（这条是原来唯一会挂的断言，载荷是精确比较）。

## 3. 耗时显示小时档 —— 已做

`fmtElapsed`（原 `elapsedText`，已移到 `utils/format.ts`）加小时档。

顺带改掉了一个**既有**的取整毛病（你确认要一起改的）：原来秒档/分档用 `Math.round` 单独取整，边界上会写出比该档上限还大的「60.0 秒」（59_999ms）和「59 分 60 秒」（3_599_999ms）。现在**先归到整秒再分档**，边界直接进下一档。

实跑扫过 0–90_000_000ms（步长 997，约 9 万个样本）+ 全部边界点，没有任何一档出现 60，逐点单调：`59499 → 59.5 秒`、`59500 → 1 分 0 秒`、`3599499 → 59 分 59 秒`、`3599500 → 1 小时 0 分`、`4445000 → 1 小时 14 分`、`7320000 → 2 小时 2 分`。前端没有测试运行器（`package.json` 只有 dev/build/preview/type-check），所以这一条靠 node 实跑 + type-check 钉，没留用例。

## 4. 编辑器芯片只到一层字段 —— 按判断不做

`{{大纲.pages.1.title}}` 后端支持（点号取值已通），但芯片列不出来：编辑器只知道 `output_keys` 顶层键名，不知道值里面的形状。要填就得让模型回一份结构描述或让后端探样例产物，成本明显高于收益。保持现状。

## 顺手可补的小缺口（看时间）

- `POST /workflows/runs/{run_id}/steps/{key}/rerun` 返回的仍是 `service.to_run_item(run)`（不带 `model_names`）。前端跑完会重载列表，所以不显眼；要统一就在那个端点补一次 `run_model_names([run_id])`。
- 改动前产生的历史运行 `model_config_id` 为空 → 行上不显示模型。已确认**存量数据不做处理**。
- 单步重跑期间 `prepare_step_rerun` 把 `run.elapsed_ms` 置 `None`，行上那段不显示耗时。属于有意为之，不是 bug。

## 待验收

- 三项都只做到了 type-check 与结构对齐，**前端视觉由你截图确认**：卡片上 `.wf-last-run` 那行的对齐与密度、实时面板里模型名出现的位置（在 `spacer` 之后、字数之前）。

## 约束（照旧）

- **不擅自 commit / push**，要提交时等你明确说；提交信息用中文 conventional-commit。
- DB 是共享 MySQL（连接串在 `backend/.env`，读它时记得 `grep -v PASSWORD`），**测试一律跑文件型 SQLite 的假连接**，不连共享库、不开端口做浏览器验收。
- 站点还没正式对外上线：路由/字段可直接改，默认不留兼容层、不加 feature flag。
- 前端视觉验收由你自己做（截图 + 短句反馈），我这边只保证 type-check 与结构对齐；视觉问题先给成因，不猜像素。
- 时间一律走 `app/timeutil.py:db_now()`，别在别处新写 `datetime.utcnow`（`tests/test_time_convention.py` 会扫源码钉住）。
