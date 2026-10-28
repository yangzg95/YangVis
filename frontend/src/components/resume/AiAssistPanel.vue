<template>
  <a-drawer
    :open="open"
    :title="title"
    placement="right"
    :width="500"
    :mask-closable="false"
    destroy-on-close
    @close="close"
  >
    <div class="assist-body">
      <a-alert v-if="error" type="error" show-icon :message="error" class="assist-alert">
        <template #action>
          <a-button size="small" type="primary" @click="start">重试</a-button>
        </template>
      </a-alert>

      <div v-if="streaming" class="streaming-hint">
        <LoadingOutlined /> 正在生成…
      </div>

      <a-empty
        v-if="!streaming && !output && !error"
        :image="simpleEmpty"
        description="还没有生成内容"
      />

      <div
        v-if="isCheck"
        class="markdown-body assist-output"
        v-html="renderedMarkdown"
      />
      <div v-else class="rich-output assist-output" v-html="sanitizedOutput" />
    </div>

    <template #footer>
      <div class="assist-footer">
        <a-button v-if="streaming" danger @click="stop">停止</a-button>
        <template v-else>
          <a-button @click="close">关闭</a-button>
          <template v-if="!isCheck">
            <a-button :disabled="!output" @click="apply('insert')">插入到光标处</a-button>
            <a-button :disabled="!output" @click="apply('append')">追加到末尾</a-button>
            <a-button type="primary" :disabled="!output" @click="apply('replace')">
              替换内容
            </a-button>
          </template>
        </template>
      </div>
    </template>
  </a-drawer>
</template>

<script setup lang="ts">
/**
 * 所有 AI 入口共用的流式面板：write/polish 累积 HTML 片段（消毒后预览），
 * check 累积 markdown（markdown-it html:false 安全渲染）。
 * 打开即开始流式请求；「停止」中断 fetch，「重试」重发。
 */
import { computed, ref, watch } from 'vue'
import { Empty, message } from 'ant-design-vue'
import { LoadingOutlined } from '@ant-design/icons-vue'
import {
  resumeBuilderApi,
  type ResumeAiAction,
} from '@/api/resumeBuilder'
import { MODULE_LABELS, type ResumeModuleKey } from '@/utils/resumeDoc'
import { sanitizeRichHtml } from '@/utils/resumeTemplate'
import { createMarkdown } from '@/utils/markdown'

const props = defineProps<{
  open: boolean
  docId: number
  module: ResumeModuleKey
  action: ResumeAiAction
  itemId?: string | null
  /** 润色原文：用户在编辑器里选中的文字。 */
  selection?: string | null
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  applied: [html: string, mode: 'replace' | 'append' | 'insert']
}>()

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const md = createMarkdown()

const streaming = ref(false)
const output = ref('')
const error = ref('')
let abortFn: (() => void) | null = null

const isCheck = computed(() => props.action === 'check')
const title = computed(() => {
  const label = MODULE_LABELS[props.module]
  if (props.action === 'check') return `智能检查 · 整份简历`
  if (props.action === 'polish') return `简历润色 · ${label}`
  return `AI 帮写 · ${label}`
})

const sanitizedOutput = computed(() => sanitizeRichHtml(output.value))
const renderedMarkdown = computed(() => md.render(output.value || ''))

watch(
  () => props.open,
  (open) => {
    if (open) start()
    else stop()
  },
)

function start() {
  stop(false)
  streaming.value = true
  output.value = ''
  error.value = ''
  abortFn = resumeBuilderApi.aiAssist(
    {
      doc_id: props.docId,
      module: props.module,
      action: props.action,
      item_id: props.itemId ?? null,
      selection: props.selection ?? null,
    },
    {
      onToken: (token: string) => {
        output.value += token
      },
      onDone: () => {
        streaming.value = false
      },
      onError: (data: { code: number; message: string }) => {
        streaming.value = false
        error.value = data?.message || '生成失败，请重试'
      },
    },
  )
}

function stop(markDone = true) {
  if (abortFn) {
    abortFn()
    abortFn = null
  }
  if (markDone) streaming.value = false
}

function close() {
  stop()
  emit('update:open', false)
}

function apply(mode: 'replace' | 'append' | 'insert') {
  if (!output.value) {
    message.warning('还没有可应用的内容')
    return
  }
  emit('applied', output.value, mode)
}
</script>

<style scoped>
.assist-body {
  min-height: 200px;
}
.assist-alert {
  margin-bottom: 12px;
}
.streaming-hint {
  color: var(--signal-text);
  font-size: 13px;
  margin-bottom: 10px;
}
.assist-output {
  font-size: 13.5px;
  line-height: 1.75;
  color: var(--text-1);
  word-break: break-word;
}
.rich-output p {
  margin: 0 0 6px;
}
.rich-output ul,
.rich-output ol {
  margin: 4px 0;
  padding-left: 22px;
}
.markdown-body h2 {
  font-size: 15px;
  margin: 14px 0 6px;
  color: var(--signal-text);
}
.markdown-body h2:first-child {
  margin-top: 0;
}
.markdown-body ul,
.markdown-body ol {
  padding-left: 20px;
}
.markdown-body li {
  margin-bottom: 4px;
}
.markdown-body code {
  background: var(--code-inline-bg);
  padding: 1px 4px;
  border-radius: 3px;
}
.assist-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
