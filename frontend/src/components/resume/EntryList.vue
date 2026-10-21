<template>
  <div class="entry-list">
    <div v-for="(entry, index) in entries" :key="entry[idField] || index" class="entry-card">
      <div class="entry-head">
        <span class="head-slot">
          <slot name="head" :entry="entry" :index="index" />
        </span>
        <span class="entry-ops">
          <a-button size="small" type="text" title="上移" :disabled="index === 0" @click="emit('move', index, -1)">
            <ArrowUpOutlined />
          </a-button>
          <a-button
            size="small"
            type="text"
            title="下移"
            :disabled="index === entries.length - 1"
            @click="emit('move', index, 1)"
          >
            <ArrowDownOutlined />
          </a-button>
          <a-popconfirm title="删除这一条？" ok-text="删除" cancel-text="取消" @confirm="emit('remove', index)">
            <a-button size="small" type="text" danger title="删除">
              <DeleteOutlined />
            </a-button>
          </a-popconfirm>
        </span>
      </div>
      <div class="entry-body">
        <slot :entry="entry" :index="index" />
      </div>
    </div>
    <a-empty
      v-if="entries.length === 0"
      :image="simpleEmpty"
      :description="emptyHint"
      class="entry-empty"
    />
    <a-button type="dashed" block class="add-btn" @click="emit('add')">
      <PlusOutlined /> {{ addLabel }}
    </a-button>
  </div>
</template>

<script setup lang="ts" generic="T extends { cid?: string; pid?: string; eid?: string }">
/** 三类经历共用的条目卡片容器：头部插槽放标题、默认插槽放表单字段。 */
import { Empty } from 'ant-design-vue'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'

defineProps<{
  entries: T[]
  idField: 'cid' | 'pid' | 'eid'
  addLabel: string
  emptyHint: string
}>()

const emit = defineEmits<{
  add: []
  remove: [index: number]
  move: [index: number, dir: -1 | 1]
}>()

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
</script>

<style scoped>
.entry-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.entry-card {
  border: 1px solid #ebedf0;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
}
.entry-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.head-slot {
  font-size: 13.5px;
  font-weight: 600;
  color: #1f2329;
  min-width: 0;
}
.entry-ops {
  flex-shrink: 0;
}
.add-btn {
  margin-top: 2px;
}
.entry-empty {
  margin: 4px 0 -8px;
}
.entry-empty :deep(.ant-empty-image) {
  margin-bottom: 4px;
}
</style>
