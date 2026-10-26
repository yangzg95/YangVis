# 双运行对比与 Rubric 评测

同一条工作流跑了两次（改了 prompt、换了模型、单步重跑前后），哪版输出更好？
对比页把两次运行的现场并排摊开，再给一把可复用的评分尺子（Rubric）逐项打星，
保存成分数与结论。灵感来自 deer-flow/llm-space 的 Evaluate。

## 用户可见行为

- 入口：运行现场页「与其他运行对比」、运行记录面板每行的「对比」。先进页选 A，
  候选 B 只列**同一条工作流**的运行；URL 即选择（`/workflows/compare/:a/:b?`），
  可收藏、可转发，交换 A/B 只改 URL。
- 现场对齐：输入差异、按步骤 `key` 并集对齐的输出（哪一侧缺步明说）、每步渲染后的
  prompt 与实际模型名都展开；JSON 步骤的输出支持 parsed/raw 切换。
- 评分细则：2–6 条有序标准，每条名称 + 可选说明；细则可命名保存、跨多次对比复用，
  按 owner 隔离、同名拒绝。
- 打分：每条标准对 A/B 各打 1–5 星；保存时给两条非加权均分与 B−A 差值
  （正数 B 好，绿色；负数 A 好，红色），可附文字结论。
- 同一对运行 + 同一把尺子重复提交按**更新**处理；A/B 反选是另一次评测
  （比较方向反过来，差值也反号）。
- 已保存的评测在对比页列出，点标签回填分数与结论；可删除。

## 为什么快照冻结

评测保存的那一刻，细则的标准被**原样复制**进 `workflow_evaluation.rubric_snapshot`。
以后细则改名、增减标准甚至整条删除，都不改写历史评测——三个月后回看，那次分数
对着的永远是当时那把尺子。细则删除后列表里 `rubric_name` 落空，但快照自足、
评测照常展示。

取舍：不做跨评测的「标准改名后自动归并」。标准是有序数组而不是有独立身份的实体，
归并必然靠模糊匹配，错配比各看各的快照更糟。

## 为什么被引用的运行不参与清理

`prune_runs` 本来按「过保留窗口 + 不在跑 + 不是业务对象最近一次」删历史。
评测引用的运行常常既老、又是没有业务对象的手动运行——但它们是评测的物证
（评分对应的输出现场），删了评测就只剩两个光秃秃的分数。所以加了一条硬豁免：
被任一评测当 A 或 B 引用的运行一律保留；删掉评测，豁免下一轮清理时解除。
豁免是全站口径（不按 owner 收敛），与 prune 本身一致。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `WorkflowEvalRubric` / `WorkflowEvaluation` | 细则表（owner+name 唯一）与评测表；评测存快照、两条分数 JSON、均分；唯一约束 `(owner, run_a, run_b, rubric)` 实现重复提交更新 |
| `backend/app/services/workflows/evals.py` | `EvalService`：细则 CRUD、评测 upsert（同流程/归属/下标对齐校验、均分 round 2）、列表与删除；`referenced_run_ids()` 给 prune 豁免用 |
| `backend/app/services/workflows/runs.py` `prune_runs` | candidates 之后排除评测引用集，并记日志 |
| `backend/app/routers/workflows.py` `/rubrics`、`/evaluations` | 静态路径注册在 `/{workflow_id}` 之前；ValueError→`code=-1` 信封、LookupError→404 |
| `frontend/src/views/WorkflowRunCompare.vue` | 对比 + 打分整页，URL 驱动（keep-alive 单实例 watch 路由参数） |
| `frontend/src/utils/appTabs.ts` | 顶栏动态标签「运行对比 #a / #b」，正则识别 + 单测 |

### 关键校验（全部 400 级拒绝）

- run_a ≠ run_b；两次运行必须同属一条 `workflow_id` 且归属当前用户
- 细则归属当前用户
- 分数下标不重不漏、与快照标准等长；每侧 1–5
- 细则标准 2–6 条、名称去空白后不许重名，空描述归一为 `null`

均分与差值由后端计算，前端只实时预览，不产生第二份事实值。

## 测试

- `backend/tests/test_workflow_evals_api.py`：细则 CRUD/重名/条数、评测保存、
  四元组去重与反选、非法配对与分数对齐、快照冻结（改/删细则）、owner 隔离、
  prune 豁免与解除
- `frontend/src/utils/appTabs.test.ts`：对比路径的标签识别（双 id、仅 A、非法形态）
