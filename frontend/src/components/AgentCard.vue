<template>
  <a-card size="small" class="agent-card">
    <div class="agent-head">
      <span class="agent-name">{{ agent.name }}</span>
      <a-tag v-if="agent.is_builtin">内置</a-tag>
    </div>

    <div class="agent-desc">{{ agent.description || '暂无描述' }}</div>

    <div class="agent-tags">
      <a-tag :color="agent.use_knowledge ? 'blue' : 'default'">
        {{ agent.use_knowledge ? '检索知识库' : '不检索知识库' }}
      </a-tag>
      <a-tag v-if="agent.use_ops" color="cyan">运维只读</a-tag>
      <a-tag v-if="!agent.chat_visible" color="default">对话中隐藏</a-tag>
      <a-tag>发散度 {{ agent.temperature }}</a-tag>
      <a-tag v-if="!agent.enabled" color="orange">已停用</a-tag>
    </div>

    <template #actions>
      <span @click="emit('view', agent)">查看</span>
      <span @click="emit('duplicate', agent)">复制</span>
      <template v-if="!agent.is_builtin">
        <span @click="emit('edit', agent)">编辑</span>
        <a-popconfirm title="确认删除该智能体？" @confirm="emit('delete', agent)">
          <span class="danger">删除</span>
        </a-popconfirm>
      </template>
    </template>
  </a-card>
</template>

<script setup lang="ts">
import type { Agent } from '@/api'

defineProps<{ agent: Agent }>()

const emit = defineEmits<{
  view: [agent: Agent]
  duplicate: [agent: Agent]
  edit: [agent: Agent]
  delete: [agent: Agent]
}>()
</script>

<style scoped>
.agent-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.agent-name {
  font-weight: 600;
  font-size: 15px;
}
.agent-desc {
  color: rgba(0, 0, 0, 0.65);
  font-size: 13px;
  min-height: 40px;
  line-height: 1.5;
}
.agent-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}
.danger {
  color: #cf1322;
}
</style>
