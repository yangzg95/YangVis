<template>
  <div class="trp">
    <div class="trp-list">
      <div v-if="!store.items.length" class="trp-empty">暂无传输任务</div>
      <div v-for="item in store.items" :key="item.id" class="trp-row">
        <component
          :is="item.kind === 'upload' ? UploadOutlined : DownloadOutlined"
          :class="['trp-kind', item.kind]"
        />
        <div class="trp-main">
          <div class="trp-name" :title="item.remotePath">{{ item.name }}</div>
          <div class="trp-bar">
            <div
              :class="['trp-bar-inner', item.status, { indeterminate: item.status === 'running' && !item.total }]"
              :style="{ width: barWidth(item) }"
            />
          </div>
          <div class="trp-meta">
            <span>{{ progressText(item) }}</span>
            <span :class="['trp-status', item.status]">{{ statusText(item) }}</span>
          </div>
        </div>
        <a-tooltip v-if="item.status === 'running'" title="取消">
          <button type="button" class="trp-cancel" @click="store.cancel(item.id)">
            <CloseOutlined />
          </button>
        </a-tooltip>
      </div>
    </div>
    <div class="trp-foot">
      <button
        type="button"
        class="trp-clear"
        :disabled="!hasFinished"
        @click="store.clearFinished()"
      >
        清除已完成
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  CloseOutlined,
  DownloadOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { useTransfersStore, type TransferItem } from '@/stores/transfers'
import { fmtSize } from '@/utils/format'

const store = useTransfersStore()

const hasFinished = computed(() => store.items.some((t) => t.status !== 'running'))

function barWidth(item: TransferItem): string {
  if (item.status === 'done') return '100%'
  if (!item.total) return item.status === 'running' ? '100%' : '0%'
  return `${Math.min(100, Math.round((item.loaded / item.total) * 100))}%`
}

function progressText(item: TransferItem): string {
  if (item.total) {
    const pct = Math.min(100, Math.round((item.loaded / item.total) * 100))
    return `${fmtSize(item.loaded)} / ${fmtSize(item.total)} · ${pct}%`
  }
  return fmtSize(item.loaded)
}

const STATUS_TEXT: Record<TransferItem['status'], string> = {
  running: '传输中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
}

function statusText(item: TransferItem): string {
  return item.status === 'error' && item.error ? item.error : STATUS_TEXT[item.status]
}
</script>

<style scoped>
.trp {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.trp-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px 0;
}

.trp-empty {
  padding: 24px 10px;
  color: #5f6b78;
  font-size: 12px;
  text-align: center;
}

.trp-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
}

.trp-row + .trp-row {
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}

.trp-kind {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 13px;
}

.trp-kind.upload {
  color: #3ad6de;
}

.trp-kind.download {
  color: #95de64;
}

.trp-main {
  flex: 1;
  min-width: 0;
}

.trp-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #d8dee4;
  font-size: 12px;
}

.trp-bar {
  height: 4px;
  margin: 6px 0 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.08);
  overflow: hidden;
}

.trp-bar-inner {
  height: 100%;
  border-radius: 2px;
  background: #3ad6de;
  transition: width 0.2s;
}

.trp-bar-inner.done {
  background: #52c41a;
}

.trp-bar-inner.error {
  background: #ff7875;
}

.trp-bar-inner.cancelled {
  background: #5f6b78;
}

/* 后端没给 Content-Length 时的不确定态：整条呼吸。 */
.trp-bar-inner.indeterminate {
  animation: trp-pulse 1.2s ease-in-out infinite;
}

@keyframes trp-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.trp-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: #5f6b78;
  font-family: var(--font-mono);
  font-size: 11px;
}

.trp-status {
  flex-shrink: 0;
  max-width: 55%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: inherit;
}

.trp-status.error {
  color: #ff7875;
}

.trp-status.done {
  color: #52c41a;
}

.trp-cancel {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: #8b98a5;
  font-size: 11px;
  cursor: pointer;
}

.trp-cancel:hover {
  background: rgba(255, 77, 79, 0.15);
  color: #ff7875;
}

.trp-foot {
  padding: 6px 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  text-align: right;
}

.trp-clear {
  padding: 2px 10px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 4px;
  background: transparent;
  color: #aab6c2;
  font-size: 11px;
  cursor: pointer;
}

.trp-clear:hover:not(:disabled) {
  border-color: rgba(58, 214, 222, 0.45);
  color: #3ad6de;
}

.trp-clear:disabled {
  opacity: 0.35;
  cursor: default;
}
</style>
