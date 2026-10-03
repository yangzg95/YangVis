<template>
  <a-drawer
    :open="open"
    :title="drawerTitle"
    placement="right"
    :width="860"
    :mask-closable="false"
    @update:open="(value: boolean) => emit('update:open', value)"
  >
    <template #extra>
      <a-button size="small" :loading="loading" @click="loadRuns">刷新</a-button>
    </template>

    <!-- 发起：填这条流程声明的输入变量。业务对象模式下不出这一栏——那条流水线
         是功能自己在发起时跑的，这里只回看它跑过的现场。 -->
    <div v-if="!subject && !composerOpen" class="toolbar-top">
      <a-button type="primary" size="small" :disabled="!workflow?.steps.length" @click="openComposer">
        <PlayCircleOutlined /> 发起一次运行
      </a-button>
      <span class="muted">
        {{ workflow?.steps.length }} 步 · {{ groupCount }} 个并行组
      </span>
    </div>

    <div v-else-if="!subject && composerOpen" class="composer">
      <a-form layout="vertical">
        <a-form-item
          v-for="field in workflow?.inputs ?? []"
          :key="field.name"
          :required="field.required"
        >
          <template #label>
            {{ field.label }}
            <span class="muted">{{ field.name }}</span>
            <span v-if="!field.required" class="muted">选填</span>
          </template>
          <a-textarea v-model:value="inputs[field.name]" :rows="4" :disabled="running" />
        </a-form-item>
        <a-empty
          v-if="!workflow?.inputs.length"
          description="这条流程没有输入变量"
          :image-style="{ height: '32px' }"
        />
      </a-form>

      <a-space class="composer-actions">
        <a-button v-if="!running" type="primary" size="small" @click="startRun">
          <PlayCircleOutlined /> 开始运行
        </a-button>
        <a-button v-if="!running" size="small" @click="resetComposer">收起</a-button>
        <span v-if="running" class="muted">关掉抽屉不会中断已跑完的步骤，进度随时回来接着看。</span>
      </a-space>
    </div>

    <!-- 进度：事件只报状态，正文由下面的历史详情按步取。发起一次运行和中途接上
         正在跑的那次共用这一块——两条流的事件形状一致，这里不用分情况写。 -->
    <div v-if="live.steps.length" class="live">
      <div class="live-head">
        <a-tag :color="statusColor(live.status)">{{ statusText(live.status) }}</a-tag>
        <span class="muted">{{ liveCaption }}</span>
        <span class="spacer" />
        <a-button v-if="liveBusy" size="small" @click="stopLive">停止跟随</a-button>
        <a-button v-else size="small" @click="clearLive">收起</a-button>
      </div>
      <div v-for="step in live.steps" :key="step.key" class="live-step">
        <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
        <span class="live-key">{{ step.key }}</span>
        <span class="muted">{{ step.agent_name || step.agent_slug }}</span>
        <span v-if="step.optional" class="muted">可选</span>
        <span class="spacer" />
        <span v-if="step.model_name" class="muted">{{ step.model_name }}</span>
        <span v-if="step.output_chars" class="muted">{{ step.output_chars }} 字</span>
        <span v-if="step.elapsed_ms != null" class="muted">{{ fmtElapsed(step.elapsed_ms) }}</span>
        <span v-if="step.error" class="danger">{{ step.error }}</span>
      </div>
      <a-alert
        v-if="live.status === 'failed' || live.status === 'cancelled'"
        type="error"
        :message="live.error || '运行失败'"
        show-icon
      />
    </div>

    <a-divider style="margin: 16px 0">{{ subject ? '历次运行' : '最近运行' }}</a-divider>

    <!-- 筛完再翻页：条件变了总数就重算，停在第 3 页筛一个只剩 1 条的状态会看到
         空列表，所以筛选项一律把页码拨回第一页。 -->
    <div v-if="total" class="runs-bar">
      <a-select
        v-model:value="statusFilter"
        :options="statusOptions"
        placeholder="全部状态"
        allow-clear
        size="small"
        class="runs-filter"
        @change="onFilterChange"
      />
      <span class="spacer" />
      <span class="muted">共 {{ total }} 次</span>
      <a-pagination
        v-if="total > PAGE_SIZE"
        v-model:current="page"
        :total="total"
        :page-size="PAGE_SIZE"
        size="small"
        simple
        @change="() => void loadRuns()"
      />
    </div>

    <!-- 拉列表失败不能只靠拦截器的一次 toast：错过就静默留空，所以占位＋重试。
         已有旧数据时警告条压在列表上方，刷新失败不至于把记录变没。 -->
    <a-alert
      v-if="runsError"
      type="error"
      show-icon
      class="load-error"
      message="运行记录加载失败"
      :description="runsError"
    >
      <template #action>
        <a-button size="small" danger @click="loadRuns">重试</a-button>
      </template>
    </a-alert>

    <a-spin :spinning="loading">
      <a-empty v-if="!runs.length && !runsError" :description="emptyText" />
      <a-collapse v-else v-model:active-key="expanded" @change="onExpand">
        <a-collapse-panel v-for="run in runs" :key="run.id">
          <template #header>
            <span class="run-head">
              <span class="run-id">#{{ run.id }}</span>
              <span>{{ startedText(run) }}</span>
              <span v-if="!subject && run.subject_label" class="run-subject">{{ run.subject_label }}</span>
              <!-- 反方向入口：跑挂了得有个去处，新开标签页回到那份产物，抽屉里的
                   现场留着接着看。 -->
              <a
                v-if="subjectLinks.get(run.id)"
                :href="subjectLinks.get(run.id)?.href"
                target="_blank"
                rel="noopener"
                class="run-jump"
                @click.stop
              >{{ subjectLinks.get(run.id)?.text }}</a>
              <span class="spacer" />
              <span v-if="run.elapsed_ms != null" class="muted">{{ fmtElapsed(run.elapsed_ms) }}</span>
              <span v-if="run.model_names?.length" class="muted run-models">
                {{ run.model_names.join('、') }}
              </span>
            </span>
          </template>
          <template #extra>
            <a-tag :color="statusColor(run.status)">{{ statusText(run.status) }}</a-tag>
          </template>

          <div v-if="details[run.id]" class="scene">
            <div v-if="inputPairs(run).length" class="scene-inputs">
              <div v-for="[name, value] in inputPairs(run)" :key="name" class="scene-input">
                <span class="muted">{{ name }}</span>
                <pre>{{ value }}</pre>
              </div>
            </div>
            <p v-if="run.error_msg" class="danger">{{ run.error_msg }}</p>

            <div v-for="step in details[run.id].steps" :key="step.id" class="scene-step">
              <div class="scene-head">
                <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
                <span class="live-key">{{ step.key }}</span>
                <span class="muted">{{ step.agent_name || step.agent_slug }}</span>
                <span v-if="step.optional" class="muted">可选</span>
                <span class="spacer" />
                <span v-if="step.model_name" class="muted">{{ step.model_name }}</span>
                <span v-if="step.elapsed_ms != null" class="muted">{{ fmtElapsed(step.elapsed_ms) }}</span>
                <a-tooltip
                  title="用流程当前定义里的这一步重跑一次，上游产物按当时落库的结果喂。跑通这一步不会重写业务结果——要新产物请回功能页面重新发起。"
                >
                  <a-button
                    size="small"
                    :disabled="!canRerun(run)"
                    :loading="rerunning === `${run.id}:${step.key}`"
                    @click="onRerun(run, step)"
                  >
                    重跑这步
                  </a-button>
                </a-tooltip>
              </div>
              <div v-if="step.error_msg" class="danger">{{ step.error_msg }}</div>
              <div v-if="step.prompt" class="scene-block">
                <div class="scene-label">发给模型的 prompt</div>
                <pre>{{ step.prompt }}</pre>
              </div>
              <div v-if="step.output" class="scene-block">
                <div class="scene-label">模型原始输出</div>
                <pre>{{ step.output }}</pre>
              </div>
              <div v-if="step.parsed" class="scene-block">
                <div class="scene-label">按契约解析出的对象</div>
                <pre>{{ JSON.stringify(step.parsed, null, 2) }}</pre>
              </div>
            </div>
          </div>
          <a-skeleton v-else active :paragraph="{ rows: 3 }" />
        </a-collapse-panel>
      </a-collapse>
    </a-spin>
  </a-drawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter, type RouteLocationRaw } from 'vue-router'
