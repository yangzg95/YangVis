<template>
  <div class="page-card">
    <div class="page-title">本地工具</div>
    <div class="subtitle">
      后端代码注册、可以在对话里勾选给大模型调用的本地方法。对话页的「选择工具」
      里出现的「本地工具」分组就是这份目录。
    </div>

    <div class="toolbar">
      <a-alert
        type="info"
        show-icon
        message="本地工具以代码为唯一事实源：在后端 services/tools/builtins/ 下新增函数并重启服务即被收录，本页只读，不提供新增或编辑。"
      />
    </div>

    <a-table
      :data-source="tools"
      :loading="loading"
      :pagination="false"
      row-key="key"
    >
      <a-table-column key="title" title="标题" :width="150">
        <template #default="{ record }">
          <span>{{ record.title }}</span>
        </template>
      </a-table-column>
      <a-table-column key="key" title="工具名" :width="170">
        <template #default="{ record }">
          <a-tag class="key-tag">{{ record.key }}</a-tag>
        </template>
      </a-table-column>
      <a-table-column key="description" title="描述" :width="260" ellipsis>
        <template #default="{ record }">
          <a-tooltip :title="record.description">
            <span>{{ record.description }}</span>
          </a-tooltip>
        </template>
      </a-table-column>
      <a-table-column key="params" title="参数">
        <template #default="{ record }">
          <div v-if="record.params.length" class="param-list">
            <div v-for="param in record.params" :key="param.name" class="param-line">
              <span class="param-name">{{ param.name }}</span>
              <span class="param-type">{{ param.type }}</span>
              <span v-if="param.description" class="param-desc">— {{ param.description }}</span>
              <span class="param-meta">
                （{{ param.required ? '必填' : '可选' }}<template
                  v-if="!param.required && param.default !== null"
                  >，默认 {{ formatDefault(param.default) }}</template
                >）
              </span>
            </div>
          </div>
          <span v-else class="param-none">—</span>
        </template>
      </a-table-column>
      <a-table-column key="is_async" title="执行方式" :width="90">
        <template #default="{ record }">
          <a-tag :color="record.is_async ? 'blue' : 'default'">
            {{ record.is_async ? '异步' : '同步' }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column key="source" title="来源" :width="300">
        <template #default="{ record }">
          <span class="source-text">{{ record.source }}</span>
        </template>
      </a-table-column>
    </a-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { toolsApi, type LocalTool } from '@/api'

const tools = ref<LocalTool[]>([])
const loading = ref(false)

/** 默认值目录展示：对象/数组收成一行 JSON，字符串原样，其余转字符串。 */
function formatDefault(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

onMounted(async () => {
  loading.value = true
  try {
    const data = await toolsApi.listLocal()
    tools.value = data.items
  } finally {
    // 失败由 axios 拦截器统一弹错，页面停在空表即可。
    loading.value = false
  }
})
</script>

<style scoped>
.subtitle {
  color: var(--text-3);
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
.toolbar {
  margin-bottom: 16px;
}
.key-tag {
  font-family: var(--font-mono, monospace);
}
.param-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.param-line {
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.param-name {
  font-family: var(--font-mono, monospace);
  color: var(--text-1);
}
.param-type {
  margin-left: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.param-desc {
  margin-left: 6px;
  color: var(--text-2);
}
.param-meta {
  color: var(--text-3);
}
.param-none {
  color: var(--text-3);
}
.source-text {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
  color: var(--text-3);
}
</style>
