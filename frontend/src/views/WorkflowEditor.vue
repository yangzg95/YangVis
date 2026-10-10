<template>
  <div class="page-card wizard-card">
    <div class="page-title">{{ title }}</div>

    <!-- 打开页面要拉智能体/模型/工作流三份数据，任何一份失败下拉与回填都是空的：
         与列表页同一口径，警告条＋重试，不静默。 -->
    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      class="load-error"
      message="配置页数据加载失败"
      :description="loadError"
    >
      <template #action>
        <a-button size="small" danger @click="boot">重试</a-button>
      </template>
    </a-alert>

    <!-- 向导步骤条：可点击跳转（离开当前页要先过当前页校验）。节点多时横向可滚。 -->
    <a-steps
      class="wizard-steps"
      :current="wizardPage"
      :items="stepItems"
      size="small"
      @change="onStepChange"
    />

    <a-form layout="vertical" class="wizard-form">
      <!-- 第 1 页 · 基本信息 -------------------------------------------------- -->
      <section v-show="wizardPage === 0" class="wizard-pane">
        <!-- 起步方式：内置流程都是调通过的完整骨架，复制来改远比从零敲字段省心。
             只在新建时出现；标识必须自己起，因为功能在代码里按标识调用这条流程。 -->
        <div v-if="!editing" class="preset-bar">
          <span class="preset-label">起步于</span>
          <a-select
            v-model:value="presetId"
            :options="presetOptions"
            placeholder="空白工作流"
            size="small"
            allow-clear
            class="w-preset"
            @change="onPresetChange"
          />
          <span class="muted">选一条内置流程带入现成的步骤与引用，改名字和标识后保存。</span>
        </div>

        <div class="row">
          <a-form-item label="名称" required class="col" :class="{ 'field-problem': hasProblem('name') }">
            <a-input
              v-model:value="form.name"
              :maxlength="128"
              :disabled="readonly"
              :status="hasProblem('name') ? 'error' : undefined"
            />
          </a-form-item>
          <a-form-item label="标识" required class="col" :class="{ 'field-problem': hasProblem('slug') }">
            <a-input
              v-model:value="form.slug"
              :disabled="editing || readonly"
              placeholder="小写字母、数字与短横线"
              :status="hasProblem('slug') ? 'error' : undefined"
            />
            <div class="hint">创建后不可修改：功能在代码里按标识调用这条流程。</div>
          </a-form-item>
        </div>

        <a-form-item label="说明">
          <a-input
            v-model:value="form.description"
            :maxlength="512"
            placeholder="这条流程解决什么问题"
            :disabled="readonly"
          />
        </a-form-item>

        <a-form-item v-if="editing" label="启用">
          <a-switch v-model:checked="form.enabled" :disabled="readonly" />
          <div class="hint">
            停用后这条流程不能发起运行；界面选择器里也不再出现。内置流程由功能自己调用，
            停用与否不影响那个功能。
          </div>
        </a-form-item>

        <div class="section-head">
          <span class="section-title">输入变量</span>
          <span class="muted">{{ form.inputs.length }} 个</span>
        </div>
        <div class="hint block" v-pre>
          步骤模板里能用 {{变量名}} 引用的，就是这里声明的变量加上前面步骤的产物。输出格式是 JSON 的步骤还能按字段取：{{产物名.字段}}，嵌套对象接着往下点、数组用下标（{{大纲.pages.1.title}}）；整段引用它拿到的是解析出来的对象。条件段落（「有补充说明才加这一段」）请在调用方拼好再作为一个变量传进来——模板只做替换，不含判断。
        </div>
        <div
          v-for="(field, index) in form.inputs"
          :key="index"
          class="field-row"
          :class="{ 'field-problem': hasProblem(`input${index}.name`) }"
        >
          <a-input
            v-model:value="field.name"
            placeholder="变量名 topic"
            class="w-name"
            :disabled="readonly"
            :status="hasProblem(`input${index}.name`) ? 'error' : undefined"
          />
          <a-input
            v-model:value="field.label"
            placeholder="显示名（留空用变量名）"
            class="w-label"
            :disabled="readonly"
          />
          <a-checkbox v-model:checked="field.required" :disabled="readonly">必填</a-checkbox>
          <a-button
            size="small"
            type="text"
            danger
            :disabled="readonly"
            @click="form.inputs.splice(index, 1)"
          >
            <MinusOutlined />
          </a-button>
        </div>
        <a-button size="small" :disabled="readonly" @click="addInput">
          <PlusOutlined /> 加一个变量
        </a-button>

        <div class="section-head mcp-head">
          <span class="section-title">MCP 服务挂载</span>
          <span class="muted">{{ form.mcp_server_ids.length }} 个</span>
        </div>
        <a-select
          v-model:value="form.mcp_server_ids"
          mode="multiple"
          class="w-mcp"
          :options="mcpOptions"
          :disabled="readonly"
          placeholder="选择要挂载的 MCP 服务"
          allow-clear
        />
        <div class="hint block">
          挂载后，这条工作流每个步骤的模型在运行时都可以调用这些服务提供的工具；
          停用的服务不会出现在列表里。
        </div>
      </section>

      <!-- 第 2..G+1 页 · 一个并行组一页：组内多个节点同页配置，v-show 保留 DOM ---- -->
      <section
        v-for="(g, gi) in wizardGroups"
        :key="gi"
        v-show="wizardPage === gi + 1"
        class="wizard-pane"
      >
        <div
          v-for="index in g.indexes"
          :key="index"
          class="step-item"
          :data-step-index="index"
        >
          <WorkflowStepForm
            :step="form.steps[index]"
            :index="index"
            :total="form.steps.length"
            :readonly="readonly"
            :chips="chipsAll[index] ?? []"
            :upstream-groups="upstreamAll[index] ?? []"
            :group-label-text="groupLabelByStep[index]"
            :parallel="groupParallelByStep[index]"
            :agent="agentOf(form.steps[index].agent_slug)"
            :agent-options="agentOptions"
            :model-options="modelOptions"
            :mcp-options="mcpOptions"
            :problem-ids="problemIds"
            :show-problems="showProblems"
            :advanced-open="expanded.has(form.steps[index])"
            @move="onMove(index, $event)"
            @remove="removeStep(index)"
            @toggle-advanced="toggleExpanded(index)"
          />
        </div>
      </section>
    </a-form>

    <!-- 结构图独立成预览：任意一步都能点开，弹窗里看全并行关系与引用走向。
         destroy-on-close：每次打开画布重新挂载；after-open 再补一次测量。 -->
    <a-modal
      v-model:open="previewOpen"
      title="结构图预览"
      width="1280px"
      centered
      :footer="null"
      destroy-on-close
      :wrap-class-name="previewFullscreen ? 'wf-preview-modal wf-preview-fullscreen' : 'wf-preview-modal'"
      @after-open="onPreviewAfterOpen"
    >
      <WorkflowCanvas
        ref="previewCanvasEl"
        :steps="form.steps"
        :active="null"
        :input-names="declaredInputs"
        :agents="agents"
        :model-options="modelOptions"
        :problem-ids="problemIds"
        :show-problems="showProblems"
        :collapsible="false"
        :show-fullscreen="true"
        :fullscreen="previewFullscreen"
        @locate="onPreviewLocate"
        @toggle-fullscreen="toggleFullscreen"
      />
    </a-modal>

    <!-- 底部导航：按当前页条件渲染 -->
    <div class="wizard-footer">
      <span class="muted">{{ footerHint }}</span>

      <a-button @click="back">{{ readonly ? '关闭' : '取消' }}</a-button>
      <!-- 预览任意一步都能点：弹窗看结构图，不占向导步骤。 -->
      <a-button @click="openPreview">
        <EyeOutlined /> 预览
      </a-button>

      <template v-if="wizardPage > 0">
        <a-button @click="prev">上一步</a-button>
      </template>

      <!-- 基本信息页：下一步 -->
      <a-button v-if="wizardPage === 0" type="primary" @click="next">下一步</a-button>

      <!-- 组页：加并行（留本页）、下一步（最后组=新增组）、完成 -->
      <template v-if="groupPage">
        <a-button v-if="!readonly" @click="addParallel">
          <PlusOutlined /> 并行步骤
        </a-button>
        <!-- 只读且已是最后一组：没有下一页可去，禁用。 -->
        <a-button type="primary" :disabled="readonly && isLastGroup" @click="next">
          {{ isLastGroup && !readonly ? '下一步 · 新增组' : '下一步' }}
        </a-button>
        <a-button v-if="!readonly" type="primary" ghost :loading="saving" @click="submit">
          完成
        </a-button>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  onActivated,
  onMounted,
  reactive,
  ref,
} from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { EyeOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  agentsApi,
  mcpApi,
  settingsApi,
  workflowsApi,
  type Agent,
  type McpServer,
  type ModelConfig,
  type Workflow,
  type WorkflowPayload,
} from '@/api'
import { editorTarget, type EditorTarget } from '@/stores/workflowEditor'
import WorkflowCanvas from '@/components/workflow/WorkflowCanvas.vue'
import WorkflowStepForm from '@/components/workflow/WorkflowStepForm.vue'
import {
  chipGroupsFor,
  emptyStep,
  groupSequence,
  groupSteps,
  toStepForm,
  uniqueGroupName,
  upstreamGroupsFor,
  groupLabel,
  type InputFieldForm,
  type StepForm,
} from '@/components/workflow/workflowLayout'

