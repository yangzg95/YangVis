<template>
  <a-drawer
    :open="open"
    :title="title"
    placement="right"
    :width="960"
    :mask-closable="false"
    @update:open="(value: boolean) => emit('update:open', value)"
  >
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
      <div class="hint block">
        数组顺序就是执行顺序：相邻且分组名相同的步骤算一组，组内并行、组间顺序执行。
        分组名不用记：「加一步」自动开新组，「加一个并行步骤」自动并入上一组，要合并再在高级里改成同名。
      </div>

      <div
        v-for="(step, index) in form.steps"
        :key="index"
        class="step-card"
        :class="{ 'field-problem': stepProblem(index) }"
      >
        <div class="step-head">
          <span class="step-no">{{ index + 1 }}</span>
          <a-input
            v-model:value="step.key"
            placeholder="步骤标识"
            size="small"
            class="w-key"
            :disabled="readonly"
            :status="hasProblem(`step${index}.key`) ? 'error' : undefined"
          />
          <a-select
            v-model:value="step.agent_slug"
            :options="agentOptions"
            placeholder="选择人设"
            size="small"
            show-search
            option-filter-prop="label"
            class="w-agent"
            :disabled="readonly"
            :status="hasProblem(`step${index}.agent`) ? 'error' : undefined"
          >
            <!-- 下拉里带一句人设描述：选之前就知道这是位干什么的，不用凭名字猜。 -->
            <template #option="opt">
              <div class="agent-opt">
                <span>{{ opt.label }}</span>
                <span class="agent-opt-desc">{{ opt.agent?.description || '无描述' }}</span>
              </div>
            </template>
          </a-select>
          <a-tag class="group-tag">{{ step.group_key.trim() || '未分组' }}</a-tag>
          <span class="spacer" />
          <a-button
            v-if="!readonly"
            size="small"
            type="text"
            @click="advancedOpen[index] = !advancedOpen[index]"
          >
            {{ advancedOpen[index] ? '收起' : '高级' }}
          </a-button>
          <a-button
            size="small"
            type="text"
            :disabled="index === 0 || readonly"
            @click="move(index, -1)"
          >
            <ArrowUpOutlined />
          </a-button>
          <a-button
            size="small"
            type="text"
            :disabled="index === form.steps.length - 1 || readonly"
            @click="move(index, 1)"
          >
            <ArrowDownOutlined />
          </a-button>
          <a-button
            size="small"
            type="text"
            danger
            :disabled="form.steps.length <= 1 || readonly"
            @click="form.steps.splice(index, 1)"
          >
            <DeleteOutlined />
          </a-button>
        </div>

        <!-- 选中后把这个人设是谁、管什么摆在手边；点开能看全系统提示词，
             配下游引用时不用切去智能体页对照。 -->
        <div v-if="agentOf(step.agent_slug)" class="agent-hint">
          <span class="muted">{{ agentOf(step.agent_slug)?.description || '这个人设没有描述' }}</span>
          <a-popover placement="left" trigger="click" title="人设（系统提示词）">
            <template #content>
              <pre
                style="
                  max-width: 420px;
                  max-height: 320px;
                  overflow: auto;
                  white-space: pre-wrap;
                  word-break: break-word;
                  font-size: 12px;
                  margin: 0;
                "
              >{{ agentOf(step.agent_slug)?.system_prompt }}</pre>
            </template>
            <a-button size="small" type="link">查看人设</a-button>
          </a-popover>
        </div>

        <a-form-item label="Prompt 模板" class="tight">
          <div class="prompt-wrap">
            <a-textarea
              v-model:value="step.prompt_tpl"
              :rows="4"
              placeholder="就{{topic}}写一段话；打 {{ 会弹出可引用的变量"
              :disabled="readonly"
              :status="hasProblem(`step${index}.prompt`) ? 'error' : undefined"
              @input="onPromptInput(index, $event)"
              @keydown="onPromptKeydown(index, $event)"
              @blur="closeCompletion"
            />
            <!-- 「{{」自动补全：候选就是下面芯片那一份集合，键盘上下选、回车补。 -->
            <div v-if="completion.step === index && completion.items.length" class="completion">
              <button
                v-for="(name, i) in completion.items"
                :key="name"
                type="button"
                :class="{ 'completion-active': i === completion.active }"
                @mousedown.prevent="applyCompletion(name)"
              >
                {{ '{' + '{' + name + '}' + '}' }}
              </button>
            </div>
          </div>
          <!-- 引用芯片按来源分组：整段是这一步拿全部产物，带点的是 JSON 步按字段取。
               列什么是后端说了算——声明过的输入，加上前面并行组的产物；同组并行
               的那几步这时还没有输出，所以不列（见 _check_template_vars）。 -->
          <div
            v-for="group in chipGroupsFor(index)"
            :key="group.label"
            class="chip-row"
            :class="{ 'chip-row-step': group.label !== '输入变量' }"
          >
            <span class="chip-from">{{ group.label }}</span>
            <button
              v-for="chip in group.chips"
              :key="chip"
              type="button"
              class="chip"
              :class="{ 'chip-field': chip.includes('.') }"
              :disabled="readonly"
              :title="chip.includes('.') ? '插入按字段取值' : '插入整段引用'"
              v-text="chip"
              @mousedown.prevent="insertChip(index, chip, $event)"
            />
          </div>
        </a-form-item>

        <div class="step-body">
          <span class="body-label">产出变量</span>
          <a-input
            v-model:value="step.output_var"
            placeholder="供后续步骤引用，留空则不进上下文"
            size="small"
            class="w-var"
            :disabled="readonly"
          />
        </div>

        <div v-if="showAdvanced(index)" class="step-foot">
          <a-input
            v-model:value="step.group_key"
            placeholder="并行分组"
            size="small"
            class="w-key"
            :disabled="readonly"
            :status="hasProblem(`step${index}.group`) ? 'error' : undefined"
          />
          <a-radio-group v-model:value="step.output_kind" size="small" button-style="solid" :disabled="readonly">
            <a-radio-button value="markdown">Markdown</a-radio-button>
            <a-radio-button value="json">JSON</a-radio-button>
          </a-radio-group>
          <a-select
            v-if="step.output_kind === 'json'"
            v-model:value="step.output_keys"
            mode="tags"
            :open="false"
            placeholder="期望字段，回车添加"
            size="small"
            class="w-keys"
            :disabled="readonly"
          />
          <a-select
            v-model:value="step.model_config_id"
            :options="modelOptions"
            allow-clear
            placeholder="默认对话模型"
            size="small"
            class="w-model"
            :disabled="readonly"
          />
          <a-checkbox v-model:checked="step.optional" :disabled="readonly">可选步骤</a-checkbox>
        </div>

        <!-- 「格式字段」的心结说破：字段不是智能体定义的，是这一步的模板要的。
             v-pre 挂内层：挂自身会把 v-if 一起跳过编译。 -->
        <div v-if="showAdvanced(index) && step.output_kind === 'json'" class="hint block keys-hint">
          <span v-pre>
            期望字段不由智能体决定——人设只管口吻与立场；模型输出哪些 JSON 键，取决于你在
            Prompt 模板里怎么要求。这里写的键，就是声明那份契约，下游 {{产物.字段}} 引用的也是它们。
          </span>
        </div>
      </div>

      <a-space>
        <a-button size="small" :disabled="readonly" @click="addStep(false)">
          <PlusOutlined /> 加一步
        </a-button>
        <a-button size="small" :disabled="readonly" @click="addStep(true)">
          <PlusOutlined /> 加一个并行步骤
        </a-button>
      </a-space>
    </a-form>

    <template #footer>
      <div class="footer">
        <span class="muted">
          {{
            readonly
              ? '内置流程由代码维护，需要改动请先「复制」成自己的版本。'
              : '保存即整份替换：步骤之间引用关系已闭合才能存下。'
          }}
        </span>
        <a-button @click="emit('update:open', false)">{{ readonly ? '关闭' : '取消' }}</a-button>
        <a-button v-if="!readonly" type="primary" :loading="saving" @click="submit">保存</a-button>
      </div>
    </template>
  </a-drawer>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  MinusOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'
import {
  agentsApi,
  settingsApi,
  workflowsApi,
  type Agent,
  type ModelConfig,
  type Workflow,
  type WorkflowPayload,
  type WorkflowStep,
} from '@/api'

const props = defineProps<{
  open: boolean
  workflow: Workflow | null
  readonly?: boolean
  /** 起步模板：通常是全部内置工作流（带完整步骤），只在新建时用。 */
  presets?: Workflow[]
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  saved: [workflow: Workflow]
}>()

interface InputFieldForm {
  name: string
  label: string
  required: boolean
}

interface StepForm {
  key: string
  group_key: string
  agent_slug: string
  prompt_tpl: string
  output_kind: string
  output_keys: string[]
  output_var: string
  model_config_id: number | null
  optional: boolean
}

const emptyStep = (groupKey = ''): StepForm => ({
  key: '',
  group_key: groupKey,
  agent_slug: '',
  prompt_tpl: '',
  output_kind: 'markdown',
  output_keys: [],
  output_var: '',
  model_config_id: null,
  optional: false,
})

/** 后端步骤 → 表单行：编辑回填与「从内置起步」共用同一份映射。 */
function toStepForm(step: WorkflowStep): StepForm {
  return {
    key: step.key,
    group_key: step.group_key,
    agent_slug: step.agent_slug,
    prompt_tpl: step.prompt_tpl,
    output_kind: step.output_kind,
    output_keys: [...(step.output_keys ?? [])],
    output_var: step.output_var ?? '',
    model_config_id: step.model_config_id ?? null,
    optional: step.optional,
  }
}

const form = reactive({
  slug: '',
  name: '',
  description: '',
  enabled: true,
  inputs: [] as InputFieldForm[],
  steps: [emptyStep()] as StepForm[],
})

const saving = ref(false)
const agents = ref<Agent[]>([])
const chatModels = ref<ModelConfig[]>([])
const presetId = ref<number | undefined>(undefined)

/** 步骤卡片的「高级」折叠：只读展示时整摊开，不然看不到分组与输出契约。 */
const advancedOpen = reactive<Record<number, boolean>>({})
function showAdvanced(index: number) {
  return !!props.readonly || !!advancedOpen[index]
}

/** 打开或换入一批步骤时，把配过高级项的步骤直接摊开，免得藏住 JSON 契约与模型选择。 */
function autoExpandAdvanced(steps: StepForm[]) {
  steps.forEach((step, index) => {
    if (step.output_kind === 'json' || step.model_config_id != null || step.optional) {
      advancedOpen[index] = true
    }
  })
}

/** 校验红框：点保存才亮灯，之后随填随灭（problemIds 是响应式算出来的）。 */
const showProblems = ref(false)

const editing = computed(() => !!props.workflow)
const title = computed(() => {
  if (props.readonly) return `查看工作流 · ${props.workflow?.name ?? ''}`
  return editing.value ? `编辑工作流 · ${props.workflow?.name ?? ''}` : '新建工作流'
})
const groupCount = computed(
  () => new Set(form.steps.map((step) => step.group_key.trim() || '(未命名)')).size,
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
const presetOptions = computed(() =>
  (props.presets ?? []).map((workflow) => ({ value: workflow.id, label: workflow.name })),
)

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

function stepProblem(index: number) {
  if (!showProblems.value) return false
  const prefix = `step${index}.`
  for (const id of problemIds.value) if (id.startsWith(prefix)) return true
  return false
}

/** 高级折叠的摊开状态整体清一次，换入另一批步骤时不残留旧下标。 */
function clearAdvanced() {
  for (const index of Object.keys(advancedOpen)) delete advancedOpen[Number(index)]
}

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    showProblems.value = false
    closeCompletion()
    clearAdvanced()
    const source = props.workflow
    presetId.value = undefined
    form.slug = source?.slug ?? ''
    form.name = source?.name ?? ''
    form.description = source?.description ?? ''
    form.enabled = source?.enabled ?? true
    form.inputs = (source?.inputs ?? []).map((field) => ({ ...field }))
    form.steps = source ? source.steps.map(toStepForm) : [emptyStep('g1')]
    autoExpandAdvanced(form.steps)
    // 每次打开都重取：编辑期间新建的智能体应该马上能选到。
    try {
      const [agentData, modelData] = await Promise.all([
        agentsApi.list(),
        settingsApi.listModels('chat'),
      ])
      agents.value = agentData.items
      chatModels.value = modelData.items
    } catch {
      // 拦截器已提示；下拉为空时保存会被后端的引用检查挡住。
    }
  },
)

function addInput() {
  form.inputs.push({ name: '', label: '', required: true })
}

function addStep(parallel: boolean) {
  const last = form.steps[form.steps.length - 1]
  // 并行步骤沿用上一个的分组名；普通步骤开一个新组，默认就是顺序执行。
  form.steps.push(emptyStep(parallel ? last?.group_key ?? '' : `g${form.steps.length + 1}`))
}

/** 从内置流程起步：整份搬来输入与步骤，名字带「副本」，标识留空必须自己起。 */
function onPresetChange(value: number | undefined) {
  const source = (props.presets ?? []).find((workflow) => workflow.id === value)
  if (!source) {
    // 清空起步选择 = 回到空白新建：选择器与表单内容必须同进同退，
    // 不然显示「空白工作流」、底下还留着刚才那份，比从零配更容易出错。
    form.slug = ''
    form.name = ''
    form.description = ''
    form.inputs = []
    form.steps = [emptyStep('g1')]
    clearAdvanced()
    return
  }
  form.slug = ''
  form.name = `${source.name} 副本`
  form.description = source.description ?? ''
  form.inputs = source.inputs.map((field) => ({ ...field }))
  form.steps = source.steps.map(toStepForm)
  clearAdvanced()
  autoExpandAdvanced(form.steps)
}

function move(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= form.steps.length) return
  const [step] = form.steps.splice(index, 1)
  form.steps.splice(target, 0, step)
}

