<template>
  <div class="wf-canvas">
    <div class="canvas-head">
      <span class="canvas-title">结构图</span>
      <span class="spacer" />
      <!-- 缩放与折叠是画布自己的事：父级只把它当全宽块放，只读模式下这些照样能用。 -->
      <a-button-group size="small" class="canvas-zoom">
        <a-button :disabled="scale <= 0.301" title="缩小" @click="zoomBy(-0.1)">
          <MinusOutlined />
        </a-button>
        <a-button title="适应宽度" @click="applyFit">
          <ColumnWidthOutlined />
        </a-button>
        <a-button :disabled="scale >= 1.999" title="放大" @click="zoomBy(0.1)">
          <PlusOutlined />
        </a-button>
        <a-button title="实际大小" @click="zoomTo100">100%</a-button>
      </a-button-group>
      <span class="canvas-zoom-pct">{{ Math.round(scale * 100) }}%</span>
      <a-button
        v-if="showFullscreen"
        size="small"
        class="canvas-fs-btn"
        :title="fullscreen ? '退出全屏' : '全屏'"
        @click="$emit('toggle-fullscreen')"
      >
        <component :is="fullscreen ? FullscreenExitOutlined : FullscreenOutlined" />
        {{ fullscreen ? '退出全屏' : '全屏' }}
      </a-button>
      <a-button
        v-if="collapsible"
        size="small"
        type="text"
        :class="{ collapsed }"
        title="折叠结构图"
        @click="toggleCollapsed"
      >
        <DownOutlined />
      </a-button>
    </div>

    <div v-show="!collapsed" class="canvas-body">
      <div class="canvas-hint">
        上→下：一行一个并行组，行内几步同时跑。线是从模板里的
        <span v-pre>{{引用}}</span> 算出来的；悬停节点看详情、高亮引用线，点节点跳回该步配置。
      </div>

      <!-- 唯一滚动层：默认 100% 原尺寸，组多向下滚；组内节点特别多时再出横向滚动。 -->
      <div ref="scrollEl" class="canvas-scroll">
        <!-- 占位层：transform 不占文档流，缩放后的宽高得由它撑开，滚动范围才对。 -->
        <div class="canvas-holder" :style="holderStyle">
          <!-- 变换层：origin 左上，内容的布局量不受它影响，量到的永远是自然尺寸。 -->
          <div class="canvas-scale" :style="scaleStyle">
            <div ref="contentEl" class="canvas-content">
              <svg class="canvas-edges" :width="size.w" :height="size.h" aria-hidden="true">
                <defs>
                  <marker :id="markerId" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
                    <path d="M0,0 L6,3 L0,6 z" class="edge-head" />
                  </marker>
                </defs>
                <g v-for="(edge, i) in edges" :key="i">
                  <path
                    :d="edge.d"
                    class="edge"
                    :class="edgeClass(edge)"
                    :marker-end="`url(#${markerId})`"
                  />
                  <text
                    :x="edge.labelX"
                    :y="edge.labelY"
                    class="edge-label"
                    :class="edgeClass(edge)"
                  >
                    {{ edge.variable }}
                  </text>
                </g>
              </svg>

              <!-- 输入变量行：只读，看一眼这条流程收哪些变量，不参与画线。 -->
              <div v-if="inputNames.length" class="io-row">
                <span class="io-label">输入</span>
                <div class="io-chips">
                  <span
                    v-for="name in inputNames"
                    :key="name"
                    class="input-chip mono"
                    v-text="'{{' + name + '}}'"
                  />
                </div>
              </div>

              <template v-for="(group, gi) in groups" :key="gi">
                <div v-if="gi || inputNames.length" class="flow-arrow">↓</div>

                <div class="group-row">
                  <div class="group-head">
                    <span class="group-name" :title="groupLabels[gi]">
                      {{ groupLabels[gi] }}
                    </span>
                    <span class="group-meta">
                      {{ group.indexes.length > 1 ? `${group.indexes.length} 步并行` : '1 步' }}
                    </span>
                  </div>

                  <div class="group-body">
                    <a-popover
                      v-for="si in group.indexes"
                      :key="si"
                      trigger="hover"
                      placement="right"
                      :mouse-enter-delay="0.25"
                    >
                      <template #content>
                        <div class="node-detail">
                          <div class="nd-head">
                            <span class="nd-key mono">
                              {{ steps[si].key.trim() || '（未命名）' }}
                            </span>
                            <span class="nd-no">第 {{ si + 1 }} 步</span>
                          </div>

                          <div class="nd-row">
                            <span class="nd-k">人设</span>
                            <span class="nd-v">{{ agentName(steps[si].agent_slug) }}</span>
                            <span v-if="steps[si].agent_slug" class="nd-sub mono">
                              {{ steps[si].agent_slug }}
                            </span>
                          </div>

                          <div class="nd-row nd-col">
                            <span class="nd-k">Prompt</span>
                            <pre class="nd-prompt">{{ steps[si].prompt_tpl }}</pre>
                          </div>

                          <div v-if="refsOf(si).length" class="nd-row nd-col">
                            <span class="nd-k">引用</span>
                            <div class="nd-chips">
                              <span v-for="r in refsOf(si)" :key="r" class="nd-chip mono">
                                {{ '{' + '{' + r + '}' + '}' }}
                              </span>
                            </div>
                          </div>

                          <div class="nd-row nd-col">
                            <span class="nd-k">产出</span>
                            <div class="nd-chips">
                              <span class="nd-chip mono">
                                {{ steps[si].output_var.trim() || '不产出变量' }}
                              </span>
                              <span
                                v-for="k in jsonKeysOf(si)"
                                :key="k"
                                class="nd-chip mono"
                              >.{{ k }}</span>
                            </div>
                          </div>

                          <div class="nd-row">
                            <span class="nd-k">模型</span>
                            <span class="nd-v">{{ modelNameOf(si) }}</span>
                            <a-tag v-if="steps[si].optional" color="orange" class="nd-opt">
                              可选
                            </a-tag>
                          </div>
                        </div>
                      </template>

                      <div
                        class="wf-node"
                        :data-si="si"
                        :class="nodeClass(si)"
                        @click="$emit('locate', si)"
                        @mouseenter="hovered = si"
                        @mouseleave="hovered = null"
                      >
                        <div class="node-top">
                          <span class="node-no">{{ si + 1 }}</span>
                          <span class="node-key">
                            {{ steps[si].key.trim() || '（未命名）' }}
                          </span>
                          <span v-if="steps[si].output_kind === 'json'" class="node-badge">
                            JSON
                          </span>
                          <span v-if="steps[si].optional" class="node-badge node-badge-opt">
                            可选
                          </span>
                          <ThunderboltOutlined
                            v-if="steps[si].model_config_id != null"
                            class="node-model-ic"
                            :title="modelNameOf(si)"
                          />
                        </div>

                        <div class="node-agent">
                          {{ agentName(steps[si].agent_slug) }}
                        </div>

                        <div class="node-prompt">{{ promptSummary(si) }}</div>

                        <div v-if="refsOf(si).length" class="node-refs">
                          <span class="node-section-lbl">引用</span>
                          <span v-for="r in refsOf(si)" :key="r" class="mini-chip mono">
                            {{ r }}
                          </span>
                        </div>

                        <div class="node-out">
                          <span class="node-section-lbl">产出</span>
                          <span class="node-out-name">
                            {{ varLabel(si) || '不产出' }}
                          </span>
                          <span v-for="k in jsonKeysOf(si)" :key="k" class="mini-chip mono">
                            .{{ k }}
                          </span>
                        </div>
                      </div>
                    </a-popover>
                  </div>
                </div>
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onActivated,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import {
  ColumnWidthOutlined,
  DownOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  MinusOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import type { Agent } from '@/api'
import { storageKeys } from '@/utils/storage'
import {
  groupSteps,
  groupSequence,
  groupLabel,
  referencedVars,
  stepLinks,
  type StepForm,
  type StepGroup,
  type StepLink,
} from './workflowLayout'

const props = withDefaults(
  defineProps<{
    steps: StepForm[]
    /** 列表里正在看的那一步：对应节点跟着亮，null 表示谁都没亮。 */
    active: number | null
    /** 声明过的输入变量名，画在最上面的输入行里。 */
    inputNames: string[]
    agents: Agent[]
    /** 用于显示指定了模型的步骤覆盖的是哪个模型。 */
    modelOptions?: { value: number; label: string }[]
    problemIds: Set<string>
    showProblems: boolean
    /** 是否显示折叠按钮并读取折叠偏好；弹窗预览传 false，强制展开。 */
    collapsible?: boolean
    /** 是否在工具栏显示全屏按钮；当前仅预览弹窗需要。 */
    showFullscreen?: boolean
    /** 弹窗当前是否全屏，按钮图标/文案跟着它切换。 */
    fullscreen?: boolean
  }>(),
  {
    active: null,
    modelOptions: () => [],
    collapsible: true,
    showFullscreen: false,
    fullscreen: false,
  },
)

defineEmits<{
  /** 点了第几号节点：父级负责跳到对应组的配置页。 */
  (e: 'locate', index: number): void
  /** 点了全屏按钮：父级负责切换弹窗的全屏 class。 */
  (e: 'toggle-fullscreen'): void
}>()

const groups = computed<StepGroup[]>(() => groupSteps(props.steps))
/** 组标题用组内步骤标识拼成，不显示 g1/g2 这种内部凭据。 */
const groupLabels = computed(() => groups.value.map((g) => groupLabel(props.steps, g)))
const links = computed<StepLink[]>(() => stepLinks(props.steps))

/** 几张画布同时挂载也不能撞 marker id，与 MermaidCard 同一招。 */
let markerSeq = 0
const markerId = `wf-edge-head-${++markerSeq}`

function agentName(slug: string) {
  if (!slug) return '未选人设'
  return props.agents.find((agent) => agent.slug === slug)?.name ?? slug
}

function varLabel(index: number) {
  const name = props.steps[index].output_var.trim()
  return name ? `{{${name}}}` : ''
}

function refsOf(index: number) {
  return referencedVars(props.steps[index].prompt_tpl)
}

function jsonKeysOf(index: number) {
  const step = props.steps[index]
  return step.output_kind === 'json' ? step.output_keys : []
}

/** Prompt 摘要：空白压成一格，CSS 两行截断，不把节点撑高。 */
function promptSummary(index: number) {
  return props.steps[index].prompt_tpl.replace(/\s+/g, ' ').trim()
}

function modelNameOf(index: number) {
  const step = props.steps[index]
  if (step.model_config_id == null) return '默认对话模型'
  return (
    props.modelOptions.find((m) => m.value === step.model_config_id)?.label ??
    `模型 #${step.model_config_id}`
  )
}

function nodeProblem(index: number) {
  if (!props.showProblems) return false
  const prefix = `step${index}.`
  for (const id of props.problemIds) if (id.startsWith(prefix)) return true
  return false
}

/** 当前悬停的节点，引用线与相关节点都跟着它走。 */
const hovered = ref<number | null>(null)

function edgeRelated(edge: StepLink, si: number) {
  return edge.from === si || edge.to === si
}

function edgeClass(edge: StepLink) {
  const h = hovered.value
  if (h == null) return {}
  const hot = edgeRelated(edge, h)
  return { 'edge-hot': hot, 'edge-dim': !hot }
}

function nodeClass(si: number) {
  const h = hovered.value
  const hot =
    h != null &&
    (si === h || links.value.some((edge) => edgeRelated(edge, h) && edgeRelated(edge, si)))
  return {
    'node-active': props.active === si,
    'node-problem': nodeProblem(si),
    'node-optional': props.steps[si].optional,
    'node-hot': hot,
  }
}

// ---- 缩放：默认 100% 原尺寸，手动档可放可缩；「适应」才缩小看全宽 ----------------

const scrollEl = ref<HTMLElement | null>(null)
const contentEl = ref<HTMLElement | null>(null)

/** 内容未缩放的自然尺寸：scrollWidth/Height 是布局量，祖先的 transform 不影响它。 */
const natural = ref({ w: 0, h: 0 })
const scale = ref(1)
/** fit：跟着容器宽走；manual：−/+ 与 100% 之后只认手动系数。 */
const zoomMode = ref<'fit' | 'manual'>('manual')

/** 只在可折叠时认持久化偏好；弹窗预览强制展开，避免「结构图不展示」。 */
const collapsed = ref(
  props.collapsible && localStorage.getItem(storageKeys.workflowCanvasCollapsed) === '1',
)

function toggleCollapsed() {
  collapsed.value = !collapsed.value
  localStorage.setItem(storageKeys.workflowCanvasCollapsed, collapsed.value ? '1' : '0')
  // display:none → block 的 RO 时序各浏览器不一：展开这一下自己补测＋fit。
  if (!collapsed.value) {
    void nextTick(() => {
      measureNatural()
      applyFit()
    })
  }
}

const PAD = 12 // 与 .canvas-scroll 的 padding 同口径。

function measureNatural() {
  const content = contentEl.value
  if (!content) return
  const w = content.scrollWidth
  const h = content.scrollHeight
  // 折叠态（display:none）量到 0：别写进去，展开时才会有真值。
  if (!w || !h) return
  natural.value = { w, h }
}

function fitScale(): number {
  const vp = scrollEl.value
  if (!vp || !natural.value.w) return 1
  // 适应宽度且不放大超过 1：宽屏留白，窄屏缩小。
  return Math.min(1, (vp.clientWidth - PAD * 2) / natural.value.w)
}

function applyFit() {
  zoomMode.value = 'fit'
  scale.value = fitScale()
}

function zoomBy(delta: number) {
  zoomMode.value = 'manual'
  scale.value = Math.min(2, Math.max(0.3, Math.round((scale.value + delta) * 10) / 10))
  if (props.active != null) void nextTick(revealActive)
}

function zoomTo100() {
  zoomMode.value = 'manual'
  scale.value = 1
  if (props.active != null) void nextTick(revealActive)
}

const holderStyle = computed(() => ({
  width: `${natural.value.w * scale.value}px`,
  height: `${natural.value.h * scale.value}px`,
}))
const scaleStyle = computed(() => ({ transform: `scale(${scale.value})` }))

// ---- 引用线：底出顶入的正交线，横向段走组间空白带，位置从 DOM 量出来 -------------

interface Edge {
  d: string
  from: number
  to: number
  variable: string
  labelX: number
  labelY: number
}

const edges = ref<Edge[]>([])
const size = ref({ w: 0, h: 0 })

/** 每个节点属于第几个并行组，端口分组与跳组绕轨都按它判断。 */
const stepGroupSeq = computed(() => groupSequence(props.steps))

/**
 * 点序列 → 圆角正交路径：中间拐角用二次贝塞尔收个小圆角，首尾段保持笔直，
 * 让箭头正好落在目标边。
 */
function roundedPath(points: Array<[number, number]>, radius: number): string {
  if (points.length < 2) return ''
  let d = `M ${points[0][0]} ${points[0][1]}`
  for (let i = 1; i < points.length - 1; i += 1) {
    const [px, py] = points[i - 1]
    const [x, y] = points[i]
    const [nx, ny] = points[i + 1]
    // 沿来向、去向各退一小段，段太短就退一半，拐角不会刺出线段。
    const backFromX = Math.sign(x - px) * Math.min(radius, Math.abs(x - px) / 2)
    const backFromY = Math.sign(y - py) * Math.min(radius, Math.abs(y - py) / 2)
    const backNextX = Math.sign(x - nx) * Math.min(radius, Math.abs(x - nx) / 2)
    const backNextY = Math.sign(y - ny) * Math.min(radius, Math.abs(y - ny) / 2)
    d += ` L ${x - backFromX} ${y - backFromY} Q ${x} ${y} ${x - backNextX} ${y - backNextY}`
  }
  const last = points[points.length - 1]
  d += ` L ${last[0]} ${last[1]}`
  return d
}

/**
 * 线画在内容的自然坐标系里。
 *
 * 变换在更外层 .canvas-scale 上，content 与节点的 rect 同被缩放：差值统一除以
 * scale 还原成自然坐标；origin 是 top-left，线性可除。size 取自然尺寸而不是 rect。
 *
 * 节点每次现从 DOM 上按 data-si 找，不缓存元素：加一步、改组名之后同一批 DOM 节点
 * 挂的是哪一步全变了，缓存下来的只会认错人、把线画丢。
 *
 * 路由底出顶入：源底边 → 组间空白带（总线）→ 目标顶边。并行组里靠右的节点
 * 再也不会把线横着拉过同组左节点；相邻组走一条总线，跳组才绕左侧主干轨。
 */
function measure() {
  const content = contentEl.value
  if (!content) return
  measureNatural()
  if (!natural.value.w || !natural.value.h) return
  const base = content.getBoundingClientRect()
  size.value = { ...natural.value }
  const s = scale.value || 1
  const elAt = new Map<number, HTMLElement>()
  content.querySelectorAll<HTMLElement>('.wf-node[data-si]').forEach((el) => {
    elAt.set(Number(el.dataset.si), el)
  })

  // 每个并行组行的顶/底（自然坐标）：总线 y 从相邻两组的行间空白里取。
  const groupRects = groups.value.map((g) => {
    let top = Infinity
    let bottom = 0
    for (const si of g.indexes) {
      const el = elAt.get(si)
      if (!(el instanceof HTMLElement)) continue
      const r = el.getBoundingClientRect()
      top = Math.min(top, (r.top - base.top) / s)
      bottom = Math.max(bottom, (r.bottom - base.top) / s)
    }
    return { top: Number.isFinite(top) ? top : 0, bottom }
  })
  /** 第 g 组与 g+1 组之间的总线 y：贴近下行顶部（留 5px），避开空白里的 ↓ 箭头。 */
  function busY(g: number): number {
    const cur = groupRects[g]
    const next = groupRects[g + 1]
    if (!cur || !next) return cur ? cur.bottom + 12 : 0
    const gap = next.top - cur.bottom
    return gap >= 14 ? next.top - 5 : cur.bottom + gap / 2
  }

  // 出入端口先计数：同一节点有多条边时，端口在底边/顶边均布，不叠在同一点。
  const outCount = new Map<number, number>()
  const inCount = new Map<number, number>()
  for (const link of links.value) {
    outCount.set(link.from, (outCount.get(link.from) ?? 0) + 1)
    inCount.set(link.to, (inCount.get(link.to) ?? 0) + 1)
  }
  const outSeen = new Map<number, number>()
  const inSeen = new Map<number, number>()

  const list: Edge[] = []
  links.value.forEach((link, serial) => {
    const fromEl = elAt.get(link.from)
    const toEl = elAt.get(link.to)
    if (!(fromEl instanceof HTMLElement) || !(toEl instanceof HTMLElement)) return
    const a = fromEl.getBoundingClientRect()
    const b = toEl.getBoundingClientRect()
    const x1 = (a.left - base.left) / s
    const x2 = (b.left - base.left) / s
    const w1 = a.width / s
    const w2 = b.width / s
    const y1 = (a.bottom - base.top) / s
    const y2 = (b.top - base.top) / s
    // 端口位置：n 条边把边分成 n+1 段，第 i 条取第 i 个分点（单条边即中点）。
    const pi = outSeen.get(link.from) ?? 0
    const qi = inSeen.get(link.to) ?? 0
    outSeen.set(link.from, pi + 1)
    inSeen.set(link.to, qi + 1)
    const sx = x1 + (w1 * (pi + 1)) / ((outCount.get(link.from) ?? 1) + 1)
    const tx = x2 + (w2 * (qi + 1)) / ((inCount.get(link.to) ?? 1) + 1)

    const seqFrom = stepGroupSeq.value[link.from] ?? 0
    const seqTo = stepGroupSeq.value[link.to] ?? 0
    const yDown = busY(seqFrom)
    const yUp = busY(seqTo - 1)
    let pts: Array<[number, number]>
    if (seqTo === seqFrom + 1) {
      // 相邻组：只走中间一条总线；列对齐（sx≈tx）时整条路退化成竖直直线。
      pts = [[sx, y1], [sx, yDown], [tx, yDown], [tx, y2]]
    } else {
      // 跳组：下到源组下总线 → 左侧主干轨纵走 → 从目标组上总线右行进入。
      const rail = 8 + (serial % 3) * 4
      pts = [
        [sx, y1],
        [sx, yDown],
        [rail, yDown],
        [rail, yUp],
        [tx, yUp],
        [tx, y2],
      ]
    }
    list.push({
      d: roundedPath(pts, 6),
      from: link.from,
      to: link.to,
      variable: link.variable,
      // 变量名仅悬停时显示：相邻组标在走线旁空白里，跳组标在主干段中点。
      labelX:
        seqTo === seqFrom + 1 ? Math.min(sx, tx) + 6 : 8 + (serial % 3) * 4 + 3,
      labelY: seqTo === seqFrom + 1 ? (y1 + y2) / 2 : (yDown + yUp) / 2,
    })
  })
  edges.value = list
}

/**
 * 高亮跟着列表切到视野外的节点：把它滚进来。port 与 node 的屏幕 rect、holder 的
 * 滚动坐标同为缩放后像素，差值公式直接成立，不用除 scale。横竖两个方向都处理。
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
  if (box.bottom > port.bottom - gap) scroller.scrollTop += box.bottom - port.bottom + gap
  else if (box.top < port.top + gap) scroller.scrollTop -= port.top + gap - box.top
}

/**
 * 外部触发重测：弹窗动画结束（afterOpen）后容器尺寸才落定，由父级调一次，
 * 确保自然尺寸与引用线都是对的。
 */
function refresh() {
  measure()
  if (zoomMode.value === 'fit') scale.value = fitScale()
}
defineExpose({ refresh })

let viewportObserver: ResizeObserver | null = null
let contentObserver: ResizeObserver | null = null

onMounted(() => {
  // 纵向流默认原尺寸：不主动 fit，组多就往下滚。
  void nextTick(measure)
  if (scrollEl.value && typeof ResizeObserver !== 'undefined') {
    // 视口宽变化（窗口、面板）：fit 档重算系数；manual 不动。
    viewportObserver = new ResizeObserver(() => {
      if (zoomMode.value === 'fit') scale.value = fitScale()
    })
    viewportObserver.observe(scrollEl.value)
  }
  if (contentEl.value && typeof ResizeObserver !== 'undefined') {
    // 内容自然尺寸变化（增删步骤、改组名）：重画 → fit 档再重算。
    contentObserver = new ResizeObserver(() => {
      measure()
      if (zoomMode.value === 'fit') scale.value = fitScale()
    })
    contentObserver.observe(contentEl.value)
  }
})

onBeforeUnmount(() => {
  viewportObserver?.disconnect()
  contentObserver?.disconnect()
  viewportObserver = null
  contentObserver = null
})

// keep-alive 切标签回来：几何可能被期间的增删改动，补一次测量。
onActivated(() => {
  if (collapsed.value) return
  void nextTick(() => {
    measure()
    if (zoomMode.value === 'fit') scale.value = fitScale()
  })
})

// 增删步骤、改组名、改模板都会让线重算。
watch(
  () =>
    props.steps
      .map((step) => `${step.group_key}|${step.key}|${step.output_var}|${step.prompt_tpl}`)
      .join('\n'),
  () => void nextTick(measure),
)

watch(
  () => props.active,
  () => {
    if (zoomMode.value === 'manual') void nextTick(revealActive)
  },
)
</script>

<style scoped>
.canvas-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.canvas-title {
  font-size: var(--fs-sm);
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.canvas-zoom-pct {
  min-width: 42px;
  text-align: center;
  color: var(--text-3);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}
/* 全屏按钮与缩放控件同行：青绿色描边，保证一眼能找到。 */
.canvas-fs-btn {
  color: var(--signal-text);
  border-color: var(--signal-border);
}
.canvas-fs-btn:hover {
  color: var(--signal-text);
  border-color: var(--signal-text);
}
/* 折叠按钮箭头朝左旋转，与 v-show 的 body 同进同退。 */
.canvas-head :deep(.ant-btn) {
  transition: transform 0.15s ease;
}
.canvas-head :deep(.ant-btn.collapsed) {
  transform: rotate(-90deg);
}
.canvas-hint {
  color: var(--text-3);
  font-size: var(--fs-xs);
  margin-bottom: 8px;
}
.canvas-scroll {
  overflow: auto;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--surface);
  padding: 12px;
}
.canvas-holder {
  position: relative;
  /* 内容比视口窄时整体居中；超出时 margin 自动归零，滚动不受影响。 */
  margin-inline: auto;
}
.canvas-scale {
  width: max-content;
  height: max-content;
  transform-origin: top left;
}
.canvas-content {
  position: relative;
  display: flex;
  flex-direction: column;
  /* 左侧 26px：跳组引用的主干轨走这里；右侧对称留白，居中时视觉不偏。 */
  padding-left: 26px;
  padding-right: 26px;
  min-width: max-content;
}
.canvas-edges {
  position: absolute;
  top: 0;
  left: 0;
  z-index: var(--z-base);
  pointer-events: none;
}
.edge {
  fill: none;
  stroke: var(--text-3);
  stroke-width: 1.4;
  opacity: 0.5;
  transition: opacity 0.15s ease, stroke 0.15s ease, stroke-width 0.15s ease;
}
.edge-hot {
  stroke: var(--signal);
  stroke-width: 2;
  opacity: 1;
}
.edge-dim {
  opacity: 0.12;
}
.edge-head {
  fill: var(--text-3);
  opacity: 0.6;
}
/* 变量名默认不显示，只在该线 edge-hot 时出现；借 surface 底色描边保证可读。 */
.edge-label {
  display: none;
  fill: var(--signal-text);
  font-size: 10px;
  font-family: var(--font-mono);
  paint-order: stroke;
  stroke: var(--surface);
  stroke-width: 3px;
}
.edge-label.edge-hot {
  display: inline;
}
.mono {
  font-family: var(--font-mono);
}

/* ---- 输入行 ---- */
.io-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.io-label {
  width: 88px;
  flex: none;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.io-chips {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
}
.input-chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text-2);
  font-size: var(--fs-xs);
  padding: 1px 6px;
}

