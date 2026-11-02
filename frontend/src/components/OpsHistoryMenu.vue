<template>
  <a-dropdown
    :trigger="['click']"
    :overlay-class-name="dark ? 'ops-history-overlay dark' : 'ops-history-overlay'"
    @open-change="(open: boolean) => emit('open-change', open)"
  >
    <slot />
    <template #overlay>
      <a-menu :selected-keys="conversationId ? [conversationId] : []">
        <a-menu-item v-for="conv in history" :key="conv.id" @click="emit('open', conv.id)">
          <div class="history-item">
            <div class="history-line">
              <span class="history-title">{{ conv.title }}</span>
              <!-- stop：不触发菜单项的「打开会话」，也不让下拉收起来。 -->
              <a-tooltip title="重命名这段会话">
                <EditOutlined class="history-rename" @click.stop="emit('rename', conv)" />
              </a-tooltip>
              <a-tooltip title="删除这段会话">
                <DeleteOutlined class="history-del" @click.stop="emit('remove', conv)" />
              </a-tooltip>
            </div>
            <span class="history-meta">
              {{ fmtShortDateTime(conv.updated_at) }} · {{ conv.message_count }} 条
            </span>
          </div>
        </a-menu-item>
        <a-menu-item v-if="!history.length" key="empty" disabled>
          还没有历史会话
        </a-menu-item>
      </a-menu>
    </template>
  </a-dropdown>
</template>

<script setup lang="ts">
import { DeleteOutlined, EditOutlined } from '@ant-design/icons-vue'
import type { Conversation } from '@/api'
import { fmtShortDateTime } from '@/utils/format'

defineProps<{
  history: Conversation[]
  conversationId: number | null
  /** 深色变体：嵌在深色终端页的 AI 面板里。 */
  dark?: boolean
}>()

const emit = defineEmits<{
  (e: 'open', id: number): void
  (e: 'rename', conv: Conversation): void
  (e: 'remove', conv: Conversation): void
  /** 下拉展开/收起：展开时父组件顺手刷新一次历史。 */
  (e: 'open-change', open: boolean): void
}>()
</script>

<!-- 下拉渲染在 body（scoped 够不到），样式只能全局：一律用 ops-history 前缀避免撞名。 -->
<style>
.ops-history-overlay .ant-dropdown-menu {
  max-width: 300px;
  max-height: 320px;
  overflow-y: auto;
}

.ops-history-overlay .history-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0;
}

.ops-history-overlay .history-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ops-history-overlay .history-title {
  flex: 1;
  min-width: 0;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
}

/* 删除图标平时藏着，悬停该条才出现：列表主要还是用来「打开」的。 */
.ops-history-overlay .history-del {
  flex-shrink: 0;
  padding: 2px;
  border-radius: var(--radius-sm);
  color: var(--text-3);
  font-size: var(--fs-xs);
  opacity: 0;
  transition: all 0.15s;
}

.ops-history-overlay .ant-dropdown-menu-item:hover .history-del {
  opacity: 1;
}

.ops-history-overlay .history-del:hover {
  background: var(--danger-bg);
  color: var(--danger);
}

/* 重命名图标与删除同一交互：悬停整行才显形，hover 各自上色。 */
.ops-history-overlay .history-rename {
  flex-shrink: 0;
  padding: 2px;
  border-radius: var(--radius-sm);
  color: var(--text-3);
  font-size: var(--fs-xs);
  opacity: 0;
  transition: all 0.15s;
}

.ops-history-overlay .ant-dropdown-menu-item:hover .history-rename {
  opacity: 1;
}

.ops-history-overlay .history-rename:hover {
  background: var(--signal-bg);
  color: var(--signal-text);
}

.ops-history-overlay .history-meta {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

/* 深色变体（全屏终端页的 AI 面板）：菜单底色/文字与面板同族。 */
.ops-history-overlay.dark .ant-dropdown-menu {
  background: var(--surface-dark);
}

.ops-history-overlay.dark .ant-dropdown-menu-item {
  color: var(--text-dark);
}

.ops-history-overlay.dark .ant-dropdown-menu-item:hover,
.ops-history-overlay.dark .ant-dropdown-menu-item-selected {
  background: var(--signal-on-dark-bg-strong) !important;
  color: var(--signal-bright);
}

.ops-history-overlay.dark .ant-dropdown-menu-item-disabled {
  color: var(--text-dark-2) !important;
}

.ops-history-overlay.dark .history-meta {
  color: var(--text-dark-2);
}

.ops-history-overlay.dark .history-del {
  color: var(--text-dark-2);
}

.ops-history-overlay.dark .history-del:hover {
  background: var(--danger-on-dark-bg);
  color: var(--danger-soft);
}
</style>