/** 每一步属于第几个并行组：分组名与上一步相同就是同一组（后端同一套口径）。 */
const groupSequence = computed(() => {
  const sequences: number[] = []
  let current = -1
  let previous: string | null = null
  for (const step of form.steps) {
    const key = step.group_key.trim()
    if (key !== previous) {
      current += 1
      previous = key
    }
    sequences.push(current)
  }
  return sequences
})

interface ChipGroup {
  label: string
  chips: string[]
}

/**
 * 这一步的模板里能引用谁，按来源分组：声明过的输入，加上**前面**并行组的产物。
 *
 * JSON 步骤额外列出按字段取值（``{{产物.字段}}``，字段来自它的期望字段）——后端
 * 只允许 JSON 步这样取，芯片列出的集合必须和校验一致，否则点出来的是一个存不下的
 * 模板。分组名写明来源步骤，用户不必再记「这个名字是哪步来的」。
 */
function chipGroupsFor(index: number): ChipGroup[] {
  const own = groupSequence.value[index] ?? 0
  const groups: ChipGroup[] = []
  const inputs = form.inputs
    .map((field) => field.name.trim())
    .filter((name) => name)
    .map((name) => `{{${name}}}`)
  if (inputs.length) groups.push({ label: '输入变量', chips: [...new Set(inputs)] })
  form.steps.forEach((step, stepIndex) => {
    if ((groupSequence.value[stepIndex] ?? 0) >= own) return
    const variable = step.output_var.trim()
    if (!variable) return
    const chips = [`{{${variable}}}`]
    if (step.output_kind === 'json') {
      for (const key of step.output_keys) {
        const field = key.trim()
        if (field) chips.push(`{{${variable}.${field}}}`)
      }
    }
    const label = `第 ${stepIndex + 1} 步 · ${step.key.trim() || variable}`
    groups.push({ label, chips: [...new Set(chips)] })
  })
  return groups
}