import { message } from 'ant-design-vue'
import { PlayCircleOutlined } from '@ant-design/icons-vue'
import {
  workflowsApi,
  type RunStatus,
  type Workflow,
  type WorkflowRun,
  type WorkflowRunDetail,
  type WorkflowRunStep,
  type WorkflowRunSubject,
} from '@/api'
// 状态文案与颜色在 utils/format，工作流卡片上那行「最近一次运行」用的是同一份口径。
import {
  fmtElapsed,
  runStatusColor as statusColor,
  runStatusText as statusText,
} from '@/utils/format'

const props = defineProps<{
  open: boolean
  workflow: Workflow | null
  /** 业务对象模式：列「这个对象跑过的哪几次」，不提供发起入口。 */
  subject?: WorkflowRunSubject | null
}>()

const emit = defineEmits<{ 'update:open': [value: boolean]; changed: [] }>()

const router = useRouter()

/** 一步的实时进度。事件里只带状态与长度，正文从详情接口取。 */
interface LiveStep {
  key: string
  agent_slug: string
  agent_name?: string | null
  optional: boolean
  status: string
  elapsed_ms?: number | null
  output_chars?: number
  error?: string | null
  /** 这一步跑完后实际用的模型；还没跑到的步骤是 null。 */
  model_name?: string | null
}

