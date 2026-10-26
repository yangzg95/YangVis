<template>
  <div class="compare-page">
    <header class="page-head">
      <div class="head-main">
        <span class="head-title">运行对比</span>
        <span v-if="detailA" class="muted">{{ detailA.workflow_slug }}</span>
        <span v-if="detailA?.subject_label" class="muted">· {{ detailA.subject_label }}</span>
      </div>
      <div class="head-meta">
        <a-button size="small" @click="router.push({ name: 'Workflows' })">工作流页</a-button>
      </div>
    </header>

    <!-- A/B 选择：候选只列同一条流程的运行，跨流程步骤对不齐，后端也会拒。 -->
    <div class="pick-bar">
      <div class="pick">
        <span class="pick-tag tag-a">A 基准</span>
        <a-select
          :value="runAId ?? undefined"
          class="pick-select"
          show-search
          :placeholder="loadingA ? '加载中…' : '选择基准运行'"
          :options="runOptions"
          :field-names="{ value: 'id', label: 'text' }"
          :loading="loadingCandidates"
          @change="onPickA"
        />
      </div>
      <a-tooltip title="交换 A / B（差值方向会反过来）">
        <a-button size="small" :disabled="!runBId" @click="swap">
          <SwapOutlined />
        </a-button>
      </a-tooltip>
      <div class="pick">
        <span class="pick-tag tag-b">B 候选</span>
        <a-select
          :value="runBId ?? undefined"
          class="pick-select"
          show-search
          placeholder="选择另一次运行作为对比"
          :options="runOptions.filter((item) => item.id !== runAId)"
          :field-names="{ value: 'id', label: 'text' }"
          :loading="loadingCandidates"
          allow-clear
          @change="onPickB"
        />
      </div>
    </div>

    <a-spin :spinning="loadingA || loadingB">
      <a-alert v-if="errorText" type="error" show-icon :message="errorText" class="load-error">
        <template #action>
          <a-button size="small" danger @click="reload">重试</a-button>
        </template>
      </a-alert>

      <a-empty
        v-else-if="detailA && !runBId"
        description="选好右侧的 B 运行后，两次运行会按步骤并排展示，可逐条打分"
      />

      <template v-else-if="detailA && detailB">
        <!-- 运行摘要头 -->
        <div class="run-meta cols">
          <div v-for="(detail, side) in [detailA, detailB]" :key="side" class="col run-meta-col">
            <a-tag :color="statusColor(detail.status)">{{ statusText(detail.status) }}</a-tag>
            <span class="mono">#{{ detail.id }}</span>
            <a :class="['run-link', side === 0 ? 'tag-a-text' : 'tag-b-text']"
               href="#" @click.prevent="openRun(detail.id)">完整现场</a>
            <span class="spacer" />
            <span class="muted">{{ startedText(detail) }}</span>
            <span v-if="detail.elapsed_ms != null" class="muted">{{ fmtElapsed(detail.elapsed_ms) }}</span>
            <span v-if="detail.model_names?.length" class="muted models">
              {{ detail.model_names.join('、') }}
            </span>
          </div>
        </div>

        <!-- 运行输入：长材料默认折起来，要核对入参再展开。 -->
        <details class="inputs-row">
          <summary class="muted">运行输入对比</summary>
          <div class="cols">
            <div v-for="(detail, side) in [detailA, detailB]" :key="side" class="col">
              <pre>{{ formatInputs(detail.inputs) }}</pre>
            </div>
          </div>
        </details>

        <!-- 逐步对齐 -->
        <div class="steps">
          <div v-for="row in alignedSteps" :key="row.key" class="step-row">
            <div class="step-key">
              <span class="mono">{{ row.key }}</span>
              <span class="muted">{{ groupOf(row) }}</span>
            </div>
            <div class="cols">
              <div v-for="(step, side) in [row.a, row.b]" :key="side" class="col">
                <div v-if="!step" class="step-missing muted">该次运行没有这一步</div>
                <template v-else>
                  <div class="step-head">
                    <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
                    <a-tag
                      v-if="personaDiffers(row, side === 0 ? 'a' : 'b')"
                      color="orange"
                      title="两侧绑定的人设版本不同：输出差异可能来自人设改版"
                    >人设版本不同</a-tag>
                    <span class="spacer" />
                    <span v-if="step.model_name" class="muted">{{ step.model_name }}</span>
                    <span v-if="step.elapsed_ms != null" class="muted">{{ fmtElapsed(step.elapsed_ms) }}</span>
                  </div>
                  <div v-if="step.error_msg" class="danger">{{ step.error_msg }}</div>
                  <div v-if="step.output || step.parsed" class="step-out">
                    <div class="out-tools">
                      <a-radio-group
                        v-if="step.parsed && step.output"
                        v-model:value="viewModes[viewKey(step.id)]"
                        size="small"
                        option-type="button"
                        button-style="solid"
                      >
                        <a-radio-button value="parsed">解析对象</a-radio-button>
                        <a-radio-button value="raw">原始输出</a-radio-button>
                      </a-radio-group>
                      <span v-else-if="step.parsed" class="muted">解析对象</span>
                      <span v-else class="muted">{{ step.output?.length ?? 0 }} 字</span>
                    </div>
                    <pre>{{ outText(step) }}</pre>
                  </div>
                  <details v-if="step.prompt" class="step-prompt">
                    <summary class="muted">发给模型的 prompt</summary>
                    <pre>{{ step.prompt }}</pre>
                  </details>
                  <details
                    v-if="step.agent_version"
                    class="step-prompt"
                    @toggle="(e: Event) => togglePersona(e, step)"
                  >
                    <summary class="muted">
                      当时的人设 · v{{ step.agent_version }}
                      <span v-if="personaDiffers(row, side === 0 ? 'a' : 'b')" class="danger-inline">与对侧不同</span>
                    </summary>
                    <template v-if="personaKey(step) && personaCache.get(personaKey(step)!)">
                      <div class="muted persona-meta">
                        {{ personaCache.get(personaKey(step)!)?.snapshot.name }}
                        <span v-if="personaCache.get(personaKey(step)!)?.snapshot.temperature != null">
                          · 温度 {{ personaCache.get(personaKey(step)!)?.snapshot.temperature }}
                        </span>
                      </div>
                      <pre>{{ personaCache.get(personaKey(step)!)?.snapshot.system_prompt }}</pre>
                    </template>
                    <span v-else class="muted">{{ personaLoading.has(personaKey(step) ?? '') ? '加载中…' : '展开加载' }}</span>
                  </details>
                </template>
              </div>
            </div>
          </div>
        </div>

        <!-- 评测 -->
        <section class="eval-card">
          <div class="eval-head">
            <span class="eval-title">评分对比</span>
            <span class="muted">每条标准给 A、B 各打 1–5 分，保存的是非加权均分</span>
          </div>

          <!-- 这一对已保存过的评测：点一行回填，可在原尺子上改分后再存（后端按更新处理）。 -->
          <div v-if="evaluations.length" class="saved-row">
            <span class="muted">本对比已有 {{ evaluations.length }} 份评测：</span>
            <a-tag
              v-for="item in evaluations"
              :key="item.id"
              :color="item.id === activeEvaluationId ? 'blue' : undefined"
              class="saved-tag"
              @click="loadEvaluation(item)"
            >
              {{ item.rubric_name || '（已删除的细则）' }}
              · {{ item.avg_a }} vs {{ item.avg_b }}
            </a-tag>
          </div>

          <div class="rubric-bar">
            <a-select
              v-model:value="rubricId"
              class="rubric-select"
              placeholder="选择评分细则"
              :options="rubricOptions"
              :field-names="{ value: 'id', label: 'name' }"
              @change="onPickRubric"
            />
            <a-button v-if="!editingRubric" size="small" @click="startCreateRubric">新建细则</a-button>
            <a-button v-if="rubricId && !editingRubric" size="small" type="link" @click="startEditRubric">
              编辑这份
            </a-button>
          </div>

          <!-- 细则新建 / 编辑：细则是可复用资产，先存细则再打分。 -->
          <div v-if="editingRubric" class="rubric-form">
            <a-form layout="vertical" size="small">
              <div class="rubric-form-row">
                <a-form-item label="名称" required>
                  <a-input v-model:value="rubricForm.name" :maxlength="128" />
                </a-form-item>
                <a-form-item label="说明">
                  <a-input v-model:value="rubricForm.description" :maxlength="512" />
                </a-form-item>
              </div>
              <div class="muted rubric-hint">评分标准 2–6 条，顺序就是打分表上的顺序</div>
              <div v-for="(criterion, index) in rubricForm.criteria" :key="index" class="criterion-row">
                <a-input
                  v-model:value="criterion.name"
                  :placeholder="`标准 ${index + 1} 名称`"
                  :maxlength="128"
                  class="criterion-name"
                />
                <a-input
                  v-model:value="criterion.description"
                  placeholder="怎么打分（可选）"
                  :maxlength="512"
                />
                <a-button
                  size="small"
                  danger
                  type="text"
                  :disabled="rubricForm.criteria.length <= 2"
                  @click="removeCriterion(index)"
                >
                  <DeleteOutlined />
                </a-button>
              </div>
              <div class="rubric-actions">
                <a-button size="small" :disabled="rubricForm.criteria.length >= 6" @click="addCriterion">
                  加一条标准
                </a-button>
                <span class="spacer" />
                <a-button size="small" @click="cancelRubricEdit">取消</a-button>
                <a-button size="small" type="primary" :loading="savingRubric" @click="saveRubric">
                  保存细则
                </a-button>
              </div>
            </a-form>
          </div>

          <template v-else-if="activeCriteria.length">
            <div class="score-table">
              <div class="score-row score-row-head">
                <span class="criterion-col">评分标准</span>
                <span class="rate-col tag-a-text">A 基准 #{{ runAId }}</span>
                <span class="rate-col tag-b-text">B 候选 #{{ runBId }}</span>
              </div>
              <div v-for="(criterion, index) in activeCriteria" :key="index" class="score-row">
                <div class="criterion-col">
                  <div class="criterion-title">{{ index + 1 }}. {{ criterion.name }}</div>
                  <div v-if="criterion.description" class="muted criterion-desc">
                    {{ criterion.description }}
                  </div>
                </div>
                <div class="rate-col">
                  <a-rate :value="draftScores[index]?.a ?? 0" :count="5"
                          @change="(v: number) => setScore(index, 'a', v)" />
                </div>
                <div class="rate-col">
                  <a-rate :value="draftScores[index]?.b ?? 0" :count="5"
                          @change="(v: number) => setScore(index, 'b', v)" />
                </div>
              </div>
            </div>

            <div class="verdict">
              <div class="verdict-scores">
                <span>A 均分 <b class="tag-a-text">{{ avgA ?? '—' }}</b></span>
                <span>B 均分 <b class="tag-b-text">{{ avgB ?? '—' }}</b></span>
                <a-tag :color="diffColor">{{
                  diff == null ? '待打分' : diff > 0 ? `B 优 ${diff}` : diff < 0 ? `A 优 ${-diff}` : '持平'
                }}</a-tag>
              </div>
              <a-textarea
                v-model:value="note"
                class="verdict-note"
                :rows="2"
                placeholder="总体结论：哪版好、为什么（由你决定，分数不替人下判断）"
                :maxlength="10000"
              />
              <div class="verdict-actions">
                <a-popconfirm
                  v-if="activeEvaluationId"
                  title="删除这份评测？"
                  @confirm="removeEvaluation"
                >
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
                <span class="spacer" />
                <a-button
                  size="small"
                  type="primary"
                  :loading="savingEval"
                  :disabled="!allScored"
                  @click="saveEvaluation"
                >
                  保存评测
                </a-button>
              </div>
            </div>
          </template>

          <a-empty v-else description="选择或新建一份评分细则后开始打分" :image-style="{ height: '40px' }" />
        </section>
      </template>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { DeleteOutlined, SwapOutlined } from '@ant-design/icons-vue'