/** 可引用变量全集（芯片集合的拍平版），「{{」自动补全用的是同一份。 */
function chipsFor(index: number): string[] {
  return chipGroupsFor(index).flatMap((group) => group.chips)
}

/** 点芯片：插到光标处，选中的文字被替换；没在用这一步的输入框就 append 到末尾。 */
function insertChip(index: number, insert: string, event: MouseEvent) {
  const step = form.steps[index]
  if (!step) return
  closeCompletion()
  const card = (event.currentTarget as HTMLElement).closest('.step-card')
  const focused = document.activeElement as HTMLTextAreaElement | null
  const area = card && focused && card.contains(focused) ? focused : null
  if (!area) {
    step.prompt_tpl += insert
    return
  }
  const from = area.selectionStart ?? step.prompt_tpl.length
  const to = area.selectionEnd ?? from
  step.prompt_tpl = step.prompt_tpl.slice(0, from) + insert + step.prompt_tpl.slice(to)
  const caret = from + insert.length
  void nextTick(() => {
    area.focus()
    area.setSelectionRange(caret, caret)
  })
}

/**
 * 「{{」自动补全：光标前正打着半个变量名时，就地列出可引用的候选。
 * 候选集合与芯片同源，记不住字段名也能顺着敲出来；记住的那位照样直接打字。
 */
