<template>
  <div class="tab-page">
    <div class="toolbar">
      <KeyOutlined class="icon" />
      <a-tooltip :title="detail?.key || rkey">
        <span class="key-name">{{ rkey }}</span>
      </a-tooltip>
      <template v-if="detail">
        <a-tag :color="TYPE_COLORS[detail.key_type] || 'default'">{{ detail.key_type }}</a-tag>
        <span class="ttl">{{ ttlText }}</span>
      </template>
      <span class="spacer" />
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-alert
      v-if="detail?.truncated"
      class="note"
      type="warning"
      show-icon
      message="集合较大，只显示了前一部分元素。"
    />

    <a-spin :spinning="loading" class="body">
      <a-empty v-if="detail?.key_type === 'none'" description="key 不存在或已过期" />

      <template v-else-if="detail">
        <!-- string：原文展示 -->
        <pre v-if="detail.key_type === 'string'" class="string-value">{{ detail.value }}</pre>

        <!-- list / set：单列，带序号 -->
        <a-table
          v-else-if="detail.key_type === 'list' || detail.key_type === 'set'"
          class="db-grid"
          size="small"
          :data-source="listRows"
          :columns="listColumns"
          :pagination="false"
          :scroll="{ x: 'max-content', y: 440 }"
          row-key="__i"
        />

        <!-- hash / zset / stream：两列 -->
        <a-table
          v-else
          class="db-grid"
          size="small"
          :data-source="pairRows"
          :columns="pairColumns"
          :pagination="false"
          :scroll="{ x: 'max-content', y: 440 }"
          row-key="__i"
        />
      </template>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { KeyOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { message as toast } from 'ant-design-vue'
import { opsApi, type RedisKeyDetail } from '@/api'
import './grid.css'

const props = defineProps<{
  connId: number
  db: number
  rkey: string
}>()

const TYPE_COLORS: Record<string, string> = {
  string: 'blue',
  list: 'green',
  set: 'orange',
  zset: 'purple',
  hash: 'cyan',
  stream: 'magenta',
}

const PAIR_HEADERS: Record<string, [string, string]> = {
  hash: ['field', 'value'],
  zset: ['member', 'score'],
  stream: ['id', 'fields'],
}

const detail = ref<RedisKeyDetail | null>(null)
const loading = ref(false)

const ttlText = computed(() => {
  const ttl = detail.value?.ttl
  if (ttl == null || ttl === -1) return '永不过期'
  if (ttl === -2) return ''
  return `TTL ${ttl}s`
})

const listRows = computed(() => {
  const values = (detail.value?.value as string[]) || []
  return values.map((v, i) => ({ __i: i, index: i, value: v }))
})

const listColumns = [
  { title: '#', dataIndex: 'index', key: 'index', width: 60 },
  { title: 'value', dataIndex: 'value', key: 'value', ellipsis: true },
]

const pairRows = computed(() => {
  const values = (detail.value?.value as [string, string][]) || []
  return values.map(([a, b], i) => ({ __i: i, a, b }))
})

const pairColumns = computed(() => {
  const [ha, hb] = PAIR_HEADERS[detail.value?.key_type || ''] || ['field', 'value']
  return [
    { title: ha, dataIndex: 'a', key: 'a', ellipsis: true },
    { title: hb, dataIndex: 'b', key: 'b', ellipsis: true },
  ]
})

async function load() {
  loading.value = true
  try {
    detail.value = await opsApi.redisKeyDetail(props.connId, props.db, props.rkey)
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

.icon {
  color: var(--text-3);
}

.key-name {
  max-width: 380px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 13px;
}

.ttl {
  color: var(--text-3);
  font-size: 12px;
}

.spacer {
  flex: 1;
}

.note {
  margin-bottom: 8px;
}

.body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.string-value {
  margin: 0;
  padding: 8px;
  border-radius: 6px;
  background: #fafafa;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
