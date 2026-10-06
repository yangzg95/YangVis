<template>
  <div class="wf-canvas">
    <div class="canvas-hint">
      一列一个并行组：列间顺序执行，列内几步同时跑。线是从模板里的
      <span v-pre>{{引用}}</span> 算出来的，点节点跳到左边那一步。
    </div>

    <div ref="scrollEl" class="canvas-scroll">
      <div ref="contentEl" class="canvas-content">
        <svg class="canvas-edges" :width="size.w" :height="size.h" aria-hidden="true">
          <defs>
            <marker :id="markerId" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 z" class="edge-head" />
            </marker>
          </defs>
          <path v-for="(d, i) in paths" :key="i" :d="d" class="edge" :marker-end="`url(#${markerId})`" />
        </svg>

        <!-- 输入泳道：只读，看一眼这条流程收哪些变量，不参与画线。 -->
        <div v-if="inputNames.length" class="lane lane-input">
          <div class="lane-head">
            <span class="lane-name">输入变量</span>
            <span class="lane-count">{{ inputNames.length }} 个</span>
          </div>
          <div class="lane-body">
            <div v-for="name in inputNames" :key="name" class="input-chip" v-text="'{{' + name + '}}'" />
          </div>
        </div>

        <template v-for="(group, gi) in groups" :key="gi">
          <div v-if="gi || inputNames.length" class="lane-arrow">→</div>

          <div class="lane">
            <div class="lane-head">
              <span class="lane-name" :title="group.name">{{ group.name || '未分组' }}</span>
              <span class="lane-count">{{ group.indexes.length }} 步</span>
            </div>
            <div class="lane-body">
              <div
                v-for="si in group.indexes"
                :key="si"
                class="wf-node"
                :class="{
                  'node-active': active === si,
                  'node-problem': nodeProblem(si),
                  'node-optional': steps[si].optional,
                }"
                :data-si="si"
                :title="`跳到第 ${si + 1} 步`"
                @click="$emit('locate', si)"
              >
                <div class="node-top">
                  <span class="node-no">{{ si + 1 }}</span>
                  <span class="node-key">{{ steps[si].key.trim() || '（未命名）' }}</span>
                  <span v-if="steps[si].output_kind === 'json'" class="node-badge">JSON</span>
                </div>
                <div class="node-agent">{{ agentName(steps[si].agent_slug) }}</div>
                <div class="node-var" :class="{ 'node-var-none': !varLabel(si) }">
                  {{ varLabel(si) || '不产出变量' }}
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Agent } from '@/api'
import { groupSteps, stepLinks, type StepForm, type StepGroup } from './workflowLayout'

const props = withDefaults(
  defineProps<{
    steps: StepForm[]
    /** 列表里正在看的那一步：对应节点跟着亮，null 表示谁都没亮。 */
    active: number | null
    /** 声明过的输入变量名，画在最左侧只读泳道里。 */
    inputNames: string[]
    agents: Agent[]
    problemIds: Set<string>
    showProblems: boolean
  }>(),
  { active: null },
)

defineEmits<{
  /** 点了第几号节点：父级负责把列表滚到那一张卡片。 */
  (e: 'locate', index: number): void
}>()

const groups = computed<StepGroup[]>(() => groupSteps(props.steps))
const links = computed(() => stepLinks(props.steps))

/** 几张画布同时挂载也不能撞 marker id，与 MermaidCard 同一招。 */
let markerSeq = 0
const markerId = `wf-edge-head-${++markerSeq}`

function agentName(slug: string) {
  if (!slug) return '未选人设'
  return props.agents.find((agent) => agent.slug === slug)?.name ?? slug
}

function varLabel(index: number) {
  const name = props.steps[index].output_var.trim()
  return name ? `→ {{${name}}}` : ''
}

function nodeProblem(index: number) {
  if (!props.showProblems) return false
  const prefix = `step${index}.`
  for (const id of props.problemIds) if (id.startsWith(prefix)) return true
  return false
}

// ---- 引用线：位置全部从 DOM 量出来，布局变了就重量 -----------------------------

const scrollEl = ref<HTMLElement | null>(null)
const contentEl = ref<HTMLElement | null>(null)
const paths = ref<string[]>([])
const size = ref({ w: 0, h: 0 })