const completion = reactive<{ step: number; items: string[]; active: number }>({
  step: -1,
  items: [],
  active: 0,
})
let completionArea: HTMLTextAreaElement | null = null
let completionFrom = 0
let completionTo = 0

function onPromptInput(index: number, event: Event) {
  const area = event.target as HTMLTextAreaElement | null
  if (!area || props.readonly) return
  completionArea = area
  const caret = area.selectionStart ?? area.value.length
  const match = /\{\{([^{}]*)$/.exec(area.value.slice(0, caret))
  if (!match) {
    closeCompletion()
    return
  }
  const prefix = match[1]
  const items = chipsFor(index)
    .map((chip) => chip.slice(2, -2))
    .filter((name) => name.startsWith(prefix))
    .slice(0, 12)
  if (!items.length) {
    closeCompletion()
    return
  }
  completion.step = index
  completion.items = items
  completion.active = 0
  completionFrom = caret - match[0].length
  completionTo = caret
}

function closeCompletion() {
  completion.step = -1
  completion.items = []
  completionArea = null
}

function applyCompletion(name: string) {
  const step = form.steps[completion.step]
  const area = completionArea
  if (!step || !area) return
  const insert = `{{${name}}}`
  step.prompt_tpl = step.prompt_tpl.slice(0, completionFrom) + insert + step.prompt_tpl.slice(completionTo)
  completion.step = -1
  completion.items = []
  const caret = completionFrom + insert.length
  void nextTick(() => {
    area.focus()
    area.setSelectionRange(caret, caret)
  })
}

function onPromptKeydown(index: number, event: KeyboardEvent) {
  if (completion.step !== index || !completion.items.length) return
  const last = completion.items.length - 1
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    completion.active = completion.active >= last ? 0 : completion.active + 1
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    completion.active = completion.active <= 0 ? last : completion.active - 1
  } else if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault()
    applyCompletion(completion.items[completion.active])
  } else if (event.key === 'Escape') {
    closeCompletion()
  }
}

