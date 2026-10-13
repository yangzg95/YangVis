<template>
  <a-modal
    :open="open"
    :title="mode === 'rerun' ? '重跑这一步 · 调整输入' : '从此步继续 · 调整输入'"
    ok-text="开始"
    cancel-text="取消"
    :confirm-loading="submitting"
    :width="620"
    @ok="emit('confirm', { ...values })"
    @update:open="(value: boolean) => emit('update:open', value)"
  >
    <a-alert
      type="info"
      show-icon
      class="edit-modal-tip"
      :message="
        mode === 'rerun'
          ? '只重跑这一步：用下面的变量按当前模板重新渲染 prompt，实际 prompt 仍逐行落库；不写回业务结果。'
          : '将重跑本步所在并行组及之后所有步骤：上游组的产物不变，覆盖只影响这一次执行。'
      "
    />
    <a-form layout="vertical">
      <a-form-item v-for="field in fields" :key="field.name">
        <template #label>
          {{ field.name }}
          <a-tag v-if="field.isJson" color="blue">JSON</a-tag>
        </template>
        <a-textarea v-model:value="values[field.name]" :rows="4" />
      </a-form-item>
      <a-empty
        v-if="!fields.length"
        description="这一步没有可调整的上下文变量"
        :image-style="{ height: '40px' }"
      />
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import type { StepField } from './runFields'

const props = defineProps<{
  open: boolean
  mode: 'rerun' | 'resume'
  /** 这一步实际收到的上下文变量（抽屉与整页用同一份还原逻辑算出来）。 */
  fields: StepField[]
  submitting: boolean
}>()

const emit = defineEmits<{ 'update:open': [value: boolean]; confirm: [values: Record<string, string>] }>()

const values = reactive<Record<string, string>>({})

// 每打开一次都从当次现场还原初值：上一次改过又取消的，不该带进下一步。
watch(
  () => [props.open, props.fields] as const,
  ([open]) => {
    if (!open) return
    for (const key of Object.keys(values)) delete values[key]
    for (const field of props.fields) values[field.name] = field.value
  },
  { immediate: true },
)
</script>

<style scoped>
.edit-modal-tip {
  margin-bottom: 12px;
}
</style>
