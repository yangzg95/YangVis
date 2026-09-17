<template>
  <div class="tab-page">
    <div class="toolbar">
      <span class="crumb">
        <FolderOutlined /> {{ schema }} <span class="sep">/</span>
        <TableOutlined /> {{ table }}
      </span>
      <span class="spacer" />
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-table
      class="grid db-grid"
      size="small"
      :data-source="columns"
      :columns="GRID_COLUMNS"
      :loading="loading"
      :pagination="false"
      :scroll="{ x: 'max-content', y: 480 }"
      row-key="name"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <span class="col-name">{{ record.name }}</span>
          <a-tag v-if="record.column_key === 'PRI'" color="gold" class="key-tag">PK</a-tag>
          <a-tag v-else-if="record.column_key" class="key-tag">{{ record.column_key }}</a-tag>
        </template>
        <template v-else-if="column.key === 'nullable'">
          {{ record.nullable ? 'YES' : 'NO' }}
        </template>
        <template v-else-if="column.key === 'default'">
          <span :class="{ 'is-null': record.default == null }">
            {{ record.default == null ? 'NULL' : record.default }}
          </span>
        </template>
      </template>
    </a-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { FolderOutlined, ReloadOutlined, TableOutlined } from '@ant-design/icons-vue'
import { message as toast } from 'ant-design-vue'
import { opsApi, type DbColumnItem } from '@/api'
import './grid.css'

const props = defineProps<{
  connId: number
  schema: string
  table: string
}>()

const GRID_COLUMNS = [
  { title: '列名', key: 'name', dataIndex: 'name' },
  { title: '类型', key: 'column_type', dataIndex: 'column_type' },
  { title: '可空', key: 'nullable', dataIndex: 'nullable', width: 70 },
  { title: '默认值', key: 'default', dataIndex: 'default' },
  { title: 'Extra', key: 'extra', dataIndex: 'extra' },
  { title: '注释', key: 'comment', dataIndex: 'comment', ellipsis: true },
]

const columns = ref<DbColumnItem[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const res = await opsApi.dbColumns(props.connId, props.schema, props.table)
    columns.value = res.items
  } catch (err: any) {
    toast.error(err?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.tab-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* flex 基底用全局 .toolbar（style.css），ops 面板内保持更紧凑的间距。 */
.toolbar {
  gap: 8px;
  margin-bottom: 0;
  padding-bottom: 8px;
}

.crumb {
  font-size: 13px;
  color: rgba(0, 0, 0, 0.65);
}

.sep {
  margin: 0 4px;
  color: rgba(0, 0, 0, 0.25);
}

.spacer {
  flex: 1;
}

.grid {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.col-name {
  font-weight: 500;
  margin-right: 6px;
}

.key-tag {
  font-size: 11px;
  line-height: 16px;
  padding: 0 4px;
}

.is-null {
  color: var(--text-3);
  font-style: italic;
}
</style>