import {
  agentsApi,
  evalApi,
  workflowsApi,
  type AgentVersionDetail,
  type EvalCriterion,
  type EvalRubric,
  type WorkflowEvaluation,
  type WorkflowRun,
  type WorkflowRunDetail,
  type WorkflowRunStep,
} from '@/api'
import { fmtElapsed, runStatusColor as statusColor, runStatusText as statusText } from '@/utils/format'

const route = useRoute()
const router = useRouter()

// 与 WorkflowRunView 同一套路：顶栏多标签下组件被 keep-alive 复用，
// 运行 id 跟着路由参数换。
const runAId = ref<number | null>(route.params.a ? Number(route.params.a) : null)
const runBId = ref<number | null>(route.params.b ? Number(route.params.b) : null)

const detailA = ref<WorkflowRunDetail | null>(null)
const detailB = ref<WorkflowRunDetail | null>(null)
const loadingA = ref(false)
const loadingB = ref(false)
const loadingCandidates = ref(false)
const errorText = ref('')
const candidates = ref<WorkflowRun[]>([])

/** 每步输出的查看模式（解析对象 / 原始输出），按步骤行 id 记住选择。 */
const viewModes = reactive<Record<string, 'parsed' | 'raw'>>({})

/** 人设版本快照按 agent_id:version 缓存：展开详情时才懒加载，跨步骤共享。 */
const personaCache = reactive(new Map<string, AgentVersionDetail>())
const personaLoading = reactive(new Set<string>())

