<template>
  <!-- 上传区：回形针常驻，待发附件以缩略图/文件名条展示在上方。
       图片能否发给当前模型由 gate 在前端先拦一道。 -->
  <div class="attach-bar">
    <a-tooltip :title="`上传文件或图片（最多 ${max} 个）`">
      <button
        type="button"
        class="attach-btn"
        :disabled="!canSend || pending.length >= max"
        @click="openFilePicker"
      >
        <PaperClipOutlined />
      </button>
    </a-tooltip>
    <span class="attach-count" v-if="pending.length">{{ pending.length }}/{{ max }}</span>
    <span v-if="gate" class="attach-warn">{{ gate }}</span>
  </div>
  <input
    ref="fileInput"
    type="file"
    multiple
    class="file-input"
    accept=".pdf,.docx,.txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.log,.yaml,.yml,.xml,.toml,.ini,.conf,.properties,.env,.sql,.py,.js,.mjs,.cjs,.ts,.tsx,.jsx,.vue,.java,.kt,.go,.rs,.c,.h,.cpp,.hpp,.cs,.php,.rb,.sh,.bash,.zsh,.bat,.ps1,.html,.htm,.css,.scss,.less,.png,.jpg,.jpeg,.gif,.webp,.bmp"
    @change="onFilesPicked"
  />
  <div v-if="pending.length" class="pending-list">
    <div
      v-for="att in pending"
      :key="att.key"
      :class="['pending-item', { 'is-error': att.status === 'error' }]"
    >
      <img
        v-if="att.kind === 'image' && att.previewUrl"
        :src="att.previewUrl"
        class="pending-thumb"
        alt=""
      />
      <span v-else class="pending-file-icon"><FileOutlined /></span>
      <span class="pending-meta">
        <span class="pending-name" :title="att.filename">{{ att.filename }}</span>
        <span class="pending-sub">
          <a-spin v-if="att.status === 'uploading'" size="small" />
          <template v-else-if="att.status === 'error'">
            {{ att.error || '上传失败' }}
          </template>
          <template v-else>{{ formatSize(att.size) }}</template>
        </span>
      </span>
      <button type="button" class="pending-remove" title="移除" @click="emit('remove', att)">
        <CloseOutlined />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { CloseOutlined, FileOutlined, PaperClipOutlined } from '@ant-design/icons-vue'

/** 待发附件的展示结构：与页面的 PendingAttachment 兼容，组件只读不改。 */
interface PendingItem {
  key: string
  kind: 'image' | 'file'
  filename: string
  mime: string | null
  size: number
  /** 图片预览地址：上传中为 blob URL，成功后换成服务端 URL。 */
  previewUrl?: string
  status: 'uploading' | 'ready' | 'error'
  error?: string
}

const props = defineProps<{
  pending: PendingItem[]
  canSend: boolean
  max: number
  /** 附件区的一句话状态提示：为空表示当前这批可以随消息发出。 */
  gate: string
}>()

const emit = defineEmits<{
  files: [files: File[]]
  remove: [item: PendingItem]
}>()

const fileInput = ref<HTMLInputElement | null>(null)

function openFilePicker() {
  fileInput.value?.click()
}

function onFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  // 清空 value：同一个文件连选两次也要能再次触发 change。
  input.value = ''
  if (files.length) emit('files', files)
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}
</script>

<style scoped>
/* 上传入口：常驻在输入框上方一行。就一个回形针，不抢视觉重量。 */
.attach-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 24px;
  margin-bottom: 4px;
}
.attach-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-base);
  cursor: pointer;
  transition: all 0.15s;
}
.attach-btn:hover:not(:disabled) {
  background: var(--hover-neutral);
  color: var(--signal-text);
}
.attach-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.attach-count {
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.attach-warn {
  font-size: var(--fs-xs);
  color: var(--warning);
}
.file-input {
  display: none;
}
/* 待发附件条：缩略图 + 文件名 + 大小/进度，移除按钮固定在行尾。 */
.pending-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.pending-item {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 260px;
  padding: 4px 6px 4px 4px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
}
.pending-item.is-error {
  border-color: var(--danger-soft);
  background: var(--danger-bg);
}
.pending-thumb {
  display: block;
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: 4px;
}
.pending-file-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: 4px;
  background: var(--paper);
  color: var(--text-3);
  font-size: var(--fs-lg);
}
.pending-meta {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.pending-name {
  overflow: hidden;
  font-size: var(--fs-xs);
  color: var(--text-1);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pending-sub {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.pending-item.is-error .pending-sub {
  color: var(--danger);
}
.pending-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
}
.pending-remove:hover {
  background: var(--hover-neutral);
  color: var(--danger);
}
</style>
