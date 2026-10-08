<template>
  <div>
    <div v-for="(row, index) in rows" :key="index" class="secret-row">
      <a-input
        v-model:value="row.key"
        class="secret-key"
        :placeholder="keyPlaceholder"
      />
      <a-input-password
        v-model:value="row.value"
        class="secret-value"
        placeholder="值"
      />
      <a-button type="text" danger size="small" @click="removeRow(index)">
        <DeleteOutlined />
      </a-button>
    </div>
    <a-button type="dashed" size="small" block @click="addRow">
      <PlusOutlined /> 添加
    </a-button>
    <div class="hint secret-hint">
      值保存后显示为掩码；保持掩码不变即不改，清空即删除该键。
    </div>
  </div>
</template>

<script setup lang="ts">
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons-vue'

interface SecretRow {
  key: string
  value: string
  existed: boolean
}

const props = withDefaults(
  defineProps<{
    rows: SecretRow[]
    keyPlaceholder?: string
  }>(),
  { keyPlaceholder: '名称' },
)
const emit = defineEmits<{ 'update:rows': [SecretRow[]] }>()

function emitRows(next: SecretRow[]) {
  emit('update:rows', next)
}
function addRow() {
  emitRows([...props.rows, { key: '', value: '', existed: false }])
}
function removeRow(index: number) {
  emitRows(props.rows.filter((_, i) => i !== index))
}
</script>

<style scoped>
.secret-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.secret-key {
  flex: 0 0 38%;
}
.secret-value {
  flex: 1;
}
.secret-hint {
  margin-top: 6px;
}
</style>