function personaKey(step: WorkflowRunStep): string | null {
  if (step.agent_id == null || step.agent_version == null) return null
  return `${step.agent_id}:${step.agent_version}`
}

async function togglePersona(event: Event, step: WorkflowRunStep) {
  const details = event.target as HTMLDetailsElement
  if (!details.open) return
  const key = personaKey(step)
  if (key == null || personaCache.has(key) || personaLoading.has(key)) return
  personaLoading.add(key)
  try {
    personaCache.set(
      key,
      await agentsApi.versionDetail(step.agent_id as number, step.agent_version as number),
    )
  } catch {
    // 智能体已删除等 404：拦截器已提示，缓存不写，下次展开可再试。
  } finally {
    personaLoading.delete(key)
  }
}

/** 同一步两侧都绑了人设版本、但不是同一把尺子时，在两侧给个差异提示。 */
function personaDiffers(
  pair: { a?: WorkflowRunStep | null; b?: WorkflowRunStep | null },
  side: 'a' | 'b',
): boolean {
  const left = pair.a
  const right = pair.b
  if (!left?.agent_version || !right?.agent_version) return false
  const same = left.agent_id === right.agent_id && left.agent_version === right.agent_version
  return !same && !!pair[side]?.agent_version
}

const rubrics = ref<EvalRubric[]>([])
const rubricId = ref<number | null>(null)
const evaluations = ref<WorkflowEvaluation[]>([])
const activeEvaluationId = ref<number | null>(null)
const draftScores = reactive<Record<number, { a: number; b: number }>>({})
const note = ref('')
const savingEval = ref(false)
const savingRubric = ref(false)

