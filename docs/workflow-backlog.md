# 工作流待办（换机继续用）

## 当前状态

- 分支 `main`，HEAD `3eca65c feat(workflow): 添加工作流步骤模型名称追踪功能`，**工作树干净**，上一轮的改动已经全部提交。
- 换机后门槛已重跑，两套环境都可用（`.venv` 与 `frontend/node_modules` 都在）：
  - `cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest -q` → **385 passed**
  - `cd frontend && npm run type-check` → 干净
- 已完成的相邻工作（不用再碰）：运行记录行上显示耗时合计与模型、运行现场每一步显示实际用的模型、编辑器变量/字段芯片、`{{var.field}}` 点号取值、后处理改取 `workflow_run_step.parsed`、分页与状态筛选、单步重跑、中途跟随 SSE、保留策略。
- 行号是 `3eca65c` 时的，实施时以函数名为准。

## 1. 工作流卡片没有「最近一次运行」

**现象/动机**：清单页看不出这条流程最近跑没跑、什么状态、多久，非要点开运行抽屉才知道。`latest_runs()`（`backend/app/services/workflows.py:1330`）已经是现成的「按 ref 批量取最近一次运行」写法，照它的形状来一份按 `workflow_id` 的即可。

**后端**：

- `workflows.py` 新增批量查询（放在 `latest_runs` 附近或 `WorkflowService` 内均可）：

  ```python
  def last_runs(self, workflow_ids: Sequence[int]) -> Dict[int, WorkflowRun]:
      """每条流程的最近一次运行，按 ``workflow_id`` 取；口径同 :func:`latest_runs`（id 最大）。"""
      ids = list(workflow_ids)
      if not ids:
          return {}
      newest = (
          select(WorkflowRun.workflow_id, func.max(WorkflowRun.id).label("run_id"))
          .where(WorkflowRun.owner_id == self._owner_id, WorkflowRun.workflow_id.in_(ids))
          .group_by(WorkflowRun.workflow_id)
          .subquery()
      )
      rows = self._db.scalars(
          select(WorkflowRun).join(newest, WorkflowRun.id == newest.c.run_id)
      ).all()
      return {row.workflow_id: row for row in rows}
  ```

- `to_item`（`workflows.py:769`）加参数 `last_run: Optional[WorkflowRun] = None`，**None 时自己查一条**（`self.last_runs([workflow.id]).get(workflow.id)`）。理由：`to_item` 有 5 个调用点（`routers/workflows.py:209/222/239/261/288`），只有 209 是列表，其余单对象端点不该各写一遍。
- 列表走批量：`routers/workflows.py:209` 现在是 `[service.to_item(row) for row in service.list(...)]`，改成先 `last = service.last_runs([w.id for w in rows])` 再逐行传入，避免 N+1。
- `models/schemas.py`：`WorkflowItem` 加 `last_run: Optional[WorkflowLastRun] = None`，新 DTO：

  ```python
  class WorkflowLastRun(BaseModel):
      id: int
      status: RunStatus
      elapsed_ms: Optional[int] = None
      # finished_at or started_at or created_at—— pending 的行前两个都是空。
      at: Optional[datetime] = None
  ```

  注意 `WorkflowItem` 若含 `model_*` 字段需要 `_ALLOW_MODEL_PREFIX`（`schemas.py:15`，本文件既有约定）；这里没有模型名，一般不需要。
- **卡片上放什么**：一行紧凑文本「状态 · 时间 · 耗时」，**不放模型**（密度会爆）。时间用列表既有的时间格式函数，耗时用 `elapsedText`。无 `last_run` 时整行不渲染，不要显示「从未运行」占位——站点没上线，宁缺毋滥。位置：`frontend/src/views/Workflows.vue` 的 `wf-chain`(55) 与 `wf-tags`(74) 之间，或并入 `wf-tags` 行尾，以对齐效果为准。
- `frontend/src/api/index.ts` 的 `Workflow` 类型加同名可选字段。

**测试**（`backend/tests/test_workflows.py`）：一条流程跑两次，断 `to_item` 带的是 id 最大的那次及其 `status/elapsed_ms`；另一条从没跑过的流程 `last_run is None`；列表接口（`test_workflows_api.py`）断一次查询能带出两条流程各自的最近运行（防 N+1 退化，可用现有打桩计数或 `assert` 形状）。

## 2. 运行中的实时面板看不到模型

**现象/动机**：`step_end` 事件不带模型名，只有跑完展开现场才知道这一步用的谁。

