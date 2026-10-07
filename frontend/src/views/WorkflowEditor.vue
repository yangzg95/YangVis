<template>
  <div class="page-card">
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

    <a-form layout="vertical">
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

      <div class="section-head">
        <span class="section-title">步骤</span>
        <span class="muted">{{ form.steps.length }} 步 · {{ groupCount }} 个并行组</span>
      </div>

      <!-- 左边列表是唯一的操作面，右边结构图只用来一眼看清并行关系与引用走向。 -->
      <div class="step-zone">
        <div ref="stepListEl" class="step-list" @focusin="onCardFocus">
          <div class="hint block">
            数组顺序就是执行顺序：相邻且分组名相同的步骤算一组，组内并行、组间顺序执行。
            分组名不用记：「加一步」自动开新组，「加一个并行步骤」自动并入上一组，要合并再在高级里改成同名。
          </div>
          <div
            v-for="(step, index) in form.steps"
            :key="index"
            class="step-item"
            :class="{ 'step-focus': focused === index }"
            :data-step-index="index"
          >
            <WorkflowStepForm
              :step="step"
              :index="index"
              :total="form.steps.length"
              :readonly="readonly"
              :chips="chipsAll[index] ?? []"
              :agent="agentOf(step.agent_slug)"
              :agent-options="agentOptions"
              :model-options="modelOptions"
              :problem-ids="problemIds"
              :show-problems="showProblems"
              :advanced-open="expanded.has(step)"
              @move="move(index, $event)"
              @remove="removeStep(index)"
              @toggle-advanced="toggleExpanded(index)"
            />
          </div>
          <a-space>
            <a-button size="small" :disabled="readonly" @click="addStep(false)">
              <PlusOutlined /> 加一步
            </a-button>
            <a-button size="small" :disabled="readonly" @click="addStep(true)">
              <PlusOutlined /> 加一个并行步骤
            </a-button>
          </a-space>
        </div>

        <!-- 窄屏放不下这一列，结构图只给桌面端；列表本来就是全集，缩掉不少功能。 -->
        <aside v-if="!isMobile" class="step-map">
          <WorkflowCanvas
            :steps="form.steps"
            :active="focused"
            :input-names="declaredInputs"
            :agents="agents"
            :problem-ids="problemIds"
            :show-problems="showProblems"
            @locate="locate"
          />
        </aside>
      </div>
    </a-form>

    <div class="footer">
      <span class="muted">
        {{
          readonly
            ? '内置流程由代码维护，需要改动请先在列表页「复制」成自己的版本。'
            : '保存即整份替换：步骤之间引用关系已闭合才能存下。'
        }}
      </span>
      <a-button @click="back">{{ readonly ? '关闭' : '取消' }}</a-button>
      <a-button v-if="!readonly" type="primary" :loading="saving" @click="submit">保存</a-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onActivated,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { MinusOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  agentsApi,
  settingsApi,
  workflowsApi,
  type Agent,
  type ModelConfig,
  type Workflow,
  type WorkflowPayload,
} from '@/api'
import { editorTarget, type EditorTarget } from '@/stores/workflowEditor'
import { useIsMobile } from '@/utils/useIsMobile'
import WorkflowCanvas from '@/components/workflow/WorkflowCanvas.vue'
import WorkflowStepForm from '@/components/workflow/WorkflowStepForm.vue'
import {
  chipGroupsFor,
  emptyStep,
  groupSteps,
  toStepForm,
  uniqueGroupName,
  type InputFieldForm,
  type StepForm,
} from '@/components/workflow/workflowLayout'

const router = useRouter()
const isMobile = useIsMobile()

const form = reactive({
  slug: '',
  name: '',
  description: '',
  enabled: true,
  inputs: [] as InputFieldForm[],
  steps: [emptyStep('g1')] as StepForm[],
})

const saving = ref(false)
const loadError = ref('')
const agents = ref<Agent[]>([])
const chatModels = ref<ModelConfig[]>([])
const workflows = ref<Workflow[]>([])
const presetId = ref<number | undefined>(undefined)

/** 当前在编辑谁：null = 新建。跳来时由列表页写进 store，这里只认最近一次请求。 */
const current = ref<Workflow | null>(null)
const readonly = ref(false)

/** 正在看第几步：右侧结构图高亮它。点节点、编辑卡片、滚动都会改这个下标。 */
const focused = ref<number | null>(0)