watch(
  () => [route.params.a, route.params.b],
  ([a, b]) => {
    if (route.name !== 'WorkflowRunCompare' || a == null) return
    const nextA = Number(a)
    const nextB = b == null ? null : Number(b)
    if (nextA === runAId.value && nextB === runBId.value) return
    runAId.value = nextA
    runBId.value = nextB
    void reload()
  },
)

const runOptions = computed(() =>
  candidates.value.map((run) => ({
    id: run.id,
    text: `#${run.id} · ${startedText(run)} · ${statusText(run.status)}${
      run.subject_label ? ` · ${run.subject_label}` : ''
    }`,
  })),
)

function startedText(run: WorkflowRun | WorkflowRunDetail) {
  const started = run.started_at || run.created_at
  return started ? new Date(started).toLocaleString() : '—'
}

function openRun(id: number) {
  router.push({ name: 'WorkflowRunView', params: { id } })
}

async function loadDetail(which: 'A' | 'B') {
  const id = which === 'A' ? runAId.value : runBId.value
  if (id == null) {
    if (which === 'B') detailB.value = null
    return
  }
  const flag = which === 'A' ? loadingA : loadingB
  flag.value = true
  try {
    const data = await workflowsApi.runDetail(id)
    if (id !== (which === 'A' ? runAId.value : runBId.value)) return
    if (which === 'A') detailA.value = data
    else detailB.value = data
    errorText.value = ''
  } catch (err) {
    if (which === 'A') errorText.value = (err as { message?: string })?.message || '基准运行加载失败'
  } finally {
    flag.value = false
  }
}

async function loadCandidates() {
  if (!detailA.value) return
  loadingCandidates.value = true
  try {
    // 同一条流程的运行才是候选；100 条上限足够覆盖对比场景，列表页本身另有分页。
    const data = await workflowsApi.listRuns({
      workflowId: detailA.value.workflow_id,
      page: 1,
      pageSize: 100,
    })
    candidates.value = data.items
  } catch {
    // 拦截器已提示：选择器拿候选不影响现场展示。
  } finally {
    loadingCandidates.value = false
  }
}

