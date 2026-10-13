<template>
  <div class="run-scene">
    <!-- 运行输入：整页长材料一多就糊屏，默认收成一行，点开再看全文。 -->
    <div v-if="inputPairs.length" class="scene-inputs">
      <div class="scene-group-head" @click="inputsOpen = !inputsOpen">
        <span class="scene-caret">
          <RightOutlined v-if="!inputsOpen" />
          <DownOutlined v-else />
        </span>
        <a-tag color="blue" class="scene-group-tag">运行输入</a-tag>
        <span class="muted">{{ inputPairs.length }} 项 · {{ inputPairs.map(([name]) => name).join('、') }}</span>
      </div>
      <div v-show="inputsOpen" class="scene-group-body">
        <div v-for="[name, value] in inputPairs" :key="name" class="scene-input">
          <div class="scene-line">
            <span class="scene-field-name">{{ name }}</span>
            <span class="spacer" />
            <a-tooltip title="复制">
              <button type="button" class="scene-copy" @click="copyScene(`输入 ${name}`, value)">
                <CopyOutlined />
              </button>
            </a-tooltip>
          </div>
          <pre>{{ value }}</pre>
        </div>
      </div>
    </div>
    <p v-if="detail.error_msg" class="danger">{{ detail.error_msg }}</p>

    <div class="scene-tools">
      <span class="muted">每步现场可折叠</span>
      <span class="spacer" />
      <a-button size="small" type="link" @click="collapseAll(false)">展开全部</a-button>
      <a-button size="small" type="link" @click="collapseAll(true)">收起全部</a-button>
    </div>

    <div v-for="step in detail.steps" :key="step.id" class="scene-step">
      <div class="scene-head" @click="toggleStep(step)">
        <span class="scene-caret">
          <RightOutlined v-if="isStepCollapsed(step)" />
          <DownOutlined v-else />
        </span>
        <a-tag :color="statusColor(step.status)">{{ statusText(step.status) }}</a-tag>
        <span class="live-key">{{ step.key }}</span>
        <a-tag v-if="step.group_key" class="scene-group-key" :title="`并行组 ${step.group_key}：同组步骤并发执行，组间按顺序依次跑`">
          {{ step.group_key }}
        </a-tag>
        <span class="muted">{{ step.agent_name || step.agent_slug }}</span>
        <span v-if="step.optional" class="muted">可选</span>
        <span class="spacer" />
        <span v-if="step.model_name" class="muted">{{ step.model_name }}</span>
        <span v-if="step.elapsed_ms != null" class="muted">{{ fmtElapsed(step.elapsed_ms) }}</span>
        <a-tooltip
          title="先调整这一步收到的输入变量，再用流程当前定义重跑一次；只重跑这一步，不写回业务结果——要新产物请回功能页面重新发起。"
        >
          <a-button size="small" :disabled="busy" @click.stop="emit('rerun', step)">
            重跑这步
          </a-button>
        </a-tooltip>
        <a-tooltip
          title="从这一步所在并行组继续跑到结束：本组及之后各组重置重跑，上游产物不变；可先调整输入变量。不写回业务结果——要新产物请回功能页面重新发起。"
        >
          <a-button size="small" :disabled="busy" @click.stop="emit('resume', step)">
            从此步继续
          </a-button>
        </a-tooltip>
      </div>

      <div v-show="!isStepCollapsed(step)" class="scene-body">
        <div v-if="step.error_msg" class="danger">{{ step.error_msg }}</div>

        <!-- 输入 / 输出两组，一眼分得清方向；输出默认展开（那才是大家来这一页
             要看的产物），输入组只在失败步默认展开——排查当时的入参才需要它。 -->
        <div v-if="fieldsOf(step).length || step.prompt" class="scene-group">
          <div class="scene-group-head" @click="toggleSection(step, 'in')">
            <span class="scene-caret">
              <RightOutlined v-if="!isSectionOpen(step, 'in')" />
              <DownOutlined v-else />
            </span>
            <a-tag color="blue" class="scene-group-tag">输入</a-tag>
            <span class="muted">
              {{ fieldsOf(step).length }} 个变量{{ step.prompt ? ' · 含发给模型的 prompt' : '' }}
            </span>
          </div>
          <div v-show="isSectionOpen(step, 'in')" class="scene-group-body">
            <div v-if="fieldsOf(step).length" class="scene-block">
              <div class="scene-label">这一步收到的上下文（上游产物按当时落库的结果）</div>
              <div v-for="field in fieldsOf(step)" :key="field.name" class="scene-field">
                <div class="scene-line">
                  <span class="scene-field-name">
                    {{ field.name }}
                    <a-tag v-if="field.isJson" color="blue" class="scene-field-tag">JSON</a-tag>
                  </span>
                  <span class="spacer" />
                  <a-tooltip title="复制">
                    <button
                      type="button"
                      class="scene-copy"
                      @click="copyScene(`变量 ${field.name}`, field.value)"
                    >
                      <CopyOutlined />
                    </button>
                  </a-tooltip>
                </div>
                <pre>{{ field.value }}</pre>
              </div>
            </div>
            <div v-if="step.prompt" class="scene-block">
              <div class="scene-line">
                <div class="scene-label">发给模型的 prompt</div>
                <span class="spacer" />
                <a-tooltip title="复制">
                  <button type="button" class="scene-copy" @click="copyScene('prompt', step.prompt || '')">
                    <CopyOutlined />
                  </button>
                </a-tooltip>
              </div>
              <pre>{{ step.prompt }}</pre>
            </div>
          </div>
        </div>

        <div v-if="step.output || step.parsed" class="scene-group">
          <div class="scene-group-head" @click="toggleSection(step, 'out')">
            <span class="scene-caret">
              <RightOutlined v-if="!isSectionOpen(step, 'out')" />
              <DownOutlined v-else />
            </span>
            <a-tag color="green" class="scene-group-tag">输出</a-tag>
            <span class="muted">
              {{ step.output ? `${step.output.length} 字` : '' }}
              <template v-if="step.parsed"> · 含按契约解析的对象</template>
            </span>
          </div>
          <div v-show="isSectionOpen(step, 'out')" class="scene-group-body">
            <div v-if="step.output" class="scene-block">
              <div class="scene-line">
                <div class="scene-label">模型原始输出</div>
                <span class="spacer" />
                <a-tooltip title="复制">
                  <button type="button" class="scene-copy" @click="copyScene('输出', step.output || '')">
                    <CopyOutlined />
                  </button>
                </a-tooltip>
              </div>
              <pre>{{ step.output }}</pre>
            </div>
            <div v-if="step.parsed" class="scene-block">
              <div class="scene-line">
                <div class="scene-label">按契约解析出的对象</div>
                <span class="spacer" />
                <a-tooltip title="复制">
                  <button
                    type="button"
                    class="scene-copy"
                    @click="copyScene('解析对象', JSON.stringify(step.parsed, null, 2))"
                  >
                    <CopyOutlined />
                  </button>
                </a-tooltip>
              </div>
              <pre>{{ JSON.stringify(step.parsed, null, 2) }}</pre>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { CopyOutlined, DownOutlined, RightOutlined } from '@ant-design/icons-vue'
