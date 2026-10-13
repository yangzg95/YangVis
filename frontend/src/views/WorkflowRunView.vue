<template>
  <div class="run-page">
    <header class="page-head">
      <div class="head-main">
        <span class="head-title">运行 #{{ runId }}</span>
        <a-tag v-if="detail" :color="statusColor(detail.status)">{{ statusText(detail.status) }}</a-tag>
        <span class="muted">{{ detail?.workflow_slug }}</span>
        <span v-if="detail?.subject_label" class="muted">· {{ detail.subject_label }}</span>
        <a
          v-if="subjectHref"
          :href="subjectHref.href"
          target="_blank"
          rel="noopener"
          class="head-link"
        >{{ subjectHref.text }}</a>
      </div>
      <div class="head-meta">
        <span v-if="detail" class="muted">{{ headMeta }}</span>
        <a-button size="small" :loading="loading" @click="load">刷新</a-button>
        <a-button size="small" @click="router.push({ name: 'Workflows' })">工作流页</a-button>
      </div>
    </header>

    <!-- 还在跑：接上它的进度。事件只报状态，正文在下面的现场按步取。 -->
    <div v-if="live.steps.length" class="live">
      <div class="live-head">
        <a-tag :color="statusColor(live.status)">{{ statusText(live.status) }}</a-tag>
        <span class="muted">实时进度</span>
        <span class="spacer" />
        <a-button v-if="liveBusy" size="small" @click="stopFollow">停止跟随</a-button>
      </div>
      <div v-for="step in live.steps" :key="step.key" class="live-step">
        <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
        <span class="live-key">{{ step.key }}</span>
        <span class="muted">{{ step.agent_name || step.agent_slug }}</span>
        <span class="spacer" />
        <span v-if="step.model_name" class="muted">{{ step.model_name }}</span>
        <span v-if="step.error" class="danger">{{ step.error }}</span>
      </div>
      <a-alert
        v-if="live.status === 'failed' || live.status === 'cancelled'"
        type="error"
        :message="live.error || '运行失败'"
        show-icon
      />
    </div>

    <a-spin :spinning="loading && !detail" class="page-spin">
      <a-result v-if="errorText" status="warning" :title="errorText">
        <template #extra>
          <a-button size="small" @click="load">重试</a-button>
        </template>
      </a-result>
      <RunScene
        v-else-if="detail"
        :detail="detail"
        :busy="liveBusy"
        @rerun="(step: WorkflowRunStep) => openStepModal(step, 'rerun')"
        @resume="(step: WorkflowRunStep) => openStepModal(step, 'resume')"
      />
    </a-spin>

    <StepOverrideModal
      v-model:open="editModal.open"
      :mode="editModal.mode"
      :fields="editModal.fields"
      :submitting="editModal.submitting"
      @confirm="confirmStepModal"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  workflowsApi,
  type WorkflowRunDetail,
  type WorkflowRunStep,
} from '@/api'
import { fmtElapsed, runStatusColor as statusColor, runStatusText as statusText } from '@/utils/format'
import RunScene from '@/components/workflow/RunScene.vue'
import StepOverrideModal from '@/components/workflow/StepOverrideModal.vue'
import { stepInputFields, type StepField } from '@/components/workflow/runFields'
import { subjectLink } from '@/components/workflow/runLinks'

const route = useRoute()
const router = useRouter()

const runId = Number(route.params.id)
const detail = ref<WorkflowRunDetail | null>(null)
const loading = ref(false)
const errorText = ref('')

/** 一步的实时进度。事件里只带状态，正文从详情接口按步取。 */
interface LiveStep {
  key: string
  agent_slug: string
  agent_name?: string | null
  status: string
  error?: string | null
  model_name?: string | null
}

const liveBusy = ref(false)
const live = reactive<{ status: string; error: string | null; steps: LiveStep[] }>({
  status: '',
  error: null,
  steps: [],
})

let abortStream: (() => void) | null = null

