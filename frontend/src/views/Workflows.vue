<template>
  <div class="page-card">
    <div class="page-title">工作流</div>
    <div class="subtitle">
      一条工作流就是把「一次功能调用」拆成几步写模型的流水线：输入变量、每步的人设与
      Prompt 模板、输出格式都在这儿改，不用发版。步骤之间可以并行，点任意一行进编辑器。
    </div>

    <div class="toolbar">
      <!-- 运行记录也是启动时自动清理的（保留窗口之外的、且不再是某个业务对象
           当前状态的那些）；这里给一个不用等重启的手动入口。 -->
      <a-popconfirm
        title="清理运行记录"
        ok-text="清理"
        cancel-text="取消"
        placement="bottomRight"
        @confirm="pruneRuns"
      >
        <template #description>
          <div class="prune-pop">
            <div class="prune-days">
              <span>保留最近</span>
              <a-input-number v-model:value="keepDays" size="small" :min="1" :max="365" />
              <span>天</span>
            </div>
            <div class="muted">正在跑的、以及每个业务对象的最近一次运行始终留着。</div>
          </div>
        </template>
        <a-button size="small" danger>清理运行记录</a-button>
      </a-popconfirm>
      <a-button type="primary" @click="openCreate">
        <PlusOutlined /> 新建工作流
      </a-button>
    </div>

    <!-- 首屏骨架屏：避免先闪「还没有工作流」再填充。 -->
    <a-skeleton v-if="loading && !workflows.length" active :paragraph="{ rows: 4 }" />
    <template v-else>
      <!-- 拉清单失败不能只靠拦截器的一次 toast：错过就静默留空，所以占位＋重试。
           已有旧数据时警告条压在列表上方，刷新失败不至于把清单变没。 -->
      <a-alert
        v-if="loadError"
        type="error"
        show-icon
        class="load-error"
        message="工作流列表加载失败"
        :description="loadError"
      >
        <template #action>
          <a-button size="small" danger @click="load">重试</a-button>
        </template>
      </a-alert>
      <!-- 整行点击进编辑器标签页，所以操作列只留「不改定义」的动作；
           这些按钮都得 click.stop，否则点一下先跳了页。 -->
      <a-table
        :data-source="workflows"
        :columns="columns"
        row-key="id"
        size="small"
        :scroll="{ x: 'max-content' }"
        :pagination="workflows.length > 10 ? { pageSize: 10, size: 'small' } : false"
        :custom-row="customRow"
      >
        <template #emptyText>
          <a-empty description="还没有工作流" />
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div class="wf-name-line">
              <span class="wf-name">{{ record.name }}</span>
              <a-tag v-if="record.is_builtin">内置</a-tag>
              <a-tag v-if="!record.enabled" color="orange">已停用</a-tag>
              <span class="wf-slug">{{ record.slug }}</span>
            </div>
            <div class="wf-desc" :title="record.description || ''">
              {{ record.description || '暂无描述' }}
            </div>
          </template>

          <!-- 步骤链：一行看清组间顺序与组内并行。 -->
          <template v-else-if="column.key === 'chain'">
            <div class="wf-chain">
              <template v-for="(group, index) in chainOf(record)" :key="index">
                <span v-if="index" class="wf-arrow">→</span>
                <!-- 并行组不再显示 g1 这种内部名：∥ 分隔符 + 各步骤标识已说明一切。 -->
                <span class="wf-group" :class="{ parallel: group.parallel }">
                  <span
                    v-for="(step, stepIndex) in group.steps"
                    :key="step.key"
                    class="wf-step"
                    :class="{ 'wf-optional': step.optional }"
                  >
                    <span v-if="stepIndex" class="wf-parallel">∥</span>
                    {{ step.key }}
                    <span class="wf-agent">{{ step.agent_name || step.agent_slug }}</span>
                  </span>
                </span>
              </template>
            </div>
          </template>

          <template v-else-if="column.key === 'scale'">
            <span class="muted">{{ scaleText(record) }}</span>
          </template>

          <!-- 最近一次运行：不点开抽屉也说得出跑没跑、什么状态、多久。
               没跑过给一个破折号——列对齐比留白好读。 -->
          <template v-else-if="column.key === 'last_run'">
            <div v-if="record.last_run" class="wf-run-cell">
              <a-tag :color="runStatusColor(record.last_run.status)">
                {{ runStatusText(record.last_run.status) }}
              </a-tag>
              <span class="muted">{{ lastRunText(record) }}</span>
            </div>
            <span v-else class="muted">—</span>
          </template>

          <!-- 内置的进去是只读，改不了；复制是后端给的唯一定制途径，所以留着。
               删除对内置行不通（后端 _owned 会拦），就别在界面上摆一个必失败的按钮。 -->
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a-button size="small" @click.stop="openRuns(record)">运行</a-button>
              <a-button size="small" @click.stop="onDuplicate(record)">复制</a-button>
              <a-popconfirm
                v-if="!record.is_builtin"
                title="确认删除该工作流？运行记录会保留。"
                @confirm="onDelete(record)"
              >
                <a-button size="small" danger @click.stop>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
    </template>

    <WorkflowRunsDrawer v-model:open="runsOpen" :workflow="current" @changed="load" />
  </div>
</template>

<script setup lang="ts">
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import { workflowsApi, type Workflow, type WorkflowStep } from '@/api'
import { fmtElapsed, runStatusColor, runStatusText } from '@/utils/format'
import { requestEditor } from '@/stores/workflowEditor'
import WorkflowRunsDrawer from '@/components/WorkflowRunsDrawer.vue'