import { copyText } from '@/utils/clipboard'
import type { WorkflowRunDetail, WorkflowRunStep } from '@/api'
import { fmtElapsed, runStatusColor as statusColor, runStatusText as statusText } from '@/utils/format'
import { stepInputFields, type StepField } from './runFields'

const props = defineProps<{
  detail: WorkflowRunDetail
  /** 有过程在跟（发起/跟随）时重跑与续跑按钮先禁用，避免两个流改同一条运行。 */
  busy: boolean
}>()

const emit = defineEmits<{ rerun: [step: WorkflowRunStep]; resume: [step: WorkflowRunStep] }>()

const inputsOpen = ref(false)
/**
 * 折叠状态只记用户显式动过的（key 用步骤行 id，一次运行内唯一）：
 * 没动过的按默认——失败步展开、其余收起；组内则输出展开、输入看状态。
 */
const stepOverrides = reactive<Record<string, boolean>>({})
const sectionOverrides = reactive<Record<string, boolean>>({})

const inputPairs = computed(() =>
  Object.entries(props.detail.inputs).filter(([, value]) => value),
)

// 详情整体换新（重跑后重取现场）时，旧的显式折叠状态就没意义了，回默认。
watch(
  () => props.detail,
  () => {
    for (const key of Object.keys(stepOverrides)) delete stepOverrides[key]
    for (const key of Object.keys(sectionOverrides)) delete sectionOverrides[key]
    inputsOpen.value = false
  },
)

