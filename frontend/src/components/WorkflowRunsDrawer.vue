<template>
  <a-drawer
    :open="open"
    :title="drawerTitle"
    placement="right"
    width="min(1240px, 94vw)"
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
        <a-form-item v-for="field in requiredFields" :key="field.name" :required="true">
          <template #label>
            {{ field.label }}
            <span class="muted">{{ field.name }}</span>
          </template>
          <a-textarea v-model:value="inputs[field.name]" :rows="4" :disabled="running" />
        </a-form-item>

        <!-- 选填参数默认折叠；填了会在折叠行上报个数，收起也不会忘了下面有内容。 -->
        <div v-if="optionalFields.length" class="composer-optional">
          <div class="group-head" @click="optionalOpen = !optionalOpen">
            <span class="caret">
              <RightOutlined v-if="!optionalOpen" />
              <DownOutlined v-else />
            </span>
            <span class="muted">
              选填参数 · {{ optionalFields.length }} 项
              <template v-if="filledOptionalCount">（已填 {{ filledOptionalCount }}）</template>
            </span>
          </div>
          <div v-show="optionalOpen">
            <a-form-item v-for="field in optionalFields" :key="field.name">
              <template #label>
                {{ field.label }}
                <span class="muted">{{ field.name }}</span>
              </template>
              <a-textarea v-model:value="inputs[field.name]" :rows="3" :disabled="running" />
            </a-form-item>
          </div>
        </div>

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
        <span v-if="running" class="muted">关掉抽屉不会中断运行，进度随时回来接着看。</span>
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
                v-if="linkFor(run)"
                :href="linkFor(run)?.href"
                target="_blank"
                rel="noopener"
                class="run-jump"
                @click.stop
              >{{ linkFor(run)?.text }}</a>
              <!-- 抽屉宽度终究有限：完整现场新开标签页进整页（WorkflowRunView），
                   长输入与逐步的输入/输出分组在那里看才不挤。 -->
              <a
                :href="runPageHref(run)"
                target="_blank"
                rel="noopener"
                class="run-jump"
                @click.stop
              >完整现场</a>
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

          <RunScene
            v-if="details[run.id]"
            :detail="details[run.id]"
            :busy="!canRerun(run)"
            @rerun="(step: WorkflowRunStep) => openStepModal(run, step, 'rerun')"
            @resume="(step: WorkflowRunStep) => openStepModal(run, step, 'resume')"
          />
          <a-skeleton v-else active :paragraph="{ rows: 3 }" />
        </a-collapse-panel>
      </a-collapse>
    </a-spin>

    <!-- 重跑这步 / 从此步继续 共用的输入调整弹框：改的是这次执行收到的上下文
         变量，提交后按当前模板重新渲染，不写回运行输入与任何上游行。 -->
    <StepOverrideModal
      v-model:open="editModal.open"
      :mode="editModal.mode"
      :fields="editModal.fields"
      :submitting="editModal.submitting"
      @confirm="confirmStepModal"
    />
  </a-drawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { DownOutlined, PlayCircleOutlined, RightOutlined } from '@ant-design/icons-vue'
import RunScene from '@/components/workflow/RunScene.vue'
import StepOverrideModal from '@/components/workflow/StepOverrideModal.vue'
import { stepInputFields, type StepField } from '@/components/workflow/runFields'
import { subjectLink } from '@/components/workflow/runLinks'
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
/** 本次打开里用户明点过「停止跟随」：不再自动接回还在跑的那次。 */
let followSuppressed = false
const inputs = reactive<Record<string, string>>({})
/** 发起表单里选填参数一栏的展开状态（默认折叠，见模板分组注释）。 */
const optionalOpen = ref(false)
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

/** 业务对象深链与「完整现场」整页：都新开标签页，见 runLinks 与 WorkflowRunView。 */
function linkFor(run: WorkflowRun) {
  return subjectLink(router, run)
}