const router = useRouter()

const form = reactive({
  slug: '',
  name: '',
  description: '',
  enabled: true,
  inputs: [] as InputFieldForm[],
  mcp_server_ids: [] as number[],
  steps: [emptyStep('g1')] as StepForm[],
})

const saving = ref(false)
const loadError = ref('')
const agents = ref<Agent[]>([])
const chatModels = ref<ModelConfig[]>([])
const workflows = ref<Workflow[]>([])
const mcpServers = ref<McpServer[]>([])
const presetId = ref<number | undefined>(undefined)

/** 当前在编辑谁：null = 新建。跳来时由列表页写进 store，这里只认最近一次请求。 */
const workflow = ref<Workflow | null>(null)
const readonly = ref(false)

// ---- 向导页码：0 基本信息；1..G 并行组（组内节点同页）；结构图走预览弹窗 ----------

const wizardPage = ref(0)
/** 向导按组分页：基本信息 + G 个并行组。 */
const wizardGroups = computed(() => groupSteps(form.steps))
const totalPages = computed(() => wizardGroups.value.length + 1)
/** 当前是不是组页（非基本信息）。 */
const groupPage = computed(() => wizardPage.value >= 1)
const currentGroup = computed(() => wizardGroups.value[wizardPage.value - 1])
const isLastGroup = computed(
  () => wizardPage.value === totalPages.value - 1,
)

