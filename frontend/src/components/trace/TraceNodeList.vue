<template>
  <!-- 统一 trace 的节点行：reason（思维链）/ llm（模型往返）/ tool（工具调用）。
       对话回答与工作流步骤在适配成同一组 TraceNode 后都交给它渲染，本组件不感知来源。 -->
  <div class="tn-list">
    <template v-for="node in nodes" :key="node.key">
      <!-- 思维链：推理模型才有，整段弱色文字直接铺开。 -->
      <div v-if="node.kind === 'reason'" class="tn-reason">
        <div class="tn-label"><BulbOutlined /><span>思考</span></div>
        <div class="tn-reason-text">{{ node.detail }}</div>
      </div>

      <!-- 模型往返：没有工具调用的一轮，只有耗时；进行中还没结束的轮次不会产生节点。 -->
      <div v-else-if="node.kind === 'llm'" class="tn-row">
        <span class="tn-status"><RobotOutlined class="tn-llm-icon" /></span>
        <span class="tn-title">模型思考</span>
        <span v-if="node.elapsedMs != null" class="tn-elapsed">{{ fmtElapsed(node.elapsedMs) }}</span>
      </div>

      <!-- 工具调用：进行中转圈，返回后打勾（失败红叉），点开看入参与返回。 -->
      <div v-else class="tn-row tn-tool" :class="{ 'is-error': node.status === 'error' }">
        <button type="button" class="tn-head" @click="toggle(node.key)">
          <span class="tn-status">
            <a-spin v-if="node.status === 'running'" size="small" />
            <CloseCircleFilled v-else-if="node.status === 'error'" class="tn-icon-error" />
            <CheckCircleFilled v-else class="tn-icon-done" />
          </span>
          <ToolOutlined class="tn-tool-icon" />
          <span class="tn-title" :title="node.tool">{{ node.title }}</span>
          <span
            v-if="node.elapsedMs != null"
            class="tn-elapsed"
            :title="startedTitle(node)"
          >{{ fmtElapsed(node.elapsedMs) }}</span>
          <DownOutlined
            v-if="node.blocks.length"
            :class="['tn-caret', { open: isOpen(node) }]"
          />
        </button>
        <div v-if="isOpen(node) && node.blocks.length" class="tn-detail">
          <div v-for="(block, bi) in node.blocks" :key="bi" class="tn-block">
            <div class="tn-block-label">
              <span :class="{ 'tn-error-text': block.tone === 'error' }">{{ block.label }}</span>
              <button
                v-if="block.copy"
                type="button"
                class="tn-copy"
                @click="copyBlock(node.key, bi, block.text)"
              >
                <CopyOutlined />
                {{ copiedKey === `${node.key}:${bi}` ? '已复制' : '复制' }}
              </button>
            </div>
            <pre
              class="tn-code"
              :class="{ 'tn-code-error': block.tone === 'error' }"
            >{{ block.text }}</pre>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import {
  BulbOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  CopyOutlined,
  DownOutlined,
  RobotOutlined,
  ToolOutlined,
} from '@ant-design/icons-vue'
import { fmtElapsed } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import type { TraceNode } from '@/utils/traceModel'

const props = defineProps<{
  nodes: TraceNode[]
  /** 外层是否还在流式：流式期间最新一个已返回的工具默认展开，落定后默认全收。 */
  streaming?: boolean
}>()

// 展开状态：没手动点过就走默认——进行中的工具展开看入参，流式中最新一次返回
// 也展开（看一眼结果）；落定后默认全部收起。
const toggled = reactive<Record<string, boolean>>({})
function toggle(key: string) {
  toggled[key] = !isOpenByDefault(key)
}
function isOpen(node: TraceNode): boolean {
  if (node.key in toggled) return toggled[node.key]
  return isOpenByDefault(node.key)
}
function isOpenByDefault(key: string): boolean {
  const node = props.nodes.find((item) => item.key === key)
  if (!node) return false
  if (node.status === 'running') return true
  if (!props.streaming) return false
  const lastTool = [...props.nodes].reverse().find((item) => item.kind === 'tool')
  return lastTool?.key === node.key && node.status === 'done'
}

function startedTitle(node: TraceNode): string {
  if (!node.startedAt) return ''
  const at = new Date(node.startedAt)
  if (Number.isNaN(at.getTime())) return node.startedAt
  // 只在悬浮里给绝对起点：行内空间留给耗时，避免两处都报时刻。
  return `开始于 ${at.toLocaleTimeString()}`
}

const copiedKey = ref<string | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | undefined
async function copyBlock(nodeKey: string, blockIndex: number, text: string) {
  if (await copyText(text)) {
    copiedKey.value = `${nodeKey}:${blockIndex}`
    if (copiedTimer) clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => {
      copiedKey.value = null
    }, 1500)
  }
}
</script>

<style scoped>
.tn-reason {
  margin: 6px 0;
}
.tn-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 2px;
  font-size: var(--fs-xs);
}
.tn-reason-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-3);
  line-height: 1.6;
}
.tn-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: var(--fs-sm);
  line-height: 1.5;
  color: var(--text-3);
}
.tn-tool {
  /* 标题行与展开的入参/返回结果是兄弟节点：不改成纵向的话，详情块会当成
     第二个 flex 项被 100% 宽的标题按钮挤到右侧，还跟着 align-items 垂直居中。 */
  flex-direction: column;
  align-items: stretch;
}
.tn-tool + .tn-tool {
  margin-top: 0;
}
.tn-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 2px 0;
  border: none;
  background: transparent;
  color: inherit;
  font-size: inherit;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}
.tn-head:hover {
  color: var(--text-2);
}
.tn-status {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  flex-shrink: 0;
}
.tn-icon-done {
  color: var(--success);
}
.tn-icon-error {
  color: var(--danger, #ff4d4f);
}
.tn-llm-icon {
  color: var(--signal-text);
}
.tn-tool-icon {
  flex-shrink: 0;
  font-size: var(--fs-xs);
}
.tn-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tn-elapsed {
  flex-shrink: 0;
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--text-3);
  opacity: 0.75;
}
.tn-caret {
  flex-shrink: 0;
  font-size: 10px;
  transition: transform 0.18s ease;
  transform: rotate(-90deg);
}
.tn-caret.open {
  transform: rotate(0deg);
}
.tn-detail {
  margin: 4px 0 4px 22px;
}
.tn-block + .tn-block {
  margin-top: 6px;
}
.tn-block-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.tn-error-text {
  color: var(--danger, #ff4d4f);
}
.tn-copy {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0 4px;
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.tn-copy:hover {
  color: var(--signal-text);
}
.tn-code {
  margin: 0;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-2, rgba(0, 0, 0, 0.04));
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: var(--fs-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
}
.tn-code-error {
  color: var(--danger, #ff4d4f);
}
</style>
