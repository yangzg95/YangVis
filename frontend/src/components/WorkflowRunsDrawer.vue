<template>
  <a-drawer
    :open="open"
    :title="`运行 · ${workflow?.name ?? ''}`"
    placement="right"
    :width="860"
    :mask-closable="false"
    @update:open="(value: boolean) => emit('update:open', value)"
  >
    <template #extra>
      <a-button size="small" :loading="loading" @click="loadRuns">刷新</a-button>
    </template>

    <!-- 发起：填这条流程声明的输入变量。 -->
    <div v-if="!composerOpen" class="toolbar-top">
      <a-button type="primary" size="small" :disabled="!workflow?.steps.length" @click="openComposer">
        <PlayCircleOutlined /> 发起一次运行
      </a-button>
      <span class="muted">
        {{ workflow?.steps.length }} 步 · {{ groupCount }} 个并行组
      </span>
    </div>

    <div v-else class="composer">
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

      <!-- 进度：事件只报状态，正文由下面的历史详情按步取。 -->
      <div v-if="live.steps.length" class="live">
        <div v-for="step in live.steps" :key="step.key" class="live-step">
          <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
          <span class="live-key">{{ step.key }}</span>
          <span class="muted">{{ step.agent_name || step.agent_slug }}</span>
          <span v-if="step.optional" class="muted">可选</span>
          <span class="spacer" />
          <span v-if="step.output_chars" class="muted">{{ step.output_chars }} 字</span>
          <span v-if="step.elapsed_ms != null" class="muted">{{ step.elapsed_ms }} ms</span>
          <span v-if="step.error" class="danger">{{ step.error }}</span>
        </div>
        <a-alert
          v-if="live.status === 'failed' || live.status === 'cancelled'"
          type="error"
          :message="live.error || '运行失败'"
          show-icon
        />
      </div>

      <a-space class="composer-actions">
        <a-button v-if="!running" type="primary" size="small" @click="startRun">
          <PlayCircleOutlined /> 开始运行
        </a-button>
        <a-button v-else danger size="small" @click="stopRun">
          <StopOutlined /> 停止跟随
        </a-button>
        <a-button v-if="!running" size="small" @click="resetComposer">收起</a-button>
        <span v-if="running" class="muted">关掉抽屉不会中断已跑完的步骤，进度随时回来接着看。</span>
      </a-space>
    </div>

    <a-divider style="margin: 16px 0">最近运行</a-divider>

    <a-spin :spinning="loading">
      <a-empty v-if="!runs.length" description="还没有运行记录" />
      <a-collapse v-else v-model:active-key="expanded" @change="onExpand">
        <a-collapse-panel v-for="run in runs" :key="run.id" :header="runTitle(run)">
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
                <span class="muted">{{ step.agent_slug }}</span>
                <span class="spacer" />
                <span v-if="step.elapsed_ms != null" class="muted">{{ step.elapsed_ms }} ms</span>
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
import { message } from 'ant-design-vue'
import { PlayCircleOutlined, StopOutlined } from '@ant-design/icons-vue'
import {
  workflowsApi,
  type Workflow,
  type WorkflowRun,
  type WorkflowRunDetail,
} from '@/api'

const props = defineProps<{ open: boolean; workflow: Workflow | null }>()

const emit = defineEmits<{ 'update:open': [value: boolean] }>()

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
}

const runs = ref<WorkflowRun[]>([])
const loading = ref(false)
const composerOpen = ref(false)
const running = ref(false)
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

const STATUS_TEXT: Record<string, string> = {
  pending: '待运行',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  skipped: '未执行',
}
const STATUS_COLOR: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'warning',
  skipped: 'default',
}

function statusText(status: string) {
  return STATUS_TEXT[status] ?? status
}
function statusColor(status: string) {
  return STATUS_COLOR[status] ?? 'default'
}
function runTitle(run: WorkflowRun) {
  const started = run.started_at || run.created_at
  return `#${run.id} · ${started ? new Date(started).toLocaleString() : '—'}`
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
    void loadRuns()
  },
)

async function loadRuns() {
  if (!props.workflow) return
  loading.value = true
  try {
    const data = await workflowsApi.listRuns({ workflowId: props.workflow.id, limit: 20 })
    runs.value = data.items
  } finally {
    loading.value = false
  }
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
  live.runId = null
  live.status = ''
  live.error = null
  live.steps = []
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
    onStepStart: (data) => {
      const step = live.steps.find((item) => item.key === data.key)
      if (step) step.status = 'running'
    },
    onStepEnd: (data) => {
      const step = live.steps.find((item) => item.key === data.key)
      if (!step) return
      step.status = data.status
      step.elapsed_ms = data.elapsed_ms
      step.output_chars = data.output_chars
      step.error = data.error
    },
    onRunEnd: (data) => {
      running.value = false
      live.status = data.status
      live.error = data.error ?? null
      if (data.status === 'succeeded') message.success('运行完成')
      void loadRuns()
    },
    onError: (data) => {
      running.value = false
      live.status = 'failed'
      live.error = data.message
      // 流的错误没有 axios 拦截器兜底，这里必须自己提示，否则界面只是停住不动。
      message.error(data.message)
    },
  })
}

function stopFollow() {
  abortStream?.()
  abortStream = null
}

function stopRun() {
  stopFollow()
  running.value = false
  live.status = 'cancelled'
  live.steps = live.steps.map((step) =>
    step.status === 'pending' ? { ...step, status: 'skipped' } : step,
  )
  void loadRuns()
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