async function reload() {
  evaluations.value = []
  activeEvaluationId.value = null
  resetDraft()
  detailB.value = null
  await loadDetail('A')
  await Promise.all([loadCandidates(), loadDetail('B'), loadRubrics()])
  if (runAId.value && runBId.value) await loadEvaluations()
}

function onPickA(id: number) {
  // 换基准后旧的 B 可能已不属于同流程：清掉重选，顺手重置评测草稿。
  router.replace({ name: 'WorkflowRunCompare', params: { a: id } })
}

function onPickB(id: number | undefined) {
  if (id == null) {
    router.replace({ name: 'WorkflowRunCompare', params: { a: runAId.value } })
  } else {
    router.replace({ name: 'WorkflowRunCompare', params: { a: runAId.value, b: id } })
  }
}

function swap() {
  if (!runAId.value || !runBId.value) return
  router.replace({
    name: 'WorkflowRunCompare',
    params: { a: runBId.value, b: runAId.value },
  })
}

// ---- 步骤对齐 -------------------------------------------------------------

const alignedSteps = computed(() => {
  const aSteps = detailA.value?.steps ?? []
  const bSteps = detailB.value?.steps ?? []
  const keys: string[] = []
  for (const step of [...aSteps, ...bSteps]) {
    if (!keys.includes(step.key)) keys.push(step.key)
  }
  return keys.map((key) => ({
    key,
    a: aSteps.find((step) => step.key === key),
    b: bSteps.find((step) => step.key === key),
  }))
})

function groupOf(row: { a?: WorkflowRunStep; b?: WorkflowRunStep }) {
  const group = row.a?.group_key || row.b?.group_key
  return group ? `组 ${group}` : ''
}

function viewKey(stepId: number) {
  return String(stepId)
}

function outText(step: WorkflowRunStep): string {
  const mode = viewModes[viewKey(step.id)] ?? (step.parsed ? 'parsed' : 'raw')
  if (mode === 'parsed' && step.parsed) return JSON.stringify(step.parsed, null, 2)
  return step.output || ''
}

function formatInputs(inputs: Record<string, string>) {
  const filled = Object.entries(inputs || {}).filter(([, value]) => value)
  if (!filled.length) return '（无输入）'
  return filled.map(([name, value]) => `【${name}】\n${value}`).join('\n\n')
}

// ---- 评测 -----------------------------------------------------------------

const rubricOptions = computed(() =>
  rubrics.value.map((rubric) => ({ id: rubric.id, name: `${rubric.name}（${rubric.criteria.length} 条）` })),
)

/** 打分表上展示的标准：优先编辑态的表单，否则取选中细则/回填评测的冻结快照。 */
const activeCriteria = computed<EvalCriterion[]>(() => {
  if (editingRubric.value) return rubricForm.criteria
  if (rubricId.value) {
    return rubrics.value.find((item) => item.id === rubricId.value)?.criteria ?? []
  }
  return []
})

async function loadRubrics() {
  try {
    const data = await evalApi.listRubrics()
    rubrics.value = data.items
  } catch {
    // 拦截器已提示。
  }
}

async function loadEvaluations() {
  if (!runAId.value || !runBId.value) return
  try {
    const data = await evalApi.listForRun(runAId.value, runBId.value)
    evaluations.value = data.items
  } catch {
    // 拦截器已提示。
  }
}

function resetDraft() {
  for (const key of Object.keys(draftScores)) delete draftScores[Number(key)]
  note.value = ''
}

function fillScores(criteriaCount: number, scores: { index: number; score_a: number; score_b: number }[]) {
  resetDraft()
  for (let i = 0; i < criteriaCount; i++) {
    const hit = scores.find((item) => item.index === i)
    draftScores[i] = { a: hit?.score_a ?? 0, b: hit?.score_b ?? 0 }
  }
}

/** a-rate 的落点：新建/编辑细则时标准行数会变，目标键可能还不存在。 */
function setScore(index: number, side: 'a' | 'b', value: number) {
  if (!draftScores[index]) draftScores[index] = { a: 0, b: 0 }
  draftScores[index][side] = value
}

function onPickRubric(id: number) {
  editingRubric.value = false
  activeEvaluationId.value = null
  const rubric = rubrics.value.find((item) => item.id === id)
  if (rubric) fillScores(rubric.criteria.length, [])
  note.value = ''
}

function loadEvaluation(item: WorkflowEvaluation) {
  rubricId.value = item.rubric_id
  editingRubric.value = false
  activeEvaluationId.value = item.id
  fillScores(item.rubric_snapshot.length, item.scores)
  note.value = item.note || ''
}

