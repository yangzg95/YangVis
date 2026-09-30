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
      <div class="row">
        <a-form-item label="名称" required class="col">
          <a-input v-model:value="form.name" :maxlength="128" :disabled="readonly" />
        </a-form-item>
        <a-form-item label="标识" required class="col">
          <a-input
            v-model:value="form.slug"
            :disabled="editing || readonly"
            placeholder="小写字母、数字与短横线"
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
      <div v-for="(field, index) in form.inputs" :key="index" class="field-row">
        <a-input v-model:value="field.name" placeholder="变量名 topic" class="w-name" :disabled="readonly" />
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
      </div>

      <div v-for="(step, index) in form.steps" :key="index" class="step-card">
        <div class="step-head">
          <span class="step-no">{{ index + 1 }}</span>
          <a-input
            v-model:value="step.key"
            placeholder="步骤标识"
            size="small"
            class="w-key"
            :disabled="readonly"
          />
          <a-input
            v-model:value="step.group_key"
            placeholder="并行分组"
            size="small"
            class="w-key"
            :disabled="readonly"
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
          />
          <span class="spacer" />
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

        <a-form-item label="Prompt 模板" class="tight">
          <a-textarea
            v-model:value="step.prompt_tpl"
            :rows="4"
            placeholder="就{{topic}}写一段话"
            :disabled="readonly"
          />
          <!-- 引用芯片：点一下就把 {{...}} 插到光标处，省得手打花括号打错一位。
               列什么是后端说了算——声明过的输入，加上前面并行组的产物；同组并行
               的那几步这时还没有输出，所以不列（见 _check_template_vars）。 -->
          <div v-if="chipsFor(index).length" class="chip-row">
            <button
              v-for="chip in chipsFor(index)"
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

        <div class="step-foot">
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
          <a-input
            v-model:value="step.output_var"
            placeholder="产物变量名（留空不进上下文）"
            size="small"
            class="w-var"
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
} from '@/api'

const props = defineProps<{
  open: boolean
  workflow: Workflow | null
  readonly?: boolean
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

const editing = computed(() => !!props.workflow)
const title = computed(() => {
  if (props.readonly) return `查看工作流 · ${props.workflow?.name ?? ''}`
  return editing.value ? `编辑工作流 · ${props.workflow?.name ?? ''}` : '新建工作流'
})
const groupCount = computed(
  () => new Set(form.steps.map((step) => step.group_key.trim() || '(未命名)')).size,
)
const agentOptions = computed(() =>
  agents.value.map((agent) => ({ value: agent.slug, label: agent.name })),
)
const modelOptions = computed(() =>
  chatModels.value
    .filter((config) => config.last_test_ok)
    .map((config) => ({ value: config.id, label: config.title })),
)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    const source = props.workflow
    form.slug = source?.slug ?? ''
    form.name = source?.name ?? ''
    form.description = source?.description ?? ''
    form.enabled = source?.enabled ?? true
    form.inputs = (source?.inputs ?? []).map((field) => ({ ...field }))
    form.steps = source
      ? source.steps.map((step) => ({
          key: step.key,
          group_key: step.group_key,
          agent_slug: step.agent_slug,
          prompt_tpl: step.prompt_tpl,
          output_kind: step.output_kind,
          output_keys: [...(step.output_keys ?? [])],
          output_var: step.output_var ?? '',
          model_config_id: step.model_config_id ?? null,
          optional: step.optional,
        }))
      : [emptyStep('g1')]
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

/**
 * 这一步的模板里能引用谁：声明过的输入，加上**前面**并行组的产物。
 *
 * JSON 步骤额外列出按字段取值（``{{产物.字段}}``，字段来自它的期望字段）——后端
 * 只允许 JSON 步这样取，芯片列出的集合必须和校验一致，否则点出来的是一个存不下的
 * 模板。
 */
function chipsFor(index: number): string[] {
  const own = groupSequence.value[index] ?? 0
  const chips: string[] = []
  for (const field of form.inputs) {
    const name = field.name.trim()
    if (name) chips.push(`{{${name}}}`)
  }
  form.steps.forEach((step, stepIndex) => {
    if ((groupSequence.value[stepIndex] ?? 0) >= own) return
    const variable = step.output_var.trim()
    if (!variable) return
    chips.push(`{{${variable}}}`)
    if (step.output_kind !== 'json') return
    for (const key of step.output_keys) {
      const field = key.trim()
      if (field) chips.push(`{{${variable}.${field}}}`)
    }
  })
  return [...new Set(chips)]
}

/** 点芯片：插到光标处，选中的文字被替换；没在用这一步的输入框就 append 到末尾。 */
function insertChip(index: number, insert: string, event: MouseEvent) {
  const step = form.steps[index]
  if (!step) return
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

function firstIncomplete(): string | null {
  if (!form.name.trim()) return '请填写工作流名称'
  if (!editing.value && !/^[a-z0-9][a-z0-9-]*$/.test(form.slug.trim()))
    return '标识只能包含小写字母、数字与短横线'
  for (const field of form.inputs) {
    if (!field.name.trim()) return '输入变量名不能为空'
  }
  for (const [index, step] of form.steps.entries()) {
    if (!step.key.trim() || !step.group_key.trim() || !step.agent_slug || !step.prompt_tpl.trim())
      return `第 ${index + 1} 步的标识、分组、人设与模板都要填`
  }
  return null
}

async function submit() {
  if (props.readonly) return
  const problem = firstIncomplete()
  if (problem) {
    message.warning(problem)
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
.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
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
  width: 200px;
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
.step-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
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
