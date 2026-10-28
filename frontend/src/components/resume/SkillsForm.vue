<template>
  <div class="module-form">
    <div class="skills-actions">
      <a-space :size="8" wrap>
        <a-button type="primary" ghost :disabled="!chatReady" @click="openAssist('write')">
          <RobotOutlined /> AI 帮写
        </a-button>
        <a-button :disabled="!chatReady" @click="openAssist('polish')">
          <HighlightOutlined /> 简历润色
        </a-button>
        <a-button type="primary" :disabled="!chatReady" @click="openAssist('check')">
          <SafetyCertificateOutlined /> 智能检查
        </a-button>
      </a-space>
      <span class="skills-hint">建议用无序列表，每条「技能点：掌握程度 / 应用场景」</span>
    </div>

    <RichTextEditor
      ref="editorRef"
      :html="skillsHtml"
      placeholder="例如：Python：5 年经验，主导过后端高并发服务设计"
      :min-height="320"
      @update:html="onEditorInput"
    />

    <AiAssistPanel
      v-model:open="assist.open"
      :doc-id="docId"
      module="skills"
      :action="assist.action"
      :selection="assist.selection"
      @applied="onApplied"
    />
  </div>
</template>

<script setup lang="ts">
/** 专业技能：单个富文本 + 三个 AI 入口（帮写/润色作用于本模块，检查针对整份简历）。 */
import { reactive, ref } from 'vue'
import {
  HighlightOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons-vue'
import RichTextEditor from './RichTextEditor.vue'
import AiAssistPanel from './AiAssistPanel.vue'
import type { ResumeAiAction } from '@/api/resumeBuilder'

const props = defineProps<{
  skillsHtml: string
  docId: number
  chatReady: boolean
}>()

const emit = defineEmits<{
  'update:skillsHtml': [value: string]
  change: []
}>()

const editorRef = ref<InstanceType<typeof RichTextEditor> | null>(null)
const assist = reactive<{ open: boolean; action: ResumeAiAction; selection: string }>({
  open: false,
  action: 'write',
  selection: '',
})

function onEditorInput(value: string) {
  // skillsHtml 是 prop，不能直接 v-model；内容变化向父组件回发。
  emit('update:skillsHtml', value)
  emit('change')
}

function emitChange() {
  emit('change')
}

function openAssist(action: ResumeAiAction) {
  assist.action = action
  // 润色优先用选中的文字；没选中就由后端取现有全文。
  assist.selection = action === 'polish' ? editorRef.value?.getSelectionText() ?? '' : ''
  assist.open = true
}

function onApplied(html: string, mode: 'replace' | 'append' | 'insert') {
  let next = html
  if (mode === 'append') next = (props.skillsHtml || '') + html
  if (mode !== 'insert') emit('update:skillsHtml', next)
  else editorRef.value?.insertHtml(html)
  assist.open = false
  emitChange()
}
</script>

<style scoped>
.skills-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.skills-hint {
  font-size: 12px;
  color: var(--text-3);
}
</style>