const router = useRouter()

const workflows = ref<Workflow[]>([])
const loading = ref(false)
const loadError = ref('')
const current = ref<Workflow | null>(null)
const runsOpen = ref(false)
/** 清理窗口：与后端启动时那次的默认口径一致，界面上可以临时收紧。 */
const keepDays = ref(30)

interface StepGroup {
  name: string
  parallel: boolean
  steps: WorkflowStep[]
}

const columns = [
  { title: '工作流', key: 'name', dataIndex: 'name', width: 260 },
  { title: '流水线', key: 'chain', dataIndex: 'steps' },
  { title: '规模', key: 'scale', width: 130 },
  { title: '最近运行', key: 'last_run', width: 230 },
  { title: '操作', key: 'action', width: 190 },
]

/**
 * 相邻且分组名相同的步骤算一组：后端按这个口径执行，界面也按这个口径画，
 * 两者不会各说各话。
 */
function chainOf(workflow: Workflow): StepGroup[] {
  const groups: StepGroup[] = []
  for (const step of workflow.steps) {
    const last = groups[groups.length - 1]
    if (last && last.name === step.group_key) {
      last.steps.push(step)
      continue
    }
    groups.push({ name: step.group_key, parallel: false, steps: [step] })
  }
  for (const group of groups) group.parallel = group.steps.length > 1
  return groups
}

/** 「什么时候跑的 · 跑了多久」：耗时只在跑完那次才有。 */
function lastRunText(workflow: Workflow) {
  const run = workflow.last_run
  if (!run?.at) return ''
  const at = new Date(run.at).toLocaleString()
  return run.elapsed_ms == null ? at : `${at} · ${fmtElapsed(run.elapsed_ms)}`
}

/** 「3 步 · 2 组 · 1 输入」：没有输入就说 0，省得一排长短不齐。 */
function scaleText(workflow: Workflow) {
  const parts = [
    `${workflow.steps.length} 步`,
    `${chainOf(workflow).length} 组`,
    `${workflow.inputs.length} 输入`,
  ]
  return parts.join(' · ')
}

async function load() {
  loading.value = true
  try {
    const data = await workflowsApi.list()
    workflows.value = data.items
    loadError.value = ''
  } catch (err) {
    loadError.value = errorText(err, '请检查网络后重试')
  } finally {
    loading.value = false
  }
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

// 编辑器是顶栏上的独立标签页：把「打开谁」写进 store 再切过去，
// 页面自己拉数据，列表这边不用再把整份定义传下去。
function openCreate() {
  requestEditor(null)
  router.push('/workflows/editor')
}

/** 整行点击进编辑器；内置的只能读，所以进去就是只读。 */
function openWorkflow(workflow: Workflow) {
  requestEditor(workflow.id, workflow.is_builtin)
  router.push('/workflows/editor')
}

function customRow(workflow: Workflow) {
  return { onClick: () => openWorkflow(workflow), style: { cursor: 'pointer' } }
}

function openRuns(workflow: Workflow) {
  current.value = workflow
  runsOpen.value = true
}

async function onDuplicate(workflow: Workflow) {
  try {
    const copy = await workflowsApi.duplicate(workflow.id)
    message.success(`已复制为「${copy.name}」`)
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

async function onDelete(workflow: Workflow) {
  try {
    await workflowsApi.remove(workflow.id)
    message.success('已删除')
  } catch {
    return
  }
  await load()
}

async function pruneRuns() {
  try {
    const result = await workflowsApi.pruneRuns(keepDays.value)
    message.success(`已清理 ${result.deleted_runs} 次运行`)
  } catch {
    return // 拦截器已弹 toast。
  }
}

onMounted(load)

// 页面在 keep-alive 里：编辑器标签保存后切回来，清单得跟着换一份。
// 首次激活紧接 onMounted，那一次跳过，不重复请求。
let firstActivate = true
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  load()
})
</script>

<style scoped>
.subtitle {
  color: var(--text-3);
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.toolbar {
  margin-bottom: 16px;
  justify-content: flex-end;
}
/* 清理确认框里的那一小块：窗口选择 + 一句保留规则。 */
.prune-pop {
  width: 250px;
  font-size: var(--fs-xs);
}
.prune-days {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0;
}
/* 加载失败警告条：与下方列表拉开一点距离。 */
.load-error {
  margin-bottom: 16px;
}
.wf-name-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.wf-name {
  font-weight: 600;
}
.wf-slug {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-3);
}
/* 描述只给一行：整段话在编辑器里看，这里截断并用原生 title 兜住。 */
.wf-desc {
  color: var(--text-2);
  font-size: var(--fs-xs);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 步骤链不换行：列宽交给表格的横向滚动，折行会把一行流水线切成两行。 */
.wf-chain {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.wf-group {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--paper);
}
.wf-group.parallel {
  border-style: dashed;
}
.wf-step {
  font-family: var(--font-mono);
  white-space: nowrap;
}
.wf-optional {
  color: var(--text-3);
}
.wf-agent {
  font-family: inherit;
  color: var(--text-3);
  margin-left: 2px;
}
.wf-arrow,
.wf-parallel {
  color: var(--text-3);
}
/* 最近运行列：状态标签 + 时间与耗时。 */
.wf-run-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-xs);
}
/* 这两处的间距都交给 gap：标签自带的外边距会让它和后面的文字错开。 */
.wf-name-line :deep(.ant-tag),
.wf-run-cell :deep(.ant-tag) {
  margin-inline-end: 0;
}
</style>
