<template>
  <EntryList
    :entries="entries"
    id-field="pid"
    add-label="添加项目经历"
    empty-hint="还没有项目经历，点下方按钮添加一条"
    @add="addEntry"
    @remove="removeEntry"
    @move="moveEntry"
  >
    <template #head="{ entry, index }">
      {{ entry.name || entry.role || `项目经历 ${index + 1}` }}
      <span v-if="entry.current" class="current-tag">至今</span>
    </template>
    <template #default="{ entry }">
      <a-row :gutter="16">
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>经历名称</label>
          <a-input v-model:value="entry.name" :maxlength="128" placeholder="项目名称" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>你的角色</label>
          <a-input v-model:value="entry.role" :maxlength="128" placeholder="如 项目负责人" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label">所在部门</label>
          <a-input v-model:value="entry.department" :maxlength="128" placeholder="所在部门" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>工作时间</label>
          <MonthRangeFields :model-value="entry" @change="emitChange" />
        </a-col>
      </a-row>

      <label class="field-label field-label--block">项目描述</label>
      <RichTextEditor
        :ref="editorRefFor(entry.pid)"
        v-model:html="entry.description_html"
        placeholder="背景、你的动作与可量化结果，也可以让 AI 帮写"
        :min-height="150"
        @update:html="emitChange"
      >
        <template #ai>
          <a-space :size="4">
            <a-button size="small" type="primary" ghost :disabled="!chatReady" @click="openAssist(entry, 'write')">
              <RobotOutlined /> 帮写
            </a-button>
            <a-button size="small" :disabled="!chatReady" @click="openAssist(entry, 'polish')">
              <HighlightOutlined /> 润色
            </a-button>
          </a-space>
        </template>
      </RichTextEditor>
    </template>
  </EntryList>

  <AiAssistPanel
    v-model:open="assist.open"
    :doc-id="docId"
    module="projects"
    :action="assist.action"
    :item-id="assist.itemId"
    :selection="assist.selection"
    @applied="onApplied"
  />
</template>

<script setup lang="ts">
/** 项目经历模块。 */
import { reactive } from 'vue'
import { HighlightOutlined, RobotOutlined } from '@ant-design/icons-vue'
import EntryList from './EntryList.vue'
import MonthRangeFields from './MonthRangeFields.vue'
import RichTextEditor from './RichTextEditor.vue'
import AiAssistPanel from './AiAssistPanel.vue'
import { createEntry, type ProjectEntry } from '@/utils/resumeDoc'
import type { ResumeAiAction } from '@/api/resumeBuilder'
import { sanitizeRichHtml } from '@/utils/resumeTemplate'

const props = defineProps<{
  entries: ProjectEntry[]
  docId: number
  chatReady: boolean
}>()

const emit = defineEmits<{ change: [] }>()

type EditorInstance = InstanceType<typeof RichTextEditor>
const editors = new Map<string, EditorInstance>()

// 模板函数 ref 的形参是 Element | 组件实例 | null：这里只可能挂到本组件上。
function registerEditor(id: string, el: unknown) {
  if (el) editors.set(id, el as EditorInstance)
  else editors.delete(id)
}

// 内联箭头每次渲染都是新函数，会让 Vue 对所有编辑器先解绑再重绑；按 id 缓存。
const editorRefFns = new Map<string, (el: unknown) => void>()
function editorRefFor(id: string) {
  let fn = editorRefFns.get(id)
  if (!fn) {
    fn = (el: unknown) => registerEditor(id, el)
    editorRefFns.set(id, fn)
  }
  return fn
}

const assist = reactive<{ open: boolean; action: ResumeAiAction; itemId: string; selection: string }>({
  open: false,
  action: 'write',
  itemId: '',
  selection: '',
})

function emitChange() {
  emit('change')
}

function addEntry() {
  props.entries.push(createEntry('projects') as ProjectEntry)
  emitChange()
}

function removeEntry(index: number) {
  props.entries.splice(index, 1)
  emitChange()
}

function moveEntry(index: number, dir: -1 | 1) {
  const target = index + dir
  if (target < 0 || target >= props.entries.length) return
  const [item] = props.entries.splice(index, 1)
  props.entries.splice(target, 0, item)
  emitChange()
}

function openAssist(entry: ProjectEntry, action: ResumeAiAction) {
  assist.itemId = entry.pid
  assist.action = action
  assist.selection =
    action === 'polish' ? editors.get(entry.pid)?.getSelectionText() ?? '' : ''
  assist.open = true
}

function onApplied(html: string, mode: 'replace' | 'append' | 'insert') {
  const entry = props.entries.find((item) => item.pid === assist.itemId)
  if (!entry) return
  const clean = sanitizeRichHtml(html)
  if (mode === 'replace') entry.description_html = clean
  else if (mode === 'append') entry.description_html += clean
  else editors.get(entry.pid)?.insertHtml(clean)
  assist.open = false
  emitChange()
}
</script>

<style scoped>
.field-label {
  display: block;
  margin: 10px 0 6px;
  font-size: 13px;
  color: var(--text-2);
}
.field-label--block {
  margin-top: 14px;
}
.required {
  color: var(--danger);
  margin-right: 2px;
}
.current-tag {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: var(--signal-text);
}
</style>
