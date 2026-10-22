<template>
  <EntryList
    :entries="entries"
    id-field="eid"
    add-label="添加教育经历"
    empty-hint="还没有教育经历，点下方按钮添加一条"
    @add="addEntry"
    @remove="removeEntry"
    @move="moveEntry"
  >
    <template #head="{ entry, index }">
      {{ entry.school || entry.major || `教育经历 ${index + 1}` }}
      <span v-if="entry.degree" class="degree-tag">{{ entry.degree }}</span>
    </template>
    <template #default="{ entry }">
      <a-row :gutter="16">
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>学校名称</label>
          <a-input v-model:value="entry.school" :maxlength="128" placeholder="学校名称" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label">专业</label>
          <a-input v-model:value="entry.major" :maxlength="128" placeholder="专业" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label">学院</label>
          <a-input v-model:value="entry.college" :maxlength="128" placeholder="学院" @change="emitChange" />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>学历</label>
          <a-select
            v-model:value="entry.degree"
            class="field-control"
            placeholder="选择学历"
            allow-clear
            :options="degreeOptions"
            @change="emitChange"
          />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label">就读形式</label>
          <a-select
            v-model:value="entry.study_mode"
            class="field-control"
            placeholder="全日制 / 非全日制"
            allow-clear
            :options="studyModeOptions"
            @change="emitChange"
          />
        </a-col>
        <a-col :xs="24" :sm="12">
          <label class="field-label"><span class="required">*</span>在读时间</label>
          <MonthRangeFields :model-value="entry" @change="emitChange" />
        </a-col>
      </a-row>

      <label class="field-label field-label--block">在校经历</label>
      <RichTextEditor
        :ref="editorRefFor(entry.eid)"
        v-model:html="entry.description_html"
        placeholder="成绩、荣誉、主修课程等，也可以让 AI 帮写"
        :min-height="130"
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
    module="educations"
    :action="assist.action"
    :item-id="assist.itemId"
    :selection="assist.selection"
    @applied="onApplied"
  />
</template>

<script setup lang="ts">
/** 学校/教育模块：额外有学历与就读形式两个白名单下拉。 */
import { reactive } from 'vue'
import { HighlightOutlined, RobotOutlined } from '@ant-design/icons-vue'
import EntryList from './EntryList.vue'
import MonthRangeFields from './MonthRangeFields.vue'
import RichTextEditor from './RichTextEditor.vue'
import AiAssistPanel from './AiAssistPanel.vue'
import {
  DEGREE_OPTIONS,
  STUDY_MODE_OPTIONS,
  createEntry,
  type EducationEntry,
} from '@/utils/resumeDoc'
import type { ResumeAiAction } from '@/api/resumeBuilder'
import { sanitizeRichHtml } from '@/utils/resumeTemplate'

const props = defineProps<{
  entries: EducationEntry[]
  docId: number
  chatReady: boolean
}>()

const emit = defineEmits<{ change: [] }>()

const degreeOptions = DEGREE_OPTIONS.map((value) => ({ value, label: value }))
const studyModeOptions = STUDY_MODE_OPTIONS.map((value) => ({ value, label: value }))

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
  props.entries.push(createEntry('educations') as EducationEntry)
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

function openAssist(entry: EducationEntry, action: ResumeAiAction) {
  assist.itemId = entry.eid
  assist.action = action
  assist.selection =
    action === 'polish' ? editors.get(entry.eid)?.getSelectionText() ?? '' : ''
  assist.open = true
}

function onApplied(html: string, mode: 'replace' | 'append' | 'insert') {
  const entry = props.entries.find((item) => item.eid === assist.itemId)
  if (!entry) return
  const clean = sanitizeRichHtml(html)
  if (mode === 'replace') entry.description_html = clean
  else if (mode === 'append') entry.description_html += clean
  else editors.get(entry.eid)?.insertHtml(clean)
  assist.open = false
  emitChange()
}
</script>

<style scoped>
/* a-select 根节点是 inline-block 且不带宽度，不给 100% 就按内容塌窄；
   下拉面板默认与选择框等宽，塌到几十字宽时选项会一字一行竖排。 */
.field-control {
  width: 100%;
}
.field-label {
  display: block;
  margin: 10px 0 6px;
  font-size: 13px;
  color: #4b5563;
}
.field-label--block {
  margin-top: 14px;
}
.required {
  color: #ff4d4f;
  margin-right: 2px;
}
.degree-tag {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #0a7e86;
}
</style>