const editingRubric = ref(false)
const rubricForm = reactive<{ id: number | null; name: string; description: string; criteria: EvalCriterion[] }>({
  id: null,
  name: '',
  description: '',
  criteria: [
    { name: '', description: '' },
    { name: '', description: '' },
  ],
})

function startCreateRubric() {
  activeEvaluationId.value = null
  rubricId.value = null
  Object.assign(rubricForm, {
    id: null,
    name: '',
    description: '',
    criteria: [
      { name: '', description: '' },
      { name: '', description: '' },
    ],
  })
  editingRubric.value = true
}

function startEditRubric() {
  const rubric = rubrics.value.find((item) => item.id === rubricId.value)
  if (!rubric) return
  Object.assign(rubricForm, {
    id: rubric.id,
    name: rubric.name,
    description: rubric.description || '',
    criteria: rubric.criteria.map((item) => ({ ...item, description: item.description || '' })),
  })
  editingRubric.value = true
}

function cancelRubricEdit() {
  editingRubric.value = false
}

function addCriterion() {
  if (rubricForm.criteria.length >= 6) return
  rubricForm.criteria.push({ name: '', description: '' })
}

function removeCriterion(index: number) {
  if (rubricForm.criteria.length <= 2) return
  rubricForm.criteria.splice(index, 1)
}

async function saveRubric() {
  const name = rubricForm.name.trim()
  if (!name) {
    message.warning('请填写细则名称')
    return
  }
  const criteria = rubricForm.criteria
    .map((item) => ({ name: item.name.trim(), description: (item.description ?? '').trim() || null }))
    .filter((item) => item.name)
  if (criteria.length < 2) {
    message.warning('至少要有 2 条有名称的评分标准')
    return
  }
  savingRubric.value = true
  try {
    const saved =
      rubricForm.id == null
        ? await evalApi.createRubric({ name, description: rubricForm.description.trim() || null, criteria })
        : await evalApi.updateRubric(rubricForm.id, {
            name,
            description: rubricForm.description.trim() || null,
            criteria,
          })
    await loadRubrics()
    rubricId.value = saved.id
    editingRubric.value = false
    fillScores(saved.criteria.length, [])
    message.success('评分细则已保存')
  } catch {
    // 拦截器已提示（重名等）。
  } finally {
    savingRubric.value = false
  }
}

const avgA = computed(() => averageOf('a'))
const avgB = computed(() => averageOf('b'))
const diff = computed(() => {
  if (avgA.value == null || avgB.value == null) return null
  return Math.round((avgB.value - avgA.value) * 100) / 100
})
const diffColor = computed(() =>
  diff.value == null ? 'default' : diff.value > 0 ? 'green' : diff.value < 0 ? 'red' : 'default',
)
const allScored = computed(
  () =>
    activeCriteria.value.length > 0 &&
    activeCriteria.value.every((_, i) => (draftScores[i]?.a ?? 0) >= 1 && (draftScores[i]?.b ?? 0) >= 1),
)

function averageOf(side: 'a' | 'b'): number | null {
  if (!activeCriteria.value.length) return null
  const values = activeCriteria.value.map((_, i) => draftScores[i]?.[side] ?? 0).filter((v) => v >= 1)
  if (!values.length) return null
  return Math.round((values.reduce((sum, v) => sum + v, 0) / values.length) * 100) / 100
}

async function saveEvaluation() {
  if (!runAId.value || !runBId.value || !rubricId.value) return
  const payload = {
    run_a_id: runAId.value,
    run_b_id: runBId.value,
    rubric_id: rubricId.value,
    scores: activeCriteria.value.map((_, index) => ({
      index,
      score_a: draftScores[index]?.a ?? 0,
      score_b: draftScores[index]?.b ?? 0,
    })),
    note: note.value.trim() || null,
  }
  savingEval.value = true
  try {
    await evalApi.save(payload)
    message.success(activeEvaluationId.value ? '评测已更新' : '评测已保存')
    await loadEvaluations()
    const saved = evaluations.value.find(
      (item) =>
        item.run_a_id === payload.run_a_id &&
        item.run_b_id === payload.run_b_id &&
        item.rubric_id === payload.rubric_id,
    )
    if (saved) activeEvaluationId.value = saved.id
  } catch {
    // 拦截器已提示（跨流程/分数不齐/404）。
  } finally {
    savingEval.value = false
  }
}