const runs = ref<WorkflowRun[]>([])
const loading = ref(false)
const runsError = ref('')
/** 后端按条件算好的总条数：界面上的「共 N 次」和翻页都靠它。 */
const total = ref(0)
const PAGE_SIZE = 20
const page = ref(1)
const statusFilter = ref<RunStatus>()
const composerOpen = ref(false)
const running = ref(false)
/** 在跟随一条**别人发起**的流（功能页面的生成、或刚重跑完的那一步）。 */
const following = ref(false)
/** 正在等后端受理的重跑，形状是 ``运行 id:步骤 key``，用来只转那一颗按钮。 */
const rerunning = ref<string | null>(null)
const inputs = reactive<Record<string, string>>({})
const details = reactive<Record<number, WorkflowRunDetail>>({})
const expanded = ref<number[]>([])
const live = reactive<{ runId: number | null; status: string; error: string | null; steps: LiveStep[] }>({
  runId: null,
  status: '',
  error: null,
  steps: [],
})

let abortStream: (() => void) | null = null

const groupCount = computed(
  () => new Set((props.workflow?.steps ?? []).map((step) => step.group_key)).size,
)

const drawerTitle = computed(() =>
  props.subject
    ? `运行记录 · ${props.subject.label ?? props.subject.ref}`
    : `运行 · ${props.workflow?.name ?? ''}`,
)

const liveBusy = computed(() => running.value || following.value)

const emptyText = computed(() => {
  if (statusFilter.value) return '没有符合这个状态的运行'
  return props.subject ? '这个对象还没有运行记录' : '还没有运行记录'
})

/**
 * 那次运行认领的业务对象能不能被深链。
 *
 * 报告页与工作台都在 ``/office`` 下按整数 id 寻址，运行记录的 ``subject_ref``
 * 就是那个 id。面试题不能：它的 ref 是题目在自己记录里的 qid，光凭这一个数落不到
 * 「哪一条面试记录」——与其给一个会跳错的链接，不如只留标题里快照下来的名字。
 */
function subjectLink(run: WorkflowRun): { text: string; href: string } | null {
  const id = Number(run.subject_ref)
  if (!run.subject_kind || !Number.isInteger(id)) return null
  switch (run.subject_kind) {
    case 'resume':
      return openInNewTab('看简历', { name: 'ReportView', params: { kind: 'resume', id } })
    case 'resume-comparison':
      return openInNewTab('看对比', { name: 'ReportView', params: { kind: 'comparison', id } })
    case 'resume-toolkit':
      return openInNewTab('看助手', { name: 'ReportView', params: { kind: 'toolkit', id } })
    case 'slide-deck':
      return openInNewTab('开工作台', { name: 'SlideWorkbench', params: { id } })
    default:
      return null
  }
}

/** 与简历/幻灯片列表「查看报告」同一模式：整页展示是 bare 路由，新开标签页。 */
function openInNewTab(text: string, to: RouteLocationRaw) {
  return { text, href: router.resolve(to).href }
}

/** 按运行 id 索引一次，模板里就不必为同一个对象算三遍。 */
const subjectLinks = computed(() => {
  const links = new Map<number, { text: string; href: string } | null>()
  // 业务对象模式下整列都是同一个对象，用户已经站在它上面了。
  if (props.subject) return links
  for (const run of runs.value) links.set(run.id, subjectLink(run))
  return links
})

const liveCaption = computed(() => {
  if (running.value) return '本次运行'
  if (following.value) return `正在跟随运行 #${live.runId ?? '—'}`
  return `运行 #${live.runId ?? '—'} 的进度`
})

/** 筛选器只给运行会有的状态：``skipped`` 是步骤的「没轮到」，运行本身不存在这个值。 */
const statusOptions = (['pending', 'running', 'succeeded', 'failed', 'cancelled'] as RunStatus[]).map(
  (value) => ({ value, label: statusText(value) }),
)