**后端**：

- `_execute_step`（`workflows.py:1496-1519`）在 `require_chat_model` 之后手上有 `config`，把名字传给 `_finish_step`：`_finish_step(..., model_name=config.model_name)`。渲染 prompt 失败(1494)与「步骤记录不存在」(1482)两条路径没有 config，保持 `None`。
- `_finish_step`(1522) 加关键字参数 `model_name: Optional[str] = None`，透传给 `_step_event`(1556)，载荷加 `"model_name": model_name`。
- `_follow_state`(1745) 的步骤摘要字典带出模型名：把 `select(WorkflowRunStep)` 换成 `select(WorkflowRunStep, ModelConfig.model_name).outerjoin(ModelConfig, ModelConfig.id == WorkflowRunStep.model_config_id)`（`ModelConfig` 已在该文件导入，见 1248 的 `step_models`），字典加 `"model_name": name`。注意 outerjoin 要带 `ModelConfig` 的 owner 过滤与否——按现有 `step_models` 的写法保持一致即可。
- `follow_run`(1789) 的两处载荷补字段：`run_start` 的 steps 条目(1827-1838) 加 `"model_name": info["model_name"]`，`step_end`(1855-1861) 加同名键。

**前端**：

- `api/index.ts`：`onStepEnd` 载荷(805-811) 与 `run_start` 的 step 条目(790-802) 各加 `model_name?: string | null`。
- `WorkflowRunsDrawer.vue`：`LiveStep`(220-229) 加 `model_name?: string | null`；live 面板(65 起 `.live-step`)在 `agent_name` 后面渲染 `<span v-if="step.model_name" class="muted">{{ step.model_name }}</span>`，样式沿用现场那处(156)的 `.muted`；`onStepEnd` 处理里把 `model_name` 写回对应 step。

**测试**：`test_workflows.py` 里已有的跟随流用例（`_follow_state`/`follow_run` 相关，1400 行附近）补一条断言：跑完的 `step_end` 载荷带 `model_name`，且等于当时 `require_chat_model` 解析出的那份配置名；未跑到的步骤该键为 `None`。

## 3. 超过 1 小时的耗时显示成「74 分 5 秒」

**改点**：`frontend/src/components/WorkflowRunsDrawer.vue:360` 的 `elapsedText` 补一档，插在分钟档之前：

```ts
function elapsedText(ms: number) {
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)} 分 ${Math.round((ms % 60_000) / 1000)} 秒`
  return `${Math.floor(ms / 3_600_000)} 小时 ${Math.floor((ms % 3_600_000) / 60_000)} 分`
}
```

前端没有测试运行器（`package.json` 只有 dev/build/preview/type-check），靠 type-check + 人工看一眼。

## 4. 编辑器芯片只到一层字段 —— 按判断不做

`{{大纲.pages.1.title}}` 后端支持（点号取值已通），但芯片列不出来：编辑器只知道 `output_keys` 顶层键名，不知道值里面的形状。要填就得让模型回一份结构描述或让后端探样例产物，成本明显高于收益。保持现状。

## 顺手可补的小缺口（不在上面三项里，看时间）

- `POST /workflows/runs/{run_id}/steps/{key}/rerun` 返回的仍是 `service.to_run_item(run)`（不带 `model_names`）。前端跑完会重载列表，所以不显眼；要统一就在那个端点补一次 `run_model_names([run_id])`。
- 改动前产生的历史运行 `model_config_id` 为空 → 行上不显示模型。已确认**存量数据不做处理**。
- 单步重跑期间 `prepare_step_rerun` 把 `run.elapsed_ms` 置 `None`，行上那段不显示耗时。属于有意为之，不是 bug。

## 约束（照旧）

- **不擅自 commit / push**，要提交时等你明确说；提交信息用中文 conventional-commit。
- DB 是共享 MySQL（连接串在 `backend/.env`，读它时记得 `grep -v PASSWORD`），**测试一律跑文件型 SQLite 的假连接**，不连共享库、不开端口做浏览器验收。
- 站点还没正式对外上线：路由/字段可直接改，默认不留兼容层、不加 feature flag。
- 前端视觉验收由你自己做（截图 + 短句反馈），我这边只保证 type-check 与结构对齐；视觉问题先给成因，不猜像素。
- 时间一律走 `app/timeutil.py:db_now()`，别在别处新写 `datetime.utcnow`（`tests/test_time_convention.py` 会扫源码钉住）。