/** 步骤条项：每一步是一个并行组，标题用组内步骤标识；多节点组标注几步并行。 */
const stepItems = computed(() => [
  { title: '基本信息' },
  ...wizardGroups.value.map((g) => ({
    title: groupLabel(form.steps, g),
    description: g.indexes.length > 1 ? `${g.indexes.length} 步并行` : undefined,
  })),
])

// ---- 步骤卡片的「高级」折叠 ----------------------------------------------------

/** 按步骤对象记，重排之后不会串到别的步骤上。 */
const expanded = reactive(new Set<StepForm>())
function toggleExpanded(index: number) {
  const step = form.steps[index]
  if (!step) return
  if (expanded.has(step)) expanded.delete(step)
  else expanded.add(step)
}

/** 换入一批步骤时，把配过高级项的直接摊开，免得藏住 JSON 契约与模型选择。 */
function autoExpandAdvanced(steps: StepForm[]) {
  expanded.clear()
  for (const step of steps) {
    if (
      step.output_kind === 'json' ||
      step.model_config_id != null ||
      step.mcp_server_ids.length ||
      step.optional
    ) {
      expanded.add(step)
    }
  }
}

// ---- 校验与翻页 ----------------------------------------------------------------

/** 校验红框：翻页/完成触发后亮灯，之后随填随灭（problemIds 是响应式算出来的）。 */
const showProblems = ref(false)

