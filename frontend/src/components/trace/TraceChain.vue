<template>
  <!-- 统一 trace 时间线：内部节点模型 TraceNode 适配成 ant-design-x-vue 的
       ThoughtChain items。虚线轨道、状态图标（转圈/打勾/红叉）、running 扫光、
       折叠箭头与展开动效全部交给库组件，本文件只保留两件业务逻辑：
       1. TraceNode → items 的映射（reason/llm/tool 三类节点）；
       2. 工具节点入参/返回结果 blocks 的渲染与复制。 -->
  <ThoughtChain
    class="tc"
    :items="items"
    line="dashed"
    :expanded-keys="expandedKeys"
    @expand="onExpand"
  />
</template>

<script setup lang="ts">
import { computed, h, reactive, ref } from 'vue'
// 只用命名导入：默认导出是带全量 install 的对象，会把 mermaid/prism/x-sdk 整条
// 依赖链激活打进 bundle；命名导入配合 sideEffects:false 才能摇掉未用组件。
import { ThoughtChain, type ThoughtChainItemType } from 'ant-design-x-vue'
import { CopyOutlined, RobotOutlined } from '@ant-design/icons-vue'
import { fmtElapsed } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import { toolStateText } from '@/utils/chatTools'
import type { TraceBlock, TraceNode } from '@/utils/traceModel'

const props = defineProps<{
  nodes: TraceNode[]
  /** 外层是否还在流式：流式中的最后一段思考保持整段铺开（边生成边看），落定后收回一行摘要。 */
  streaming?: boolean
}>()

// 展开状态：工具的入参/返回结果可能很长（SQL、检索片段），默认全部收起，
// 不挤占消息流；用户点箭头展开的手动意图存 toggled。running 中的工具也不
// 自动展开——头部的转圈与扫光已足够表达进行中。
const toggled = reactive<Record<string, boolean>>({})
const expandedKeys = computed(() =>
  props.nodes.filter((node) => toggled[node.key]).map((node) => node.key),
)
// 库回调给的是展开后的全量 keys，与当前受控值做差集，差异的那个就是被点击的节点。
function onExpand(keys: string[]) {
  const next = new Set(keys)
  for (const node of props.nodes) {
    const wasOpen = expandedKeys.value.includes(node.key)
    const isOpen = next.has(node.key)
    if (wasOpen !== isOpen) toggled[node.key] = isOpen
  }
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

/** 工具节点的入参/返回结果：库没有代码块概念，用 h 构造业务 blocks 放进 content。 */
function blocksContent(node: TraceNode) {
  return h(
    'div',
    { class: 'tc-blocks' },
    node.blocks.map((block: TraceBlock, bi: number) =>
      h('div', { class: 'tc-block', key: bi }, [
        h('div', { class: 'tc-block-label' }, [
          h(
            'span',
            { class: block.tone === 'error' ? 'tc-error-text' : '' },
            block.label,
          ),
          block.copy
            ? h(
                'button',
                {
                  class: 'tc-copy',
                  type: 'button',
                  onClick: () => copyBlock(node.key, bi, block.text),
                },
                [
                  h(CopyOutlined),
                  copiedKey.value === `${node.key}:${bi}` ? '已复制' : '复制',
                ],
              )
            : null,
        ]),
        h(
          'pre',
          {
            class: ['tc-code', block.tone === 'error' ? 'tc-code-error' : ''],
          },
          block.text,
        ),
      ]),
    ),
  )
}

/**
 * 轨迹行主体：动作名 + 状态词 + 灰色摘要 + 耗时，全挤进库组件的 title 位。
 *
 * 不用 ThoughtChain 的 description：它的 header 是纵向 flex，description 会掉到
 * 第二行，而这里要的是「一行一件事」。摘要吃掉剩余宽度并省略号截断，动作名与
 * 耗时不缩。
 */
function rowLine(node: TraceNode, withState: boolean) {
  return h('span', { class: 'tc-line' }, [
    h('span', { class: 'tc-action' }, node.action),
    withState
      ? h(
          'span',
          { class: ['tc-state', node.status === 'error' ? 'tc-state--error' : ''] },
          toolStateText(node.status),
        )
      : null,
    node.preview ? h('span', { class: 'tc-preview' }, `· ${node.preview}`) : null,
    node.elapsedMs != null
      ? h('span', { class: 'tc-elapsed', title: startedTitle(node) }, fmtElapsed(node.elapsedMs))
      : null,
  ])
}

const items = computed<ThoughtChainItemType[]>(() =>
  props.nodes.map((node, index) => {
    // 思维链：行内只留首行摘要，全文收在展开内容里。唯一例外是还在生成的最后
    // 一段——那会儿要看它正在想，整段铺开（外层容器负责限高内滚）。
    if (node.kind === 'reason') {
      const live = !!props.streaming && index === props.nodes.length - 1
      return {
        key: node.key,
        status: live ? ('loading' as const) : ('success' as const),
        title: rowLine(node, false),
        collapsible: !live,
        content: live
          ? h('div', { class: 'tc-reason-text' }, node.detail ?? '')
          : blocksContent(node),
      }
    }
    // 模型往返：机器人图标 + 耗时，没有可展开内容。
    if (node.kind === 'llm') {
      return { key: node.key, icon: h(RobotOutlined), title: rowLine(node, false) }
    }
    // 工具调用：状态交给库——loading 转圈（标题扫光 blink）、success 打勾、
    // error 红叉；有 blocks 才可折叠，content 挂入参/返回结果。
    return {
      key: node.key,
      status:
        node.status === 'running'
          ? 'loading'
          : node.status === 'error'
            ? 'error'
            : 'success',
      blink: node.status === 'running',
      title: rowLine(node, true),
      collapsible: node.blocks.length > 0,
      content: node.blocks.length ? blocksContent(node) : undefined,
    }
  }),
)
</script>

<style scoped>
/* 压到气泡内的紧凑字号；颜色/轨道/状态图标全部吃库主题 token。 */
.tc :deep(.ant-thought-chain) {
  font-size: var(--fs-sm);
}
/* 库默认给 title 留了 inline-block + nowrap，行内摘要要能自己收缩。 */
.tc :deep(.ant-thought-chain-node-title) {
  min-width: 0;
  overflow: hidden;
}
.tc-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  /* 折叠箭头由库追加在 title 末尾，这一行得先给它让出宽度。 */
  flex: 1 1 auto;
}
.tc-action {
  flex: none;
  color: var(--text-2);
  font-weight: 500;
}
.tc-state {
  flex: none;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.tc-state--error {
  color: var(--danger);
}
.tc-preview {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: var(--text-3);
  font-weight: 400;
}
.tc-elapsed {
  flex: none;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.tc-reason-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-3);
  line-height: 1.6;
}
/* 详情盒沿用 ThoughtChain 文档范例：8px 圆角、浅底、1px 边框。 */
.tc-blocks {
  margin: 4px 0;
  padding: 10px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.tc-block + .tc-block {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--hairline);
}
.tc-block-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.tc-error-text {
  color: var(--danger);
}
.tc-copy {
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
.tc-copy:hover {
  color: var(--signal-text);
}
.tc-code {
  margin: 0;
  padding: 0;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: var(--fs-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
}
.tc-code-error {
  color: var(--danger);
}
</style>
