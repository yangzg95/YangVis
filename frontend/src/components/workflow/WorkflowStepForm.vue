<template>
  <div class="step-form" :class="{ 'field-problem': cardProblem }">
    <div class="step-head">
      <span class="step-no">{{ index + 1 }}</span>
      <a-input
        v-model:value="step.key"
        placeholder="步骤标识"
        size="small"
        class="w-key"
        :disabled="readonly"
        :status="bad(`step${index}.key`) ? 'error' : undefined"
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
        :status="bad(`step${index}.agent`) ? 'error' : undefined"
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
        @click="$emit('toggle-advanced', index)"
      >
        {{ advancedOpen ? '收起' : '高级' }}
      </a-button>
      <a-button size="small" type="text" :disabled="index === 0 || readonly" @click="$emit('move', -1)">
        <ArrowUpOutlined />
      </a-button>
      <a-button
        size="small"
        type="text"
        :disabled="index >= total - 1 || readonly"
        @click="$emit('move', 1)"
      >
        <ArrowDownOutlined />
      </a-button>
      <a-button
        size="small"
        type="text"
        danger
        :disabled="total <= 1 || readonly"
        @click="$emit('remove')"
      >
        <DeleteOutlined />
      </a-button>
    </div>

    <!-- 选中后把这个人设是谁、管什么摆在手边；点开能看全系统提示词，
         配下游引用时不用切去智能体页对照。 -->
    <div v-if="agent" class="agent-hint">
      <span class="muted">{{ agent.description || '这个人设没有描述' }}</span>
      <a-popover placement="left" trigger="click" title="人设（系统提示词）">
        <template #content>
          <pre
            style="
              max-width: 420px;
              max-height: 320px;
              overflow: auto;
              white-space: pre-wrap;
              word-break: break-word;
              font-size: var(--fs-xs);
              margin: 0;
            "
          >{{ agent.system_prompt }}</pre>
        </template>
        <a-button size="small" type="link">查看人设</a-button>
      </a-popover>
    </div>

    <!-- 上一步（紧邻的前一个并行组）产出的变量名直接摆在写模板的位置：
         不必先开「插入引用」才知道这一步能接什么；芯片与弹层同源，点一下即插入。 -->
    <div v-if="upstreamGroups.length" class="upstream-hint">
      <span class="upstream-label">上一步产出</span>
      <button
        v-for="group in upstreamGroups"
        :key="group.label"
        type="button"
        class="chip upstream-chip"
        :title="`${group.label}，点击插入`"
        :disabled="readonly"
        @mousedown.prevent="insertChip(group.chips[0])"
      >
        {{ group.chips[0] }}
      </button>
    </div>

    <a-form-item label="Prompt 模板" class="tight">
      <div class="prompt-wrap">
        <a-textarea
          ref="areaEl"
          v-model:value="step.prompt_tpl"
          :rows="4"
          placeholder="就{{topic}}写一段话；打 {{ 会弹出可引用的变量"
          :disabled="readonly"
          :status="bad(`step${index}.prompt`) ? 'error' : undefined"
          @input="onPromptInput"
          @keydown="onPromptKeydown"
          @blur="onPromptBlur"
        />
        <!-- 「{{」自动补全：候选就是插入引用弹层里那一份集合，键盘上下选、回车补。 -->
        <div v-if="completionItems.length" class="completion">
          <button
            v-for="(name, i) in completionItems"
            :key="name"
            type="button"
            :class="{ 'completion-active': i === completionActive }"
            @mousedown.prevent="applyCompletion(name)"
          >
            {{ '{' + '{' + name + '}' + '}' }}
          </button>
        </div>
      </div>

      <!-- 引用芯片按来源分组收进弹层：整段是这一步拿全部产物，带点的是 JSON 步按字段取。
           列什么是后端说了算——声明过的输入，加上前面并行组的产物；同组并行
           的那几步这时还没有输出，所以不列（见后端 _check_template_vars）。
           触发按钮 mousedown.prevent：弹层照开，但 textarea 不丢焦点与选区。 -->
      <a-popover
        trigger="click"
        placement="bottomLeft"
        overlay-class-name="ref-popover"
        title="插入引用（按来源分组）"
      >
        <template #content>
          <div class="ref-pop">
            <div v-for="group in chips" :key="group.label" class="ref-pop-group">
              <div class="ref-pop-label">{{ group.label }}</div>
              <div class="ref-pop-chips">
                <button
                  v-for="chip in group.chips"
                  :key="chip"
                  type="button"
                  class="chip"
                  :class="{ 'chip-field': chip.includes('.') }"
                  :title="chip.includes('.') ? '插入按字段取值' : '插入整段引用'"
                  v-text="chip"
                  @mousedown.prevent="insertChip(chip)"
                />
              </div>
            </div>
          </div>
        </template>
        <a-button
          size="small"
          class="insert-ref-btn"
          :disabled="readonly || !hasChips"
          title="先声明输入变量，或由前面步骤产出变量"
          @mousedown.prevent
        >
          ＋插入引用
        </a-button>
      </a-popover>
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

    <div v-if="advancedOpen" class="step-foot">
      <a-input
        v-model:value="step.group_key"
        placeholder="并行分组"
        size="small"
        class="w-key"
        :disabled="readonly"
        :status="bad(`step${index}.group`) ? 'error' : undefined"
      />
      <a-radio-group
        v-model:value="step.output_kind"
        size="small"
        button-style="solid"
        :disabled="readonly"
      >
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
    <div v-if="advancedOpen && step.output_kind === 'json'" class="hint block keys-hint">
      <span v-pre>
        期望字段不由智能体决定——人设只管口吻与立场；模型输出哪些 JSON 键，取决于你在
        Prompt 模板里怎么要求。这里写的键，就是声明那份契约，下游 {{产物.字段}} 引用的也是它们。
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
} from '@ant-design/icons-vue'
import type { Agent, ModelConfig } from '@/api'
import type { ChipGroup, StepForm } from './workflowLayout'

const props = withDefaults(
  defineProps<{
    /** 表单里的那一行：直接改它的字段，父级的 reactive 对象负责收集。 */
    step: StepForm
    index: number
    /** 一共几步：删除与下移按钮的禁用条件。 */
    total: number
    readonly: boolean
    /** 这一步能引用谁，由父级按同一份布局算好传进来。 */
    chips: ChipGroup[]
    /** 紧邻前一个并行组的产出，卡片上常驻展示；第一步为空数组。 */
    upstreamGroups: ChipGroup[]
    agent: Agent | null
    agentOptions: { value: string; label: string; agent: Agent }[]
    modelOptions: { value: number; label: string }[]
    /** 校验缺口：与父级 problemIds 同一份，红框随填随灭。 */
    problemIds: Set<string>
    showProblems: boolean
    advancedOpen: boolean
  }>(),
  { agent: null, upstreamGroups: () => [] },
)

defineEmits<{
  (e: 'move', delta: number): void
  (e: 'remove'): void
  (e: 'toggle-advanced', index: number): void
}>()

function bad(id: string) {
  return props.showProblems && props.problemIds.has(id)
}

/** 整卡描红：这一步任一必填没齐，卡片就亮起来。 */
const cardProblem = computed(() => {
  if (!props.showProblems) return false
  const prefix = `step${props.index}.`
  for (const id of props.problemIds) if (id.startsWith(prefix)) return true
  return false
})

const areaEl = ref<HTMLTextAreaElement[] | HTMLTextAreaElement | null>(null)
function textarea(): HTMLTextAreaElement | null {
  const raw = areaEl.value as HTMLTextAreaElement | HTMLTextAreaElement[] | null
  return Array.isArray(raw) ? (raw[0] ?? null) : raw
}

/**
 * 「{{」自动补全：光标前正打着半个变量名时，就地列出可引用的候选。
 * 候选集合与芯片同源，记住名字的那位照样直接打字。
 */
const completionItems = ref<string[]>([])
const completionActive = ref(0)
let completionFrom = 0
let completionTo = 0

const allChips = computed(() => props.chips.flatMap((group) => group.chips))
const hasChips = computed(() => allChips.value.length > 0)

function closeCompletion() {
  completionItems.value = []
}

/**
 * 最后一次在 textarea 里的选区：点「插入引用」开弹层后焦点可能已经离开框，
 * 芯片得知道往哪儿插。
 */
let lastSel: { start: number; end: number } | null = null
function rememberSel() {
  const area = textarea()
  if (area && document.activeElement === area) {
    lastSel = {
      start: area.selectionStart ?? area.value.length,
      end: area.selectionEnd ?? area.value.length,
    }
  }
}

/** 失焦：先存住选区再收自动补全，弹层里点芯片时才插得回原位。 */
function onPromptBlur() {
  rememberSel()
  closeCompletion()
}

/**
 * 点芯片：① 框还拿着焦点 → 用实时选区；② 焦点已在弹层/别处 → 用 lastSel；
 * ③ 从没进过框 → append 到末尾。splice 后焦点会回到 textarea。
 */
function insertChip(insert: string) {
  closeCompletion()
  const area = textarea()
  if (!area) return
  if (document.activeElement === area) {
    const start = area.selectionStart ?? area.value.length
    splice(area, start, area.selectionEnd ?? start, insert)
    return
  }
  if (lastSel) {
    splice(area, lastSel.start, lastSel.end, insert)
    return
  }
  props.step.prompt_tpl += insert
}

function onPromptInput() {
  const area = textarea()
  if (!area || props.readonly) return
  const caret = area.selectionStart ?? area.value.length
  rememberSel() // 边打字边刷新：之后开弹层拿到的就是最新光标。
  const match = /\{\{([^{}]*)$/.exec(area.value.slice(0, caret))
  if (!match) {
    closeCompletion()
    return
  }
  const items = allChips.value
    .map((chip) => chip.slice(2, -2))
    .filter((name) => name.startsWith(match[1]))
    .slice(0, 12)
  completionItems.value = items
  completionActive.value = 0
  completionFrom = caret - match[0].length
  completionTo = caret
}

function applyCompletion(name: string) {
  const area = textarea()
  if (!area) return
  splice(area, completionFrom, completionTo, `{{${name}}}`)
  closeCompletion()
}

/** 改模板并把光标放回插入点之后：受控 textarea 不这么做，选区会跳到文末。 */
function splice(area: HTMLTextAreaElement, from: number, to: number, insert: string) {
  props.step.prompt_tpl = props.step.prompt_tpl.slice(0, from) + insert + props.step.prompt_tpl.slice(to)
  const caret = from + insert.length
  lastSel = { start: caret, end: caret }
  void nextTick(() => {
    area.focus()
    area.setSelectionRange(caret, caret)
  })
}

function onPromptKeydown(event: KeyboardEvent) {
  const items = completionItems.value
  if (!items.length) return
  const last = items.length - 1
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    completionActive.value = completionActive.value >= last ? 0 : completionActive.value + 1
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    completionActive.value = completionActive.value <= 0 ? last : completionActive.value - 1
  } else if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault()
    applyCompletion(items[completionActive.value])
  } else if (event.key === 'Escape') {
    closeCompletion()
  }
}

// 换看另一步时（画布上点别的节点）补全候选早就不适用了，收起浮层。
watch(
  () => props.index,
  () => closeCompletion(),
)
</script>

<style scoped>
.step-form {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px;
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
  font-size: var(--fs-xs);
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
  font-size: var(--fs-xs);
}
.keys-hint {
  margin-top: 4px;
}
/* 上一步产出行：缩进与人设说明对齐，芯片就是可点的引用。 */
.upstream-hint {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0 0 8px 28px;
}
.upstream-label {
  color: var(--text-3);
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.prompt-wrap {
  position: relative;
}
.completion {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: var(--z-dropdown);
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
  font-size: var(--fs-xs);
  line-height: 1.6;
  padding: 0 6px;
  cursor: pointer;
}
.completion-active {
  border-color: var(--signal-border) !important;
  background: var(--signal-bg) !important;
  color: var(--signal-text) !important;
}
/* 打开引用弹层的入口：芯片按来源分组都在弹层里。 */
.insert-ref-btn {
  margin-top: 6px;
}
.ref-pop {
  max-width: 360px;
  max-height: 320px;
  overflow: auto;
}
.ref-pop-group {
  margin-bottom: 8px;
}
.ref-pop-group:last-child {
  margin-bottom: 0;
}
/* 来源标签：一眼看出这排芯片来自哪一步，不用回想变量名是谁产的。 */
.ref-pop-label {
  color: var(--text-3);
  font-size: var(--fs-xs);
  white-space: nowrap;
  margin-bottom: 4px;
}
.ref-pop-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
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
  font-size: var(--fs-xs);
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
  border-color: var(--danger);
}
/* 窄屏卡头控件太多：允许折行，别把卡撑出横向滚动。 */
@media (max-width: 480px) {
  .step-head {
    flex-wrap: wrap;
  }
}
</style>
