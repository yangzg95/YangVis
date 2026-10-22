<template>
  <div class="module-form">
    <a-row :gutter="16">
      <a-col :span="12">
        <label class="field-label"><span class="required">*</span>姓名</label>
        <a-input v-model:value="profile.name" :maxlength="128" placeholder="姓名" @change="emitChange" />
      </a-col>
      <a-col :span="12">
        <label class="field-label">性别</label>
        <a-select
          :value="profile.gender || undefined"
          placeholder="选择性别"
          allow-clear
          :options="genderOptions"
          @update:value="onGenderChange"
        />
      </a-col>
      <a-col :span="12">
        <label class="field-label">出生年月</label>
        <a-date-picker
          :value="profile.birthday || undefined"
          picker="month"
          format="YYYY-MM"
          valueFormat="YYYY-MM"
          placeholder="如 1995-06"
          style="width: 100%"
          @update:value="onBirthdayChange"
        />
      </a-col>
      <a-col :span="12">
        <label class="field-label">所在城市</label>
        <a-input v-model:value="profile.city" :maxlength="64" placeholder="如 上海" @change="emitChange" />
      </a-col>
      <a-col :span="12">
        <label class="field-label">手机号码</label>
        <a-input v-model:value="profile.phone" :maxlength="32" placeholder="手机号码" @change="emitChange" />
      </a-col>
      <a-col :span="12">
        <label class="field-label">邮箱</label>
        <a-input v-model:value="profile.email" :maxlength="128" placeholder="邮箱" @change="emitChange" />
      </a-col>
      <a-col :span="12">
        <label class="field-label">个人网站</label>
        <a-input
          v-model:value="profile.website"
          :maxlength="200"
          placeholder="如 me.dev 或 https://me.dev"
          @change="emitChange"
        />
      </a-col>
      <a-col :span="24">
        <label class="field-label">求职意向</label>
        <a-input v-model:value="profile.job_target" :maxlength="128" placeholder="如 后端工程师" @change="emitChange" />
      </a-col>
    </a-row>

    <label class="field-label field-label--block">
      个人介绍
      <span class="field-hint">一句话招牌，展示在简历抬头；详细卖点写下方个人优势</span>
    </label>
    <a-textarea
      v-model:value="profile.intro"
      :rows="2"
      :maxlength="300"
      show-count
      placeholder="如：8 年 Java 后端，主导过日均亿级订单系统，专注高并发与分布式架构"
      @change="emitChange"
    />

    <label class="field-label field-label--block">个人优势</label>
    <RichTextEditor
      ref="editorRef"
      v-model:html="profile.summary_html"
      placeholder="提炼经验年限、核心能力与成果，也可以让 AI 帮写"
      :min-height="180"
      @update:html="emitChange"
    >
      <template #ai>
        <a-button size="small" type="primary" ghost :disabled="!chatReady" @click="openWrite">
          <RobotOutlined /> AI 帮写
        </a-button>
      </template>
    </RichTextEditor>

    <AiAssistPanel
      v-model:open="assist.open"
      :doc-id="docId"
      module="profile"
      :action="assist.action"
      @applied="onApplied"
    />
  </div>
</template>

<script setup lang="ts">
/** 个人信息：基本字段网格 + 个人优势富文本（write/polish 共用一个 AI 面板）。 */
import { reactive, ref } from 'vue'
import { RobotOutlined } from '@ant-design/icons-vue'
import RichTextEditor from './RichTextEditor.vue'
import AiAssistPanel from './AiAssistPanel.vue'
import type { ResumeAiAction } from '@/api/resumeBuilder'
import type { ResumeProfile } from '@/utils/resumeDoc'

const props = defineProps<{
  profile: ResumeProfile
  docId: number
  chatReady: boolean
}>()

const emit = defineEmits<{ change: [] }>()

const editorRef = ref<InstanceType<typeof RichTextEditor> | null>(null)
const assist = reactive<{ open: boolean; action: ResumeAiAction; selection: string }>({
  open: false,
  action: 'write',
  selection: '',
})

const genderOptions = [
  { value: '男', label: '男' },
  { value: '女', label: '女' },
]

function emitChange() {
  emit('change')
}

function onGenderChange(value: string | undefined) {
  props.profile.gender = value ?? ''
  emitChange()
}

function onBirthdayChange(value: string | null) {
  props.profile.birthday = value ?? ''
  emitChange()
}

function openWrite() {
  assist.selection = editorRef.value?.getSelectionText() ?? ''
  assist.action = 'write'
  assist.open = true
}

function onApplied(html: string, mode: 'replace' | 'append' | 'insert') {
  if (mode === 'replace') props.profile.summary_html = html
  else if (mode === 'append') props.profile.summary_html += html
  else editorRef.value?.insertHtml(html)
  assist.open = false
  emitChange()
}
</script>

<style scoped>
.module-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field-label {
  display: block;
  margin: 12px 0 6px;
  font-size: 13px;
  color: #4b5563;
}
.field-label--block {
  margin-top: 18px;
}
.required {
  color: #ff4d4f;
  margin-right: 2px;
}
.field-hint {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #9ca3af;
}
</style>