const subjectHref = computed(() => (detail.value ? subjectLink(router, detail.value) : null))

const headMeta = computed(() => {
  const run = detail.value
  if (!run) return ''
  const started = run.started_at || run.created_at
  const parts = [started ? new Date(started).toLocaleString() : '—']
  if (run.elapsed_ms != null) parts.push(fmtElapsed(run.elapsed_ms))
  if (run.model_names?.length) parts.push(run.model_names.join('、'))
  return parts.join(' · ')
})

async function load() {
  loading.value = true
  try {
    detail.value = await workflowsApi.runDetail(runId)
    errorText.value = ''
    // 打开时还在跑就自动接上进度；正在跟就不必再开第二条流。
    if (
      !liveBusy.value &&
      (detail.value.status === 'pending' || detail.value.status === 'running')
    ) {
      attachFollow()
    }
  } catch (err) {
    errorText.value = (err as { message?: string })?.message || '运行记录加载失败'
  } finally {
    loading.value = false
  }
}

function applyStepStart(data: { key: string; status?: string }) {
  const step = live.steps.find((item) => item.key === data.key)
  if (step) step.status = data.status || 'running'
}

function applyStepEnd(data: {
  key: string
  status: string
  error?: string | null
  model_name?: string | null
}) {
  const step = live.steps.find((item) => item.key === data.key)
  if (!step) return
  step.status = data.status
  step.error = data.error
  step.model_name = data.model_name
}

/** 跑完了：撤掉实时面板，重取现场（新的 prompt 与输出刚落库，手上那份是跑之前的）。 */
async function settleLive(status: string, error: string | null | undefined) {
  liveBusy.value = false
  live.status = status
  live.error = error ?? null
  if (status === 'succeeded') message.success('运行完成')
  await load()
  live.steps = []
}

function onStreamError(data: { message: string }) {
  liveBusy.value = false
  live.status = 'failed'
  live.error = data.message
  message.error(data.message)
}

function attachFollow() {
  if (liveBusy.value) return
  liveBusy.value = true
  live.status = 'running'
  live.error = null
  live.steps = []
  abortStream = workflowsApi.followRun(runId, {
    onRunStart: (data) => {
      live.status = data.status || 'running'
      live.steps = data.steps.map((step) => ({
        key: step.key,
        agent_slug: step.agent_slug,
        agent_name: step.agent_name,
        status: step.status || 'pending',
        model_name: step.model_name,
      }))
    },
    onStepStart: applyStepStart,
    onStepEnd: applyStepEnd,
    onRunEnd: (data) => void settleLive(data.status, data.error),
    onError: onStreamError,
  })
}

function stopFollow() {
  abortStream?.()
  abortStream = null
  liveBusy.value = false
  live.steps = []
  live.status = ''
  live.error = null
}

// ---- 单步重跑 / 从此步继续 ----------------------------------------------------

const editModal = reactive<{
  open: boolean
  mode: 'rerun' | 'resume'
  key: string
  fields: StepField[]
  submitting: boolean
}>({
  open: false,
  mode: 'rerun',
  key: '',
  fields: [],
  submitting: false,
})

function openStepModal(step: WorkflowRunStep, mode: 'rerun' | 'resume') {
  if (!detail.value) return
  editModal.mode = mode
  editModal.key = step.key
  editModal.fields = stepInputFields(detail.value, step)
  editModal.open = true
}

async function confirmStepModal(values: Record<string, string>) {
  editModal.submitting = true
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
  detail.value = null
  attachFollow()
}

void load()
onBeforeUnmount(stopFollow)
</script>

<style scoped>
.run-page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 20px 24px 48px;
}
.page-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--hairline);
  margin-bottom: 12px;
}
.head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.head-title {
  font-weight: 600;
}
.head-link {
  font-size: var(--fs-xs);
}
.head-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.live {
  margin-bottom: 12px;
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
.spacer {
  flex: 1;
}
.danger {
  color: var(--danger);
  font-size: var(--fs-sm);
  word-break: break-word;
}
</style>
