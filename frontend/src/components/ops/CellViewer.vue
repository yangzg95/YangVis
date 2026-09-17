<template>
  <a-modal
    :open="open"
    :title="column ? `单元格内容 · ${column}` : '单元格内容'"
    :footer="null"
    width="720px"
    @update:open="emit('update:open', $event)"
  >
    <pre class="viewer-text">{{ text }}</pre>
    <div class="viewer-actions">
      <span class="viewer-meta">{{ text.length }} 字符</span>
      <a-button size="small" type="primary" @click="copy">复制</a-button>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
// 长内容（JSON、长文本）在表格里被列宽截断，点单元格上的「展开」按钮
// 在这里看全文，弹窗内也能一键复制。
import { message as toast } from 'ant-design-vue'
import { copyText } from './grid'

const props = defineProps<{
  open: boolean
  column: string
  text: string
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
}>()

async function copy() {
  if (await copyText(props.text)) toast.success('已复制')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}
</script>

<style scoped>
.viewer-text {
  max-height: 50vh;
  margin: 0;
  padding: 10px 12px;
  overflow: auto;
  border-radius: var(--radius-md, 6px);
  background: rgba(17, 24, 31, 0.03);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.viewer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.viewer-meta {
  color: var(--text-3);
  font-size: 12px;
}
</style>