function runPageHref(run: WorkflowRun) {
  return router.resolve({ name: 'WorkflowRunView', params: { id: run.id } }).href
}

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

watch(
  () => props.open,
  (open) => {
    if (!open) {
      stopFollow()
      return
    }
    resetComposer()
    followSuppressed = false
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
    // 抽屉那一刻的快照，得手动刷新才知道跑完了。用户明点过停止跟随的例外：
    // 那一手是「别跟了」，不是「换个姿势接着跟」。
    const newest = runs.value[0]
    if (
      !liveBusy.value &&
      !followSuppressed &&
      newest &&
      (newest.status === 'pending' || newest.status === 'running')
    ) {
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
  // 面板还展开着就把新现场取回来：续跑/重跑后直接看到新结果，不用切一下。
  if (expanded.value.includes(runId)) await onExpand([runId])
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
 * 停止跟随。执行挂在后端自己的任务上：断这条流不会让运行停下来，
 * 发起的那次也一样——这里只是收起面板、回列表看刷新后的状态，
 * 本次打开里不再自动接回；想看进度，重开抽屉会接上还在跑的那次。
 */
function stopLive() {
  followSuppressed = true
  stopFollow()
  running.value = false
  following.value = false
  clearLive()
  void loadRuns()
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

// ---- 每步现场折叠与输入还原在 RunScene / runFields：抽屉与整页共用一份形状。 ----

// ---- 重跑 / 续跑的输入调整弹框 -----------------------------------------------

const editModal = reactive<{
  open: boolean
  mode: 'rerun' | 'resume'
  runId: number
  key: string
  fields: StepField[]
  submitting: boolean
}>({
  open: false,
  mode: 'rerun',
  runId: 0,
  key: '',
  fields: [],
  submitting: false,
})

function openStepModal(run: WorkflowRun, step: WorkflowRunStep, mode: 'rerun' | 'resume') {
  const detail = details[run.id]
  if (!detail) return
  editModal.mode = mode
  editModal.runId = run.id
  editModal.key = step.key
  editModal.fields = stepInputFields(detail, step)
  editModal.open = true
}

async function confirmStepModal(values: Record<string, string>) {
  editModal.submitting = true
  const runId = editModal.runId
  try {
    if (editModal.mode === 'rerun') {
      await workflowsApi.rerunStep(runId, editModal.key, values)
    } else {
      await workflowsApi.resumeFromStep(runId, editModal.key, values)
    }
  } catch {
    return // 拦截器已提示：400（未知变量/JSON 非法）、409（还没跑完）原因在 message。
  } finally {
    editModal.submitting = false
  }
  editModal.open = false
  // 旧现场先撤：后端已把目标行打回 pending、整条运行改回执行中，跟随流看它跑完。
  // RunScene 拿到重取的新 detail 会自己回到默认折叠（失败步重新展开）。
  delete details[runId]
  attachFollow(runId)
}

// ---- 发起表单：必填平铺，选填默认折叠 ----------------------------------------

const requiredFields = computed(
  () => (props.workflow?.inputs ?? []).filter((field) => field.required),
)
const optionalFields = computed(
  () => (props.workflow?.inputs ?? []).filter((field) => !field.required),
)
/** 折叠行上报「已填 N」：选填收起来了，填没填也要看得见。 */
const filledOptionalCount = computed(
  () => optionalFields.value.filter((field) => (inputs[field.name] ?? '').trim()).length,
)
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
  font-size: var(--fs-sm);
}
.live-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: var(--fs-sm);
}
.live-key {
  font-family: var(--font-mono);
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
  font-size: var(--fs-xs);
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
/* 发起表单的选填分组头：折叠状态也要看得见填了几项，与整页/现场的分组头同一形状。 */
.composer-optional {
  margin-bottom: 12px;
}
.group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.caret {
  display: inline-flex;
  align-items: center;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.spacer {
  flex: 1;
}
.danger {
  color: var(--danger);
  font-size: var(--fs-sm);
  word-break: break-word;
}
</style>