const editing = computed(() => !!workflow.value)
const title = computed(() => {
  if (readonly.value) return `查看工作流 · ${workflow.value?.name ?? ''}`
  return editing.value ? `编辑工作流 · ${workflow.value?.name ?? ''}` : '新建工作流'
})
const declaredInputs = computed(() =>
  form.inputs.map((field) => field.name.trim()).filter((name) => name),
)
const agentOptions = computed(() =>
  agents.value.map((agent) => ({ value: agent.slug, label: agent.name, agent })),
)
function agentOf(slug: string) {
  return agents.value.find((agent) => agent.slug === slug) ?? null
}
const modelOptions = computed(() =>
  chatModels.value
    .filter((config) => config.last_test_ok)
    .map((config) => ({ value: config.id, label: config.title })),
)
/** MCP 挂载下拉：只列启用中的本人服务。 */
const mcpOptions = computed(() =>
  mcpServers.value
    .filter((server) => server.enabled)
    .map((server) => ({ value: server.id, label: server.name })),
)
/** 内置流程都是调通的完整定义，给新建当起步模板。 */
const presetOptions = computed(() =>
  workflows.value
    .filter((workflow) => workflow.is_builtin)
    .map((workflow) => ({ value: workflow.id, label: workflow.name })),
)
/** 每一步能引用谁：声明的输入加上前面并行组的产物。节点页与画布用的是同一份。 */
const chipsAll = computed(() =>
  form.steps.map((_, index) => chipGroupsFor(form.inputs, form.steps, index)),
)
/** 每一步的「上一步产出」：紧邻前一个并行组的产物，节点卡片常驻展示。 */
const upstreamAll = computed(() =>
  form.steps.map((_, index) => upstreamGroupsFor(form.steps, index)),
)
/** 每一步所属并行组的展示名（步骤标识拼成）与是否多步并行，给卡片头部用。 */
const groupLabelByStep = computed(() => {
  const groups = wizardGroups.value
  return groupSequence(form.steps).map(
    (seq) => groupLabel(form.steps, groups[seq] ?? { indexes: [] }),
  )
})
const groupParallelByStep = computed(() => {
  const groups = wizardGroups.value
  return groupSequence(form.steps).map(
    (seq) => (groups[seq]?.indexes.length ?? 0) > 1,
  )
})

/**
 * 必填缺口的单一事实源：界面标红、翻页拦截、保存拦截都从这里算。
 * id 形状是 ``step3.key``，节点页按前缀认领自己那一步。
 */
const problemIds = computed(() => {
  const ids = new Set<string>()
  if (!form.name.trim()) ids.add('name')
  if (!editing.value && !/^[a-z0-9][a-z0-9-]*$/.test(form.slug.trim())) ids.add('slug')
  form.inputs.forEach((field, index) => {
    if (!field.name.trim()) ids.add(`input${index}.name`)
  })
  form.steps.forEach((step, index) => {
    if (!step.key.trim()) ids.add(`step${index}.key`)
    if (!step.group_key.trim()) ids.add(`step${index}.group`)
    if (!step.agent_slug) ids.add(`step${index}.agent`)
    if (!step.prompt_tpl.trim()) ids.add(`step${index}.prompt`)
  })
  return ids
})

function hasProblem(id: string) {
  return showProblems.value && problemIds.value.has(id)
}

/** 某一页相关的缺口：基本信息页看 name/slug/input；组页看组内每个节点的前缀。 */
function pageProblems(page: number): string[] {
  const all = Array.from(problemIds.value)
  if (page === 0) {
    return all.filter((id) => id === 'name' || id === 'slug' || id.startsWith('input'))
  }
  const g = wizardGroups.value[page - 1]
  if (!g) return []
  return all.filter((id) => g.indexes.some((i) => id.startsWith(`step${i}.`)))
}

/** 离开当前页：有缺口就亮灯拦住，返回 false。 */
function canLeave(page: number): boolean {
  if (!pageProblems(page).length) return true
  showProblems.value = true
  message.warning('先把当前这一步的必填项补完')
  return false
}

function goTo(page: number) {
  wizardPage.value = Math.min(Math.max(page, 0), totalPages.value - 1)
}

/** 上一步不拦截：允许退回去改前面。 */
function prev() {
  goTo(wizardPage.value - 1)
}

/**
 * 下一步：过当前组校验后进下一组；当前已是最后一组时自动新增一个空组
 * （只读模式不新增，且最后组的下一步禁用）。
 */
