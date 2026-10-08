<template>
  <!-- 深色右键菜单（终端页共用）：定位在点击处，点击外部/Esc/滚动/改窗口即收。 -->
  <Teleport to="body">
    <div
      v-if="open"
      ref="menuEl"
      class="dmenu"
      :style="{ left: `${left}px`, top: `${top}px` }"
      @contextmenu.prevent.stop
    >
      <template v-for="(item, i) in items" :key="item.key">
        <div v-if="i > 0 && item.group !== items[i - 1].group" class="dmenu-sep" />
        <button
          type="button"
          :class="['dmenu-item', { danger: item.danger }]"
          :aria-disabled="item.disabled ? 'true' : undefined"
          :title="item.disabled ? item.disabledHint : ''"
          @click="onPick(item)"
        >
          <component :is="item.icon" v-if="item.icon" class="dmenu-icon" />
          <span class="dmenu-label">{{ item.label }}</span>
          <span v-if="item.target" class="dmenu-target" :title="item.targetTitle">{{
            item.target
          }}</span>
        </button>
      </template>
    </div>
  </Teleport>
</template>

<script lang="ts">
// 这份登记必须是模块级的：<script setup> 的函数体每个实例跑一遍，
// 放在那儿就是「各记各的」，两个菜单会同时挂在页面上。
let activeCloser: (() => void) | null = null
</script>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'

export interface DarkMenuItem {
  key: string
  label: string
  icon?: any
  /** 分组号：与上一条不同就插一道分隔线。 */
  group?: number
  danger?: boolean
  disabled?: boolean
  /** 置灰原因。点下去会 toast 出来，见 onPick。 */
  disabledHint?: string
  /** 行尾的灰色提示（粘贴/上传的去向目录名）。 */
  target?: string
  targetTitle?: string
}

const props = withDefaults(
  defineProps<{
    open: boolean
    x: number
    y: number
    items: DarkMenuItem[]
  }>(),
  { items: () => [] },
)

const emit = defineEmits<{
  (e: 'pick', key: string): void
  (e: 'close'): void
}>()

const menuEl = ref<HTMLElement | null>(null)
const left = ref(0)
const top = ref(0)

// 同一时刻只该有一个菜单活着：新开的先收旧的（activeCloser 在模块作用域）。
function closeSelf() {
  emit('close')
}

// 每次打开都重新定位：菜单还开着就换目标（右键另一个条目）是常态。
watch(
  () => [props.open, props.x, props.y] as const,
  ([open, x, y]) => {
    if (!open) {
      if (activeCloser === closeSelf) activeCloser = null
      return
    }
    if (activeCloser && activeCloser !== closeSelf) activeCloser()
    activeCloser = closeSelf
    left.value = x
    top.value = y
    // 高度要渲染出来才量得到，贴边翻转放到 nextTick。
    void nextTick(fit)
  },
  { immediate: true },
)

/** 贴近视口右缘/下缘时把菜单拉回来，别溢出屏幕。 */
function fit() {
  const rect = menuEl.value?.getBoundingClientRect()
  if (!rect) return
  const pad = 6
  left.value = Math.max(pad, Math.min(left.value, window.innerWidth - rect.width - pad))
  if (top.value + rect.height > window.innerHeight - pad) {
    top.value = Math.max(pad, window.innerHeight - rect.height - pad)
  }
}

function onPick(item: DarkMenuItem) {
  // 置灰项用 aria-disabled 而不是 disabled：真 disabled 的按钮既收不到点击也不弹
  // title，那句「为什么不能点」就永远没人看见。所以菜单留着可点，点下去把原因讲
  // 出来，菜单不收——用户通常是想确认一下到底缺什么权限。
  if (item.disabled) {
    if (item.disabledHint) message.warning(item.disabledHint)
    return
  }
  emit('pick', item.key)
}

// 关闭条件全在这里：左/中键点在菜单外、Esc、滚动、视口变向。右键不关——
// 菜单开着再右键另一个位置是改目标，不是改主意，让调用方的 contextmenu  handler
// 更新坐标即可（菜单元素复用，不会重挂载）。
function onDocMouseDown(e: MouseEvent) {
  if (e.button !== 0 && e.button !== 1) return
  if (props.open && !menuEl.value?.contains(e.target as Node)) emit('close')
}

function onDocKeyDown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !props.open) return
  // 焦点在真正的输入框里（终端的搜索框就是）时，Esc 归那个框，菜单不抢。
  if (e.target instanceof HTMLInputElement) return
  // 菜单开着时 Esc 归菜单。这里拦下它是因为消费者可能是终端——
  // 放行就等于关菜单的同时又给远端 shell 发了一个 Escape。
  e.stopPropagation()
  e.preventDefault()
  emit('close')
}

// capture 才收得到各层滚动容器的滚动（菜单挂在 body，冒泡阶段的 scroll 到不了这儿）。
function onDocScroll() {
  if (props.open) emit('close')
}

onMounted(() => {
  document.addEventListener('mousedown', onDocMouseDown)
  document.addEventListener('keydown', onDocKeyDown, true)
  document.addEventListener('scroll', onDocScroll, true)
  window.addEventListener('resize', onDocScroll)
})

onBeforeUnmount(() => {
  if (activeCloser === closeSelf) activeCloser = null
  document.removeEventListener('mousedown', onDocMouseDown)
  document.removeEventListener('keydown', onDocKeyDown, true)
  document.removeEventListener('scroll', onDocScroll, true)
  window.removeEventListener('resize', onDocScroll)
})
</script>

<style>
/* teleport 到 body 后拿不到 scope id，样式必须非 scoped（与 GridContextMenu 同理）。
   这里是终端页那一族的深色：底 #16202a、发丝线 var(--on-dark-hover)。 */
.dmenu {
  position: fixed;
  z-index: var(--z-overlay);
  min-width: 172px;
  padding: 3px;
  border: 1px solid var(--on-dark-border);
  border-radius: var(--tool-radius);
  background: #16202a;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.5);
}

.dmenu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 5px 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dark);
  font-size: var(--fs-xs);
  text-align: left;
  white-space: nowrap;
  cursor: pointer;
}

.dmenu-item:hover:not([aria-disabled='true']) {
  background: rgba(58, 214, 222, 0.14);
}

.dmenu-item.danger {
  color: var(--danger-soft);
}

.dmenu-item[aria-disabled='true'] {
  color: #5b666f;
  cursor: not-allowed;
}

.dmenu-icon {
  flex-shrink: 0;
  width: 14px;
  color: inherit;
}

.dmenu-label {
  flex-shrink: 0;
}

.dmenu-target {
  min-width: 0;
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #6e7a86;
  font-family: var(--font-mono);
}

.dmenu-sep {
  height: 1px;
  margin: 3px 6px;
  background: var(--on-dark-hover);
}
</style>
