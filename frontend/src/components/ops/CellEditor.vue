<template>
  <a-modal
    :open="open"
    :title="column ? `编辑单元格 · ${column}` : '编辑单元格'"
    ok-text="保存"
    cancel-text="取消"
    width="720px"
    :confirm-loading="saving"
    @update:open="emit('update:open', $event)"
    @ok="save"
  >
    <a-textarea
      v-model:value="draft"
      :auto-size="{ minRows: 6, maxRows: 16 }"
      :disabled="asNull"
      placeholder="输入新值；NULL / 空白字符串请用下方选项或右键菜单"
    />
    <div class="editor-footer">
      <a-checkbox v-model:checked="asNull">设为 NULL</a-checkbox>
      <span class="editor-meta">{{ asNull ? '保存后该单元格为 NULL' : `${draft.length} 字符` }}</span>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
// 长内容（JSON、长文本）不适合行内编辑框，右键「在单元格编辑器中编辑」
// 打开这个弹窗改全文；「设为 NULL」勾选后输入框置灰，保存提交 null。
import { ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  column: string
  /** 打开那一刻的快照；null 表示单元格当前是 NULL。 */
  text: string | null
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'save', value: string | null): void
}>()

const draft = ref('')
const asNull = ref(false)

// 每次打开都从快照重置，上次的草稿不能带进下一个单元格。
watch(
  () => props.open,
  (open) => {
    if (open) {
      draft.value = props.text ?? ''
      asNull.value = props.text === null
    }
  },
)

function save() {
  emit('save', asNull.value ? null : draft.value)
}
</script>

<style scoped>
.editor-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.editor-meta {
  color: var(--text-3);
  font-size: 12px;
}
</style>