async function submit() {
  if (props.readonly) return
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
  if (props.workflow) payload.enabled = form.enabled

  saving.value = true
  try {
    const saved = props.workflow
      ? await workflowsApi.update(props.workflow.id, payload)
      : await workflowsApi.create(payload)
    message.success('已保存')
    emit('saved', saved)
    emit('update:open', false)
  } catch {
    return // 拦截器已把后端的校验原因弹出来了。
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
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
.step-card {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 12px;
  background: var(--paper);
}
.step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
/* 分组名是高级项，但它是执行语义的一部分，卡片头上至少让人看见现状。 */
.group-tag {
  margin-inline-end: 0;
}
/* 人设下拉项：名字下面跟一行小字描述。 */
.agent-opt {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
  padding: 2px 0;
}
.agent-opt-desc {
  color: var(--text-3);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 选中人设后的说明行：描述 + 展开系统提示词的入口。 */
.agent-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: -2px 0 8px 28px;
  font-size: 12px;
}
.keys-hint {
  margin-top: 4px;
}
.prompt-wrap {
  position: relative;
}
.completion {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 20;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
  padding: 6px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
}
.completion button {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  padding: 0 6px;
  cursor: pointer;
}
.completion-active {
  border-color: var(--signal-border) !important;
  background: var(--signal-bg) !important;
  color: var(--signal-text) !important;
}
.chip-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.chip-row-step {
  margin-top: 4px;
}
/* 来源标签：一眼看出这排芯片来自哪一步，不用回想变量名是谁产的。 */
.chip-from {
  color: var(--text-3);
  font-size: 12px;
  white-space: nowrap;
}
.chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  padding: 0 6px;
  cursor: pointer;
}
.chip:hover:not(:disabled) {
  border-color: var(--signal-border);
  background: var(--signal-bg);
  color: var(--signal-text);
}
.chip:disabled {
  cursor: default;
  opacity: 0.6;
}
.chip-field {
  color: var(--text-3);
}
.step-no {
  width: 20px;
  font-weight: 600;
  color: var(--text-3);
}
.w-key {
  width: 150px;
}
.w-agent {
  width: 200px;
}
.w-var {
  width: 260px;
}
.w-keys {
  width: 220px;
}
.w-model {
  width: 180px;
}
.spacer {
  flex: 1;
}
.tight {
  margin-bottom: 8px;
}
.step-body {
  display: flex;
  align-items: center;
  gap: 8px;
}
.body-label {
  color: var(--text-3);
  font-size: 12px;
  white-space: nowrap;
}
.step-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--hairline);
}
/* 保存拦下时标出缺口所在：整卡描红，随填随灭。 */
.field-problem {
  border-color: #ff4d4f;
}
.footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.footer .muted {
  margin-right: auto;
}
</style>