/* ---- 顺序箭头：只表示组间执行顺序，与走线轨里的引用线分开 ---- */
.flow-arrow {
  /* content 中心右移 head 的一半（44px）= 节点区域中心，与居中后的节点对齐。 */
  align-self: center;
  margin: 24px 0 24px 44px;
  color: var(--text-3);
  font-size: var(--fs-xs);
  line-height: 1;
}

/* ---- 并行组行 ---- */
.group-row {
  display: flex;
  align-items: flex-start;
}
.group-head {
  width: 88px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: 4px;
}
.group-name {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-meta {
  font-size: var(--fs-xs);
  color: var(--text-3);
  white-space: nowrap;
}
.group-body {
  flex: 1;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  gap: 14px;
}

/* ---- 节点卡片 ---- */
.wf-node {
  width: 260px;
  flex: none;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--surface);
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.wf-node:hover {
  border-color: var(--signal-border);
}
.node-hot {
  border-color: var(--signal-border);
  box-shadow: 0 0 0 1px var(--signal-border);
}
.node-active {
  border-color: var(--signal);
  box-shadow: 0 0 0 1px var(--signal-border);
}
.node-problem {
  border-color: var(--danger);
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
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-3);
}
.node-key {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-badge {
  margin-left: auto;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  color: var(--text-3);
  font-size: var(--fs-xs);
  line-height: 1.5;
  padding: 0 4px;
}
.node-badge-opt {
  margin-left: 0;
  border-color: var(--warning);
  color: var(--warning);
}
.node-model-ic {
  color: var(--signal);
  font-size: var(--fs-xs);
}
.node-agent {
  margin-top: 6px;
  color: var(--text-2);
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-prompt {
  margin-top: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-word;
}
.node-section-lbl {
  flex: none;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.node-refs,
.node-out {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 6px;
  margin-top: 8px;
}
.mini-chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text-2);
  font-size: var(--fs-xs);
  line-height: 1.6;
  padding: 1px 5px;
}
.node-out-name {
  color: var(--text-2);
  font-size: var(--fs-xs);
}

/* ---- 悬停详情浮层（popover teleport 到 body，scoped 属性仍跟着元素走） ---- */
.node-detail {
  width: 320px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.nd-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.nd-key {
  font-size: var(--fs-sm);
  font-weight: 600;
}
.nd-no {
  margin-left: auto;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.nd-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
}
.nd-col {
  flex-direction: column;
  align-items: flex-start;
}
.nd-k {
  flex: none;
  width: 40px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.nd-col .nd-k {
  width: auto;
}
.nd-v {
  color: var(--text-2);
  font-size: var(--fs-xs);
}
.nd-sub {
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.nd-prompt {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.nd-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.nd-chip {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text-2);
  font-size: var(--fs-xs);
  line-height: 1.6;
  padding: 0 6px;
}
.nd-opt {
  margin: 0;
}
</style>
