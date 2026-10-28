<template>
  <div class="module-nav">
    <div class="nav-title">简历模块</div>
    <ul class="nav-list">
      <li
        v-for="(key, index) in modelValue"
        :key="key"
        :class="['nav-item', { active: key === activeKey, dragging: index === dragIndex, over: index === overIndex }]"
        draggable="true"
        @click="$emit('update:activeKey', key)"
        @dragstart="onDragStart(index, $event)"
        @dragover.prevent="onDragOver(index)"
        @dragleave="overIndex = -1"
        @drop.prevent="onDrop(index)"
        @dragend="dragIndex = -1"
      >
        <HolderOutlined class="drag-handle" />
        <span class="nav-label">{{ MODULE_LABELS[key] }}</span>
        <a-tag v-if="counts[key]" color="default" class="nav-count">{{ counts[key] }}</a-tag>
        <a-button
          v-if="isEntryKey(key)"
          size="small"
          type="text"
          class="nav-add"
          title="添加一条"
          @click.stop="emitAdd(key)"
        >
          <PlusOutlined />
        </a-button>
      </li>
    </ul>
    <div class="nav-hint">
      <HolderOutlined /> 拖动可调整模块在简历中的顺序
    </div>
  </div>
</template>

<script setup lang="ts">
/** 五个模块的导航与排序：原生 HTML5 拖拽重排 module_order，零依赖。 */
import { ref } from 'vue'
import { HolderOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  ENTRY_MODULE_KEYS,
  MODULE_LABELS,
  type EntryModuleKey,
  type ResumeModuleKey,
} from '@/utils/resumeDoc'

const props = defineProps<{
  modelValue: ResumeModuleKey[]
  activeKey: ResumeModuleKey
  /** 三类经历模块的条目数，其余键给 0。 */
  counts: Partial<Record<ResumeModuleKey, number>>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: ResumeModuleKey[]]
  'update:activeKey': [value: ResumeModuleKey]
  add: [key: EntryModuleKey]
}>()

const dragIndex = ref(-1)
const overIndex = ref(-1)

function isEntryKey(key: ResumeModuleKey): key is EntryModuleKey {
  return (ENTRY_MODULE_KEYS as ResumeModuleKey[]).includes(key)
}

function emitAdd(key: ResumeModuleKey) {
  if (isEntryKey(key)) emit('add', key)
}

function onDragStart(index: number, event: DragEvent) {
  dragIndex.value = index
  event.dataTransfer?.setData('text/plain', String(index))
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

function onDragOver(index: number) {
  overIndex.value = index
}

function onDrop(index: number) {
  const from = dragIndex.value
  overIndex.value = -1
  dragIndex.value = -1
  if (from < 0 || from === index) return
  // v-model 语义：发新数组，不在 prop 上原地改。
  const next = [...props.modelValue]
  const [moved] = next.splice(from, 1)
  next.splice(index, 0, moved)
  emit('update:modelValue', next)
}
</script>

<style scoped>
.module-nav {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.nav-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
}
.nav-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13.5px;
  color: var(--text-1);
  user-select: none;
}
.nav-item:hover {
  background: var(--hover-neutral);
}
.nav-item.active {
  background: var(--signal-bg);
  border-color: var(--signal-border);
  color: var(--signal-text);
  font-weight: 600;
}
.nav-item.dragging {
  opacity: 0.5;
}
.nav-item.over {
  border-color: var(--signal-text);
}
.drag-handle {
  color: var(--text-3);
  cursor: grab;
}
.nav-label {
  flex: 1;
}
.nav-count {
  margin: 0;
}
.nav-add {
  visibility: hidden;
  padding: 0 4px;
  height: 22px;
}
.nav-item:hover .nav-add,
.nav-item.active .nav-add {
  visibility: visible;
}
.nav-hint {
  font-size: 12px;
  color: var(--text-3);
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