function next() {
  if (!canLeave(wizardPage.value)) return
  if (isLastGroup.value && !readonly.value) {
    form.steps.push(emptyStep(uniqueGroupName(form.steps)))
  }
  goTo(wizardPage.value + 1)
}

/** 「＋并行步骤」：本组末尾加一个同组节点，留在当前页一起配，不翻页。 */
function addParallel() {
  const g = currentGroup.value
  if (!g) return
  const at = g.indexes[g.indexes.length - 1] + 1
  form.steps.splice(at, 0, emptyStep(g.name))
}

/** 步骤条点击跳转：离开当前页要过校验。 */
function onStepChange(page: number) {
  if (page === wizardPage.value) return
  if (canLeave(wizardPage.value)) goTo(page)
}

// ---- 结构图预览弹窗 ------------------------------------------------------------

const previewOpen = ref(false)
const previewFullscreen = ref(false)
const previewCanvasEl = ref<InstanceType<typeof WorkflowCanvas> | null>(null)

function openPreview() {
  previewFullscreen.value = false
  previewOpen.value = true
}

/** 全屏 / 退出全屏：容器尺寸变了，动画后让画布补一次测量。 */
function toggleFullscreen() {
  previewFullscreen.value = !previewFullscreen.value
  window.setTimeout(() => previewCanvasEl.value?.refresh(), 300)
}

/** 弹窗入场动画结束后容器尺寸才落定：让画布补一次测量与 fit。 */
function onPreviewAfterOpen() {
  previewCanvasEl.value?.refresh()
}

/** 预览里点节点：关掉弹窗，跳到该节点所属组的配置页。 */
function onPreviewLocate(stepIndex: number) {
  previewOpen.value = false
  const groupSeq = groupSequence(form.steps)[stepIndex] ?? 0
  goTo(groupSeq + 1)
}

// ---- 节点增删移动 --------------------------------------------------------------

function addInput() {
  form.inputs.push({ name: '', label: '', required: true })
}

function removeStep(index: number) {
  const [removed] = form.steps.splice(index, 1)
  if (removed) expanded.delete(removed)
  // 当前组的最后一个节点被删、整组消失：页码退回最后一个仍存在的组页；
  // 删的是中间组则后面的组前移顶替，页码不动。
  if (wizardPage.value > wizardGroups.value.length) {
    wizardPage.value = wizardGroups.value.length
  }
}

/**
 * 头部上移/下移：重排（跨组时被移动步并入相邻组）后，跟随它到新所属组页；
 * 同组内移动组页不变。不然当前页显示的换成别的节点，容易看串。
 */
function onMove(index: number, delta: number) {
  const newIndex = moveStep(index, delta)
  const groupSeq = groupSequence(form.steps)[newIndex] ?? 0
  goTo(groupSeq + 1)
}

/** 返回移动后该节点的数组下标；越界（按钮已禁用）时原样返回。 */
function moveStep(index: number, delta: number): number {
  const target = index + delta
  if (target < 0 || target >= form.steps.length) return index
  const [step] = form.steps.splice(index, 1)
  form.steps.splice(target, 0, step)
  // group_key 改成新位置前邻的组名（移到首位取后邻）：与 groupSteps「相邻同名
  // 即同组」一致，跨组移动自动并入；同组移动邻居同名，赋值是 no-op。
  const prev = form.steps[target - 1]
  const next = form.steps[target + 1]
  step.group_key = prev?.group_key?.trim() || next?.group_key?.trim() || step.group_key
  return target
}

// ---- 数据装载 ------------------------------------------------------------------

/**
 * 每次进入本页（含切标签回来）都重取三份数据——编辑期间新建的智能体应该
 * 马上能选到。表单只在 store 里出现了新请求（nonce 变了）时才整份重灌，
 * 切标签来回不动用户的草稿。
 */
let appliedNonce = 0

async function boot() {
  try {
    const [agentData, modelData, wfData, mcpData] = await Promise.all([
      agentsApi.list(),
      settingsApi.listModels('chat'),
      workflowsApi.list(),
      mcpApi.list(),
    ])
    agents.value = agentData.items
    chatModels.value = modelData.items
    workflows.value = wfData.items
    mcpServers.value = mcpData.items
    loadError.value = ''
  } catch (err) {
    loadError.value = errorText(err, '请检查网络后重试')
    return
  }
  const target = editorTarget.value
  if (!target || target.nonce === appliedNonce) return
  appliedNonce = target.nonce
  fillForm(target)
}