async function removeEvaluation() {
  if (!activeEvaluationId.value) return
  const id = activeEvaluationId.value
  try {
    await evalApi.remove(id)
    message.success('已删除')
    evaluations.value = evaluations.value.filter((item) => item.id !== id)
    activeEvaluationId.value = null
    resetDraft()
  } catch {
    // 拦截器已提示。
  }
}

void reload()
</script>

<style scoped>
.compare-page {
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
.head-meta {
  margin-left: auto;
}
.muted {
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.mono {
  font-family: var(--font-mono);
}
.spacer {
  flex: 1;
}
.danger {
  color: var(--danger);
  font-size: var(--fs-xs);
  word-break: break-word;
}
.load-error {
  margin-bottom: 12px;
}

/* A/B 选择栏 */
.pick-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.pick {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.pick-select {
  flex: 1;
  min-width: 0;
}
.pick-tag {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  font-weight: 600;
}
.tag-a {
  background: rgba(22, 119, 255, 0.1);
  color: #1677ff;
}
.tag-b {
  background: rgba(82, 196, 26, 0.12);
  color: #389e0d;
}
.tag-a-text {
  color: #1677ff;
}
.tag-b-text {
  color: #389e0d;
}

/* 两列对齐布局：整页只有这一套列宽，运行摘要、输入、每步输出都往里套。 */
.cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.col {
  min-width: 0;
}
.run-meta {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  padding: 8px 12px;
  margin-bottom: 8px;
}
.run-meta-col {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-xs);
}
.models {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-link {
  font-size: var(--fs-xs);
}
.inputs-row {
  margin-bottom: 12px;
}
.inputs-row summary {
  cursor: pointer;
  margin-bottom: 4px;
}
.inputs-row pre {
  margin: 0;
  padding: 8px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-xs);
  line-height: 1.6;
}

/* 步骤对齐 */
.steps {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.step-row {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  padding: 8px 12px;
}
.step-key {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: var(--fs-sm);
}
.step-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.step-missing {
  padding: 12px;
  text-align: center;
  border: 1px dashed var(--hairline);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
}
.step-out {
  margin-top: 4px;
}
.out-tools {
  display: flex;
  align-items: center;
  margin-bottom: 2px;
}
.step-out pre,
.step-prompt pre {
  margin: 4px 0 0;
  padding: 8px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.step-prompt {
  margin-top: 4px;
}
.step-prompt summary {
  cursor: pointer;
}
.persona-meta {
  font-size: var(--fs-xs);
  margin-bottom: 4px;
}
.danger-inline {
  color: var(--danger);
  margin-left: 6px;
  font-size: var(--fs-xs);
}

/* 评测卡 */
.eval-card {
  margin-top: 16px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  padding: 12px 16px 16px;
}
.eval-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}
.eval-title {
  font-weight: 600;
}
.saved-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: var(--fs-xs);
}
.saved-tag {
  cursor: pointer;
  margin: 0;
}
.rubric-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.rubric-select {
  width: 280px;
}
.rubric-form {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  margin-bottom: 12px;
  background: var(--surface);
}
.rubric-form-row {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 10px;
}
.rubric-hint {
  margin-bottom: 6px;
}
.criterion-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.criterion-name {
  flex: 0 0 200px;
}
.rubric-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.score-table {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.score-row {
  display: grid;
  grid-template-columns: 1fr 180px 180px;
  align-items: center;
  padding: 8px 12px;
  border-top: 1px solid var(--hairline);
}
.score-row:first-child {
  border-top: none;
  background: var(--surface);
}
.score-row-head {
  font-size: var(--fs-xs);
  font-weight: 600;
}
.rate-col {
  text-align: center;
}
.criterion-title {
  font-size: var(--fs-sm);
}
.criterion-desc {
  margin-top: 2px;
}
.verdict {
  margin-top: 12px;
}
.verdict-scores {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 8px;
  font-size: var(--fs-sm);
}
.verdict-note {
  margin-bottom: 8px;
}
.verdict-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

@media (max-width: 900px) {
  .cols {
    grid-template-columns: 1fr;
  }
  .run-meta-col {
    grid-template-columns: 1fr;
  }
  .score-row {
    grid-template-columns: 1fr 110px 110px;
  }
  .criterion-name {
    flex-basis: 120px;
  }
}
</style>