/** 结构图上点一下：滚到那张卡片，高亮跟着过去。 */
function locate(index: number) {
  focused.value = index
  void nextTick(() => {
    document
      .querySelector<HTMLElement>(`[data-step-index="${index}"]`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

/** 光标落进哪张卡片就高亮哪一步：不用等滚动，改到哪一眼就看得到。 */
function onCardFocus(event: FocusEvent) {
  const card = (event.target as HTMLElement | null)?.closest('[data-step-index]') as HTMLElement | null
  const index = Number(card?.dataset.stepIndex)
  if (Number.isInteger(index)) focused.value = index
}

/** 步骤卡片的「高级」折叠：按步骤对象记，重排之后不会串到别的步骤上。 */
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
    if (step.output_kind === 'json' || step.model_config_id != null || step.optional) {
      expanded.add(step)
    }
  }
}

/** 校验红框：点保存才亮灯，之后随填随灭（problemIds 是响应式算出来的）。 */
const showProblems = ref(false)

const editing = computed(() => !!current.value)
const title = computed(() => {
  if (readonly.value) return `查看工作流 · ${current.value?.name ?? ''}`
  return editing.value ? `编辑工作流 · ${current.value?.name ?? ''}` : '新建工作流'
})
const groupCount = computed(() => groupSteps(form.steps).length)
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
/** 内置流程都是调通的完整定义，给新建当起步模板。 */
const presetOptions = computed(() =>
  workflows.value
    .filter((workflow) => workflow.is_builtin)
    .map((workflow) => ({ value: workflow.id, label: workflow.name })),
)
/** 每一步能引用谁：声明的输入加上前面并行组的产物。结构图与列表用的是同一份。 */
const chipsAll = computed(() =>
  form.steps.map((_, index) => chipGroupsFor(form.inputs, form.steps, index)),
)

// ---- 反向同步：列表滚到哪一步，右边结构图就亮哪一个 --------------------------

const stepListEl = ref<HTMLElement | null>(null)
let cardWatcher: IntersectionObserver | null = null

/** 卡片真正滚起来的那层容器：本页不在 window 上滚，观察带得按它来算。 */
function scrollRoot(of: HTMLElement | null): HTMLElement | null {
  let node = of?.parentElement ?? null
  while (node) {
    if (/(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY)) return node
    node = node.parentElement
  }
  return null
}

/** 正在看的是哪一步：顶边已经越过滚动区中线、且离中线最近的那一张。 */
function syncFocusedFromScroll() {
  const list = stepListEl.value
  if (!list) return
  const scroller = scrollRoot(list)
  const box = scroller?.getBoundingClientRect()
  const line = (box?.top ?? 0) + (box?.height ?? window.innerHeight) / 2
  let pick: number | null = null
  let pickTop = -Infinity
  for (const el of Array.from(list.querySelectorAll<HTMLElement>('[data-step-index]'))) {
    const at = Number(el.dataset.stepIndex)
    const top = el.getBoundingClientRect().top
    if (!Number.isInteger(at) || top > line || top <= pickTop) continue
    pick = at
    pickTop = top
  }
  // 一张都没到中线的（刚进页面、卡片都还在下面）：高亮留在原处，别乱跳。
  if (pick !== null) focused.value = pick
}

function watchStepCards() {
  cardWatcher?.disconnect()
  const list = stepListEl.value
  if (!list || typeof IntersectionObserver === 'undefined') return
  cardWatcher = new IntersectionObserver(() => syncFocusedFromScroll(), {
    root: scrollRoot(list),
    // 只关心中线以上那半屏：卡片边跨过中线才算换了正在看的那一步。
    rootMargin: '0px 0px -50% 0px',
  })
  list.querySelectorAll('[data-step-index]').forEach((el) => cardWatcher?.observe(el))
}

// 增删步骤会换掉卡片那批元素：等 DOM 补齐后重新观察一遍。
watch(
  () => form.steps.length,
  () => void nextTick(watchStepCards),
)

/**
 * 装载：每次进入本页（含切标签回来）都重取三份数据——编辑期间新建的智能体应该
 * 马上能选到。表单只在 store 里出现了新请求（nonce 变了）时才整份重灌，
 * 切标签来回不动用户的草稿。
 */
let appliedNonce = 0

async function boot() {
  try {
    const [agentData, modelData, wfData] = await Promise.all([
      agentsApi.list(),
      settingsApi.listModels('chat'),
      workflowsApi.list(),
    ])
    agents.value = agentData.items
    chatModels.value = modelData.items
    workflows.value = wfData.items
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
  current.value = source
  form.slug = source?.slug ?? ''
  form.name = source?.name ?? ''
  form.description = source?.description ?? ''
  form.enabled = source?.enabled ?? true
  form.inputs = (source?.inputs ?? []).map((field) => ({ ...field }))
  applySteps(source ? source.steps.map(toStepForm) : [emptyStep('g1')])
}

/** 整份换掉步骤：折叠状态跟着新数组重来，高亮回到第一步。 */
function applySteps(steps: StepForm[]) {
  form.steps.splice(0, form.steps.length, ...steps)
  autoExpandAdvanced(form.steps)
  focused.value = steps.length ? 0 : null
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

// keep-alive 下首次挂载会连着触发 mounted 与 activated，第二次的 boot 跳过。
let justMounted = false
onMounted(() => {
  justMounted = true
  void nextTick(watchStepCards)
  void boot()
})
onBeforeUnmount(() => {
  cardWatcher?.disconnect()
  cardWatcher = null
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

function addInput() {
  form.inputs.push({ name: '', label: '', required: true })
}

/** 加一步 = 新开一组；加并行 = 并进最后一组。组名一律取没占用过的那个。 */
function addStep(parallel: boolean) {
  const last = form.steps[form.steps.length - 1]
  const step = emptyStep(parallel ? last?.group_key ?? '' : uniqueGroupName(form.steps))
  form.steps.push(step)
  // 新卡片在列表最底下，多半在屏幕外：跟结构图说一声，滚过去接着填。
  void nextTick(() => locate(form.steps.length - 1))
}

function removeStep(index: number) {
  const [removed] = form.steps.splice(index, 1)
  if (removed) expanded.delete(removed)
}

function move(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= form.steps.length) return
  const [step] = form.steps.splice(index, 1)
  form.steps.splice(target, 0, step)
  if (focused.value === index) focused.value = target
}

/** 从内置流程起步：整份搬来输入与步骤，名字带「副本」，标识留空必须自己起。 */
function onPresetChange(value: number | undefined) {
  const source = workflows.value.find((workflow) => workflow.id === value && workflow.is_builtin)
  if (!source) {
    // 清空起步选择 = 回到空白新建：选择器与表单内容必须同进同退，
    // 不然显示「空白工作流」、底下还留着刚才那份，比从零配更容易出错。
    form.slug = ''
    form.name = ''
    form.description = ''
    form.inputs = []
    applySteps([emptyStep('g1')])
    return
  }
  form.slug = ''
  form.name = `${source.name} 副本`
  form.description = source.description ?? ''
  form.inputs = source.inputs.map((field) => ({ ...field }))
  applySteps(source.steps.map(toStepForm))
}

/**
 * 必填缺口的单一事实源：界面标红、保存拦截、滚动定位都从这里算。
 * id 形状是 ``step3.key``，卡片按前缀认领自己那几步。
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

async function submit() {
  if (readonly.value) return
  if (problemIds.value.size) {
    showProblems.value = true
    message.warning('先把标红的必填项补完')
    await nextTick()
    document
      .querySelector('.field-problem')
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
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
      optional: step.optional,
    })),
  }
  if (current.value) payload.enabled = form.enabled

  saving.value = true
  try {
    if (current.value) await workflowsApi.update(current.value.id, payload)
    else await workflowsApi.create(payload)
  } catch {
    return // 拦截器已把后端的校验原因弹出来了。
  } finally {
    saving.value = false
  }
  message.success('已保存')
  back() // 列表页在 onActivated 里重取，回到那一标签就是新数据。
}
</script>

<style scoped>
/* 页内布局沿用原抽屉：分组、卡片、芯片那套观感保持一致。 */
.row {
  display: flex;
  gap: 12px;
}
.col {
  flex: 1;
  min-width: 0;
}
/* 起步方式：选内置流程的入口，压在表单最顶上最先看到的位置。 */
.preset-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 8px 12px;
  border: 1px dashed var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  font-size: 13px;
}
.preset-label {
  font-weight: 600;
}
.w-preset {
  width: 220px;
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
/* 左列表右结构图：列表吃掉剩余宽度，图那一栏贴着顶部跟着滚。 */
.step-zone {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.step-list {
  flex: 1;
  min-width: 0;
}
.step-map {
  width: 312px;
  flex: none;
  position: sticky;
  top: 12px;
}
.step-item {
  margin-bottom: 12px;
  /* 结构图点过来时按卡片顶边对齐视口上方：留出这点边距，标题不被上一条卡压住。 */
  scroll-margin-top: 12px;
}
/* 结构图当前亮着的那一步：描一圈光环，与卡片自己的红框（校验缺口）互不遮挡。 */
.step-focus {
  outline: 1px solid var(--signal);
  outline-offset: 2px;
  border-radius: var(--radius-md);
}
/* 保存拦下时标出缺口所在：整卡描红，随填随灭；页面也靠它找第一个缺口滚过去。 */
.field-problem {
  border-color: var(--danger);
}
/* 加载失败警告条：与下方内容拉开一点距离。 */
.load-error {
  margin-bottom: 16px;
}
.footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
}
.footer .muted {
  margin-right: auto;
}
</style>