function fillForm(target: EditorTarget) {
  showProblems.value = false
  presetId.value = undefined
  readonly.value = target.readonly
  const source = target.id == null ? null : workflows.value.find((w) => w.id === target.id) ?? null
  workflow.value = source
  form.slug = source?.slug ?? ''
  form.name = source?.name ?? ''
  form.description = source?.description ?? ''
  form.enabled = source?.enabled ?? true
  form.inputs = (source?.inputs ?? []).map((field) => ({ ...field }))
  form.mcp_server_ids = [...(source?.mcp_server_ids ?? [])]
  applySteps(source ? source.steps.map(toStepForm) : [emptyStep('g1')])
}

/** 整份换掉步骤：折叠状态跟着新数组重来，向导回到基本信息页。 */
function applySteps(steps: StepForm[]) {
  form.steps.splice(0, form.steps.length, ...steps)
  autoExpandAdvanced(form.steps)
  wizardPage.value = 0
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

// keep-alive 下首次挂载会连着触发 mounted 与 activated，第二次的 boot 跳过。
let justMounted = false
onMounted(() => {
  justMounted = true
  void boot()
})
onActivated(() => {
  if (justMounted) {
    justMounted = false
    return
  }
  void boot()
})

function back() {
  router.push('/workflows')
}

/** 从内置起步：整份搬来输入与步骤，名字带「副本」，标识留空必须自己起。 */
function onPresetChange(value: number | undefined) {
  const source = workflows.value.find((workflow) => workflow.id === value && workflow.is_builtin)
  if (!source) {
    // 清空起步选择 = 回到空白新建：选择器与表单内容必须同进同退，
    // 不然显示「空白工作流」、底下还留着刚才那份，比从零配更容易出错。
    form.slug = ''
    form.name = ''
    form.description = ''
    form.inputs = []
    form.mcp_server_ids = []
    applySteps([emptyStep('g1')])
    return
  }
  form.slug = ''
  form.name = `${source.name} 副本`
  form.description = source.description ?? ''
  form.inputs = source.inputs.map((field) => ({ ...field }))
  // 内置流程不挂 MCP，预设不带入挂载。
  form.mcp_server_ids = []
  applySteps(source.steps.map(toStepForm))
}

/** 完成保存：有缺口按向导顺序跳到第一个问题页（基本信息优先，再按节点顺序）。 */
function jumpToFirstProblem() {
  const ids = Array.from(problemIds.value)
  const base = ids.find((id) => id === 'name' || id === 'slug' || id.startsWith('input'))
  if (base) {
    goTo(0)
    return
  }
  let first = Infinity
  for (const id of ids) {
    const match = /^step(\d+)\./.exec(id)
    if (match) first = Math.min(first, Number(match[1]))
  }
  // 跳到缺口节点所属的组页，而不是按节点序号当页码。
  if (Number.isFinite(first)) {
    const groupSeq = groupSequence(form.steps)[first] ?? 0
    goTo(groupSeq + 1)
  }
}

async function submit() {
  if (readonly.value) return
  if (problemIds.value.size) {
    showProblems.value = true
    message.warning('先把标红的必填项补完')
    jumpToFirstProblem()
    return
  }

  const payload: WorkflowPayload = {
    slug: form.slug.trim(),
    name: form.name.trim(),
    description: form.description.trim() || null,
    inputs: form.inputs.map((field) => ({
      name: field.name.trim(),
      label: field.label.trim(),
      required: field.required,
    })),
    mcp_server_ids: [...form.mcp_server_ids],
    // 不提交 sort_order：后端按数组顺序编号，界面与执行顺序就不会对不上。
    steps: form.steps.map((step) => ({
      key: step.key.trim(),
      group_key: step.group_key.trim(),
      agent_slug: step.agent_slug,
      prompt_tpl: step.prompt_tpl,
      output_kind: step.output_kind,
      output_keys: step.output_kind === 'json' && step.output_keys.length ? step.output_keys : null,
      output_var: step.output_var.trim() || null,
      model_config_id: step.model_config_id,
      mcp_server_ids: [...step.mcp_server_ids],
      optional: step.optional,
    })),
  }
  if (workflow.value) payload.enabled = form.enabled

  saving.value = true
  try {
    if (workflow.value) await workflowsApi.update(workflow.value.id, payload)
    else await workflowsApi.create(payload)
  } catch {
    return // 拦截器已把后端的校验原因弹出来了。
  } finally {
    saving.value = false
  }
  message.success('已保存')
  back() // 列表页在 onActivated 里重取，回到那一标签就是新数据。
}

const footerHint = computed(() =>
  readonly.value
    ? '内置流程由代码维护，需要改动请先在列表页「复制」成自己的版本。'
    : '完成前每一步的必填项与步骤间引用关系都要闭合。',
)
</script>

<style scoped>
/* 卡片撑满滚动区可视高度并纵向 flex：内容再少，底部导航也压在页面底边而不悬空。 */
.wizard-card {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}
/* 表单区吃掉中间剩余高度：把底部条顶到卡片底边。 */
.wizard-form {
  flex: 1;
  min-height: 0;
}
/* 向导步骤条：节点多时不挤压，横向滚动看全。 */
.wizard-steps {
  margin-bottom: 20px;
  overflow-x: auto;
  padding-bottom: 4px;
}
.wizard-pane {
  /* 翻页时底部按钮位置不跳动。 */
  min-height: 240px;
}
/* 起步方式：选内置流程的入口，压在基本信息页最顶上先看到。 */
.preset-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 8px 12px;
  border: 1px dashed var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  font-size: var(--fs-sm);
}
.preset-label {
  font-weight: 600;
}
.w-preset {
  width: 220px;
}
.row {
  display: flex;
  gap: 12px;
}
.col {
  flex: 1;
  min-width: 0;
}
.section-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 4px 0 8px;
}
.section-title {
  font-weight: 600;
}
.hint.block {
  margin-bottom: 8px;
}
.mcp-head {
  margin-top: 20px;
}
.w-mcp {
  width: 100%;
}
.field-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.w-name {
  width: 180px;
}
.w-label {
  flex: 1;
  min-width: 0;
}
/* 保存/翻页拦下时标出缺口所在：随填随灭。 */
.field-problem {
  border-color: var(--danger);
}
/* 加载失败警告条：与下方内容拉开一点距离。 */
.load-error {
  margin-bottom: 16px;
}
.wizard-footer {
  position: sticky;
  bottom: 0;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
  /* 内容从下面滚过时被底色盖住：与卡片同为 surface，衔接处看不出接缝。 */
  background: var(--surface);
}
.wizard-footer .muted {
  margin-right: auto;
}
</style>