function isStepCollapsed(step: WorkflowRunStep) {
  return step.id in stepOverrides ? stepOverrides[step.id] : step.status !== 'failed'
}

function toggleStep(step: WorkflowRunStep) {
  stepOverrides[step.id] = !isStepCollapsed(step)
}

function collapseAll(collapsed: boolean) {
  for (const step of props.detail.steps) stepOverrides[step.id] = collapsed
}

function sectionKey(step: WorkflowRunStep, section: 'in' | 'out') {
  return `${step.id}:${section}`
}

function isSectionOpen(step: WorkflowRunStep, section: 'in' | 'out') {
  const key = sectionKey(step, section)
  if (key in sectionOverrides) return sectionOverrides[key]
  return section === 'out' || step.status === 'failed'
}

function toggleSection(step: WorkflowRunStep, section: 'in' | 'out') {
  sectionOverrides[sectionKey(step, section)] = !isSectionOpen(step, section)
}

function fieldsOf(step: WorkflowRunStep): StepField[] {
  return stepInputFields(props.detail, step)
}

/** 现场里每一块文本都按原样复制：贴回编辑器或别处都还是落库时的那份内容。 */
async function copyScene(what: string, text: string) {
  if (await copyText(text)) message.success(`${what}已复制`)
  else message.error('复制失败，浏览器拒绝了剪贴板访问')
}
</script>

<style scoped>
.run-scene {
  min-width: 0;
}
.live-key {
  font-family: var(--font-mono);
}
/* 步骤行上原样展示落库的 group_key：并行分组的事实以当时为准，样式压低一档，
   不与状态 tag 抢视觉。 */
.scene-group-key {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 14px;
  padding: 0 4px;
}
.scene-inputs {
  margin-bottom: 12px;
}
.scene-input pre,
.scene-field pre,
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
  font-size: var(--fs-xs);
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
.scene-tools {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 4px 0 8px;
  font-size: var(--fs-xs);
}
.scene-tools :deep(.ant-btn-link) {
  padding: 0 4px;
  height: auto;
  font-size: var(--fs-xs);
}
.scene-caret {
  display: inline-flex;
  align-items: center;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.scene-body {
  margin-top: 8px;
  /* 步骤体相对步骤头缩进一格（caret 12px + 间隙 8px 对齐到步骤标识下方），
     展开后「头 → 体」的从属一眼可读。 */
  margin-left: 20px;
}
/* 输入 / 输出分组头：整行可点，标签用色区分方向（蓝=进、绿=出）。 */
.scene-group {
  margin-top: 10px;
}
/* 分组体再往内缩一层，配一根 hairline 竖线：展开内容相对分组头的从属
   靠结构表达，不靠猜。 */
.scene-group-body {
  margin: 4px 0 2px 6px;
  padding-left: 15px;
  border-left: 1px solid var(--hairline);
}
.scene-group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.scene-group-tag {
  margin: 0;
}
.scene-line {
  display: flex;
  align-items: center;
  gap: 6px;
}
.scene-copy {
  display: inline-flex;
  padding: 0 2px;
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: color 0.15s;
}
.scene-copy:hover {
  color: var(--signal-text);
}
.scene-field {
  margin-top: 6px;
}
.scene-field-name {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-3);
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
}
.scene-field-tag {
  margin: 0;
  font-size: 10px;
  line-height: 14px;
  padding: 0 3px;
}
.scene-block {
  margin-top: 8px;
}
.scene-label {
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
