<template>
  <div class="page-card">
    <div class="page-title">工作流</div>
    <div class="subtitle">
      一条工作流就是把「一次功能调用」拆成几步写模型的流水线：输入变量、每步的人设与
      Prompt 模板、输出格式都在这儿改，不用发版。步骤之间可以并行。
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
    <div v-if="loading && !workflows.length" class="workflow-grid">
      <a-card v-for="i in 2" :key="i" size="small" class="workflow-card">
        <a-skeleton active :title="{ width: '40%' }" :paragraph="{ rows: 4 }" />
      </a-card>
    </div>
    <template v-else>
      <!-- 拉清单失败不能只靠拦截器的一次 toast：错过就静默留空，所以占位＋重试。
           已有旧数据时警告条压在卡片上方，刷新失败不至于把清单变没。 -->
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
      <div class="workflow-grid">
        <a-card v-for="workflow in workflows" :key="workflow.id" size="small" class="workflow-card">
          <div class="wf-head">
            <span class="wf-name">{{ workflow.name }}</span>
            <a-tag v-if="workflow.is_builtin">内置</a-tag>
            <span class="spacer" />
            <span class="wf-slug">{{ workflow.slug }}</span>
          </div>

          <div class="wf-desc">{{ workflow.description || '暂无描述' }}</div>

          <!-- 步骤链：一行看清组间顺序与组内并行，比逐条列步骤省地方。 -->
          <div class="wf-chain">
            <template v-for="(group, index) in chainOf(workflow)" :key="index">
              <span v-if="index" class="wf-arrow">→</span>
              <span class="wf-group" :class="{ parallel: group.steps.length > 1 }">
                <span v-if="group.parallel" class="wf-group-name">{{ group.name }}</span>
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

          <!-- 最近一次运行：不点开抽屉也说得出跑没跑、什么状态、多久。
               没跑过就整行不出现——站点还没上线，宁缺毋滥。 -->
          <div v-if="workflow.last_run" class="wf-last-run">
            <a-tag :color="runStatusColor(workflow.last_run.status)">
              {{ runStatusText(workflow.last_run.status) }}
            </a-tag>
            <span class="muted">{{ lastRunText(workflow) }}</span>
          </div>

          <div class="wf-tags">
            <a-tag>{{ workflow.steps.length }} 步</a-tag>
            <a-tag>{{ chainOf(workflow).length }} 个分组</a-tag>
            <a-tag v-if="workflow.inputs.length">{{ workflow.inputs.length }} 个输入</a-tag>
            <a-tag v-if="!workflow.enabled" color="orange">已停用</a-tag>
          </div>

          <template #actions>
            <span @click="openRuns(workflow)">运行</span>
            <span v-if="workflow.is_builtin" @click="openView(workflow)">查看</span>
            <template v-else>
              <span @click="openEdit(workflow)">编辑</span>
              <span @click="onDuplicate(workflow)">复制</span>
              <a-popconfirm title="确认删除该工作流？运行记录会保留。" @confirm="onDelete(workflow)">
                <span class="danger">删除</span>
              </a-popconfirm>
            </template>
          </template>
        </a-card>
      </div>

      <a-empty v-if="!workflows.length && !loadError" description="还没有工作流" />
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

function openEdit(workflow: Workflow) {
  requestEditor(workflow.id)
  router.push('/workflows/editor')
}

function openView(workflow: Workflow) {
  requestEditor(workflow.id, true)
  router.push('/workflows/editor')
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
  font-size: 13px;
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
  font-size: 12px;
}
.prune-days {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0;
}
.workflow-grid {
  display: grid;
  /* 步骤链比智能体卡片宽，最小列也相应放宽；超小屏用 min() 兜住不横向溢出。 */
  grid-template-columns: repeat(auto-fill, minmax(min(420px, 100%), 1fr));
  gap: 16px;
}
/* 加载失败警告条：与下方卡片列表拉开一点距离。 */
.load-error {
  margin-bottom: 16px;
}
.wf-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-name {
  font-weight: 600;
  font-size: 15px;
}
.wf-slug {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-3);
}
.wf-desc {
  color: rgba(0, 0, 0, 0.65);
  font-size: 13px;
  line-height: 1.5;
  min-height: 40px;
  margin-top: 6px;
}
.wf-chain {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
  font-size: 12px;
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
.wf-group-name {
  color: var(--text-3);
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
.wf-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}
/* 最近一次运行：状态标签 + 一行紧凑的时间与耗时。 */
.wf-last-run {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
}
/* 间距交给 gap，标签自带的外边距会让它和后面的时间错开。 */
.wf-last-run :deep(.ant-tag) {
  margin-inline-end: 0;
}
.spacer {
  flex: 1;
}
.danger {
  color: #cf1322;
}
</style>