<!-- 非 scoped：modal 被 teleport 到 body，scoped 选择器够不到。 -->
<style>
/* 固定尺寸预览：1280×800 居中；小屏时留 16px 安全边，不溢出视口。
   画布区域在固定盒子里 flex 撑满，节点再多也只在内部滚动。 */
.wf-preview-modal .ant-modal {
  width: 1280px !important;
  max-width: calc(100vw - 32px);
  padding-bottom: 0;
}
.wf-preview-modal .ant-modal-content {
  height: 800px;
  max-height: calc(100vh - 32px);
  display: flex;
  flex-direction: column;
  padding: 12px 20px;
}
.wf-preview-modal .ant-modal-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.wf-preview-modal .wf-canvas {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.wf-preview-modal .canvas-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.wf-preview-modal .canvas-scroll {
  flex: 1;
  max-height: none;
}

/* 真正全屏：固定盒子被覆盖为占满整个视口。 */
.wf-preview-fullscreen {
  padding: 0;
  overflow: hidden;
}
.wf-preview-fullscreen.ant-modal-wrap {
  overflow: hidden;
}
.wf-preview-fullscreen .ant-modal {
  top: 0;
  width: 100vw !important;
  max-width: 100vw;
  margin: 0;
}
.wf-preview-fullscreen .ant-modal-content {
  height: 100vh;
  max-height: 100vh;
  border-radius: 0;
}
</style>