/**
 * 线画在内容坐标系里：横向滚动挪的是视口，节点相对内容的位置不用重算。
 *
 * 节点每次现从 DOM 上按 data-si 找，不缓存元素：加一步、改组名之后同一批 DOM 节点
 * 挂的是哪一步全变了，缓存下来的只会认错人、把线画丢。
 */
function measure() {
  const content = contentEl.value
  if (!content) return
  const base = content.getBoundingClientRect()
  size.value = { w: content.scrollWidth, h: content.scrollHeight }
  const elAt = new Map<number, HTMLElement>()
  content.querySelectorAll<HTMLElement>('.wf-node[data-si]').forEach((el) => {
    elAt.set(Number(el.dataset.si), el)
  })
  const list: string[] = []
  for (const link of links.value) {
    const from = elAt.get(link.from)
    const to = elAt.get(link.to)
    if (!from || !to) continue
    const a = from.getBoundingClientRect()
    const b = to.getBoundingClientRect()
    const x1 = a.right - base.left
    const y1 = a.top + a.height / 2 - base.top
    const x2 = b.left - base.left
    const y2 = b.top + b.height / 2 - base.top
    const bend = Math.max(28, (x2 - x1) / 2)
    list.push(`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`)
  }
  paths.value = list
}

/**
 * 高亮是跟着列表滚过来的，节点可能横在视野外：把它滚进来。
 *
 * 只动这一层容器的 scrollLeft，不用 scrollIntoView——那一下会连带把整页顶下去。
 */
function revealActive() {
  const scroller = scrollEl.value
  const node = contentEl.value?.querySelector<HTMLElement>(`.wf-node[data-si="${props.active}"]`)
  if (!scroller || !node) return
  const port = scroller.getBoundingClientRect()
  const box = node.getBoundingClientRect()
  const gap = 12
  if (box.right > port.right - gap) scroller.scrollLeft += box.right - port.right + gap
  else if (box.left < port.left + gap) scroller.scrollLeft -= port.left + gap - box.left
}

let observer: ResizeObserver | null = null

onMounted(() => {
  void nextTick(measure)
  if (contentEl.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => measure())
    observer.observe(contentEl.value)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

// 增删步骤、改组名、改模板都会让线重算。
watch(
  () =>
    props.steps
      .map((step) => `${step.group_key}|${step.key}|${step.output_var}|${step.prompt_tpl}`)
      .join('\n'),
  () => void nextTick(measure),
)

watch(() => props.active, () => void nextTick(revealActive))
</script>

<style scoped>
.canvas-hint {
  color: var(--text-3);
  font-size: 12px;
  margin-bottom: 8px;
}
.canvas-scroll {
  overflow: auto;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--surface);
  padding: 12px;
}
.canvas-content {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 4px;
  min-width: max-content;
}
.canvas-edges {
  position: absolute;
  top: 0;
  left: 0;
  z-index: 1;
  pointer-events: none;
}
.edge {
  fill: none;
  stroke: var(--text-3);
  stroke-width: 1.4;
  opacity: 0.5;
}
.edge-head {
  fill: var(--text-3);
  opacity: 0.6;
}
.lane {
  position: relative;
  z-index: 2;
  width: 176px;
  flex: none;
  border: 1px dashed var(--hairline);
  border-radius: var(--radius-md);
  background: var(--paper);
  padding: 6px;
}
.lane-arrow {
  position: relative;
  z-index: 2;
  flex: none;
  align-self: center;
  color: var(--text-3);
  font-size: 12px;
}
.lane-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px 6px;
}
.lane-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lane-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
}
.lane-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 72px;
}
.lane-input .input-chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 2px 6px;
}
.wf-node {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 6px 8px;
  cursor: pointer;
}
.wf-node:hover {
  border-color: var(--signal-border);
}
.node-active {
  border-color: var(--signal);
  box-shadow: 0 0 0 1px var(--signal-border);
}
.node-problem {
  border-color: #ff4d4f;
}
.node-optional .node-key {
  color: var(--text-3);
}
.node-top {
  display: flex;
  align-items: center;
  gap: 6px;
}
.node-no {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.node-key {
  font-family: var(--font-mono);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-badge {
  margin-left: auto;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.5;
  padding: 0 4px;
}
.node-agent {
  color: var(--text-2);
  font-size: 12px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-var {
  color: var(--text-3);
  font-family: var(--font-mono);
  font-size: 11px;
  margin-top: 2px;
}
.node-var-none {
  opacity: 0.7;
}
</style>
