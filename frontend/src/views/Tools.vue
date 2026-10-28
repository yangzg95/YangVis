<template>
  <div class="page-card">
    <div class="page-title">工具</div>
    <div class="subtitle">
      智能体与工作流可调用的工具目录：MCP 服务是外部进程或 HTTP 暴露的工具，
      可增删改与连通测试；本地工具由后端代码注册，只读。对话页「选择工具」里的
      两个分组就是这两份目录。
    </div>

    <a-tabs v-model:activeKey="activeTab">
      <a-tab-pane key="mcp" tab="MCP 服务">
        <McpPanel />
      </a-tab-pane>
      <a-tab-pane key="local" tab="本地工具">
        <LocalToolsPanel />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import McpPanel from '@/components/tools/McpPanel.vue'
import LocalToolsPanel from '@/components/tools/LocalToolsPanel.vue'

// 默认 MCP：它是可配置的主目录，本地工具只是只读参考。
const activeTab = ref('mcp')
</script>

<style scoped>
.subtitle {
  color: var(--text-3);
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
</style>