function onFilterChange() {
  page.value = 1
  void loadRuns()
}
function startedText(run: WorkflowRun) {
  const started = run.started_at || run.created_at
  return started ? new Date(started).toLocaleString() : '—'
}
function inputPairs(run: WorkflowRun): [string, string][] {
  const detail = details[run.id]
  if (!detail) return []
  return Object.entries(detail.inputs).filter(([, value]) => value)
}

watch(
  () => props.open,
  (open) => {
    if (!open) {
      stopFollow()
      return
    }
    resetComposer()
    expanded.value = []
    // 每次打开都回默认视图：上次筛的是「失败」，这次进来却显示全部，比多一次
    // 点击更容易看错。
    page.value = 1
    statusFilter.value = undefined
    runsError.value = ''
    void loadRuns()
  },
)

async function loadRuns() {
  if (!props.workflow && !props.subject) return
  loading.value = true
  try {
    const data = await workflowsApi.listRuns({
      ...(props.subject
        ? { subject: [props.subject.kind, props.subject.ref] }
        : { workflowId: props.workflow?.id }),
      status: statusFilter.value,
      page: page.value,
      pageSize: PAGE_SIZE,
    })
    runs.value = data.items
    total.value = data.total
    runsError.value = ''
    if (props.subject && runs.value.length) {
      // 业务对象模式下点开就是最近那次的现场，不必再手翻一次折叠面板。
      expanded.value = [runs.value[0].id]
      await onExpand(expanded.value)
    }
    // 最新那次还在跑就接上它的进度——功能页面发起的生成本来就不在任何流里跑，
    // 而从这一页发起的那次，关掉再重开也早就断了流。不接的话这里只能看到打开
    // 抽屉那一刻的快照，得手动刷新才知道跑完了。
    const newest = runs.value[0]
    if (!liveBusy.value && newest && (newest.status === 'pending' || newest.status === 'running')) {
      attachFollow(newest.id)
    }
  } catch (err) {
    runsError.value = errorText(err, '请检查网络后重试')
  } finally {
    loading.value = false
  }
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

async function onExpand(keys: number | number[]) {
  const wanted = Array.isArray(keys) ? keys : [keys]
  for (const runId of wanted) {
    if (details[runId]) continue
    try {
      details[runId] = await workflowsApi.runDetail(runId)
    } catch {
      return // 拦截器已提示。
    }
  }
}

function openComposer() {
  for (const field of props.workflow?.inputs ?? []) inputs[field.name] = ''
  composerOpen.value = true
}

function resetComposer() {
  composerOpen.value = false
  running.value = false
  following.value = false
  live.runId = null
  live.status = ''
  live.error = null
  live.steps = []
}

/**
 * 进度事件的处理：发起那条流和中途接上的这条流形状一致，所以只写一套。
 * 事件里只有状态与长度，正文一律从详情接口按步取。
 */
function applyStepStart(data: { key: string; status?: string }) {
  const step = live.steps.find((item) => item.key === data.key)
  if (step) step.status = data.status || 'running'
}

function applyStepEnd(data: {
  key: string
  status: string
  error?: string | null
  output_chars?: number
  elapsed_ms?: number | null
  model_name?: string | null
}) {
  const step = live.steps.find((item) => item.key === data.key)
  if (!step) return
  step.status = data.status
  step.elapsed_ms = data.elapsed_ms
  step.output_chars = data.output_chars
  step.error = data.error
  step.model_name = data.model_name
}

/** 跑完了：换回列表和那次的现场，并让调用方刷新它自己的业务列表。 */
async function settleLive(status: string, error: string | null | undefined, runId: number) {
  running.value = false
  following.value = false
  live.status = status
  live.error = error ?? null
  if (status === 'succeeded') message.success('运行完成')
  // 现场重取：这一步刚写下新的 prompt 与输出，缓存里那份是跑之前的。
  delete details[runId]
  await loadRuns()
  emit('changed')
}

/**
 * 流错误的统一收口：发起与跟随两条流的 onError 形状一致，都要停下过程、
 * 亮出失败态。流的错误没有 axios 拦截器兜底，这里必须自己提示，
 * 否则界面只是停住不动。
 */
function onStreamError(data: { message: string }) {
  running.value = false
  following.value = false
  live.status = 'failed'
  live.error = data.message
  message.error(data.message)
}

function startRun() {
  const workflow = props.workflow
  if (!workflow) return

  live.status = 'running'
  live.error = null
  live.steps = workflow.steps.map((step) => ({
    key: step.key,
    agent_slug: step.agent_slug,
    agent_name: step.agent_name,
    optional: step.optional,
    status: 'pending',
  }))
  running.value = true

  abortStream = workflowsApi.streamRun(workflow.id, { ...inputs }, {
    onRunStart: (data) => {
      live.runId = data.run_id
      const declared = new Map(data.steps.map((step) => [step.key, step]))
      for (const step of live.steps) {
        const info = declared.get(step.key)
        if (info) step.agent_name = info.agent_name ?? step.agent_name
      }
    },
    onStepStart: applyStepStart,
    onStepEnd: applyStepEnd,
    onRunEnd: (data) => void settleLive(data.status, data.error, data.run_id),
    onError: onStreamError,
  })
}

/**
 * 中途接上一条正在跑的运行。
 *
 * 步骤清单由 ``run_start`` 填：它才说得出接上的那一刻每一步是什么状态——
 * 发起新运行时全是 pending，中途接上则可能已经跑完两步、卡在第三步。
 */
function attachFollow(runId: number) {
  if (liveBusy.value) return
  following.value = true
  live.status = 'running'
  live.error = null
  live.steps = []
  abortStream = workflowsApi.followRun(runId, {
    onRunStart: (data) => {
      live.runId = data.run_id
      live.status = data.status || 'running'
      live.steps = data.steps.map((step) => ({
        key: step.key,
        agent_slug: step.agent_slug,
        agent_name: step.agent_name,
        optional: step.optional,
        status: step.status || 'pending',
        model_name: step.model_name,
      }))
    },
    onStepStart: applyStepStart,
    onStepEnd: applyStepEnd,
    onRunEnd: (data) => void settleLive(data.status, data.error, data.run_id),
    onError: onStreamError,
  })
}

function stopFollow() {
  abortStream?.()
  abortStream = null
}

/**
 * 停止跟随。两种情况不一样：发起的那次一断流，执行器就在下一个组边界收手，
 * 这次运行会变成「已取消」；而只是接上的那条流断了不动运行本身，清掉面板、
 * 回列表看刷新后的状态。
 */
function stopLive() {
  const startedHere = running.value
  stopFollow()
  if (!startedHere) {
    following.value = false
    clearLive()
    void loadRuns()
    return
  }
  running.value = false
  live.status = 'cancelled'
  live.steps = live.steps.map((step) =>
    step.status === 'pending' || step.status === 'running' ? { ...step, status: 'skipped' } : step,
  )
  void loadRuns()
  emit('changed')
}

function clearLive() {
  live.runId = null
  live.status = ''
  live.error = null
  live.steps = []
}

/** 能重跑单步的运行：已经结束的那次，且手上没有别的过程在跟。 */
function canRerun(run: WorkflowRun) {
  if (run.status === 'pending' || run.status === 'running') return false
  return !liveBusy.value
}

async function onRerun(run: WorkflowRun, step: WorkflowRunStep) {
  rerunning.value = `${run.id}:${step.key}`
  try {
    await workflowsApi.rerunStep(run.id, step.key)
  } catch {
    return // 拦截器已提示；409 的原因（还没跑完）就在它的 message 里。
  } finally {
    rerunning.value = null
  }
  // 后端已把这一步打回 pending、整条运行改回执行中：接上跟随流看它跑完。
  // 旧的现场先撤下来，跑完再取——留着一屏上一次的输出和新结果分不出。
  delete details[run.id]
  attachFollow(run.id)
}
</script>

<style scoped>
.toolbar-top {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.composer {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px;
  background: var(--paper);
}
.composer-actions {
  margin-top: 8px;
}
.live {
  margin: 12px 0;
}
.live-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 13px;
}
.live-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}
.live-key {
  font-family: var(--font-mono);
}
.scene-inputs {
  margin-bottom: 12px;
}
.run-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  white-space: nowrap;
}
/* 折叠面板把标题项给成 flex:auto 且不可缩，长标题会把右侧的状态 tag 顶出抽屉，
   所以这里放开最小宽度，让下面的省略号有机会生效。 */
:deep(.ant-collapse-header-text) {
  min-width: 0;
}
.run-id {
  font-family: var(--font-mono);
}
.run-subject {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-models {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-jump {
  font-size: 12px;
}
.runs-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
/* 加载失败警告条：与筛选栏、列表拉开一点距离。 */
.load-error {
  margin-bottom: 12px;
}
.runs-filter {
  width: 112px;
}
.scene-input pre,
.scene-block pre {
  margin: 4px 0 0;
  padding: 8px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.6;
}
.scene-step {
  border-top: 1px solid var(--hairline);
  padding-top: 8px;
  margin-top: 8px;
}
.scene-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.scene-block {
  margin-top: 8px;
}
.scene-label {
  color: var(--text-3);
  font-size: 12px;
}
.spacer {
  flex: 1;
}
.danger {
  color: #cf1322;
  font-size: 13px;
  word-break: break-word;
}
</style>
