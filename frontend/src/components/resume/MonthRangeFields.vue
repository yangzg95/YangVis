<template>
  <div class="month-range">
    <a-date-picker
      :value="modelValue.start"
      picker="month"
      format="YYYY-MM"
      valueFormat="YYYY-MM"
      placeholder="开始月份"
      class="month-picker"
      @update:value="(v: string) => setField('start', v)"
    />
    <span class="dash">—</span>
    <a-date-picker
      :value="modelValue.end"
      picker="month"
      format="YYYY-MM"
      valueFormat="YYYY-MM"
      placeholder="结束月份"
      class="month-picker"
      :disabled="modelValue.current"
      @update:value="(v: string) => setField('end', v)"
    />
    <a-checkbox :checked="modelValue.current" @change="onCurrent">至今</a-checkbox>
  </div>
</template>

<script setup lang="ts">
/** 月份区间（YYYY-MM）+「至今」：勾选至今时禁用并清空结束月。 */
import type { ResumeTimedEntry } from '@/utils/resumeDoc'

const props = defineProps<{ modelValue: ResumeTimedEntry }>()
const emit = defineEmits<{ 'update:modelValue': [value: ResumeTimedEntry]; change: [] }>()

function setField(field: 'start' | 'end', value: string | null) {
  props.modelValue[field] = value ?? ''
  emit('update:modelValue', props.modelValue)
  emit('change')
}

function onCurrent(event: { target: { checked: boolean } }) {
  props.modelValue.current = event.target.checked
  if (event.target.checked) props.modelValue.end = ''
  emit('update:modelValue', props.modelValue)
  emit('change')
}
</script>

<style scoped>
.month-range {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.month-picker {
  flex: 1;
  min-width: 120px;
}
.dash {
  color: #999;
}
</style>
