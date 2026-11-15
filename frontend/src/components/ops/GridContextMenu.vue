<template>
  <!-- 表格单元格的右键菜单：绝对定位的两级小菜单，点击任意处关闭。 -->
  <Teleport to="body">
    <div v-if="open" class="gmenu-mask" @click="emit('close')" @contextmenu.prevent="emit('close')">
      <div
        ref="menuEl"
        :class="['gmenu', { 'flip-sub-x': flipSubX }]"
        :style="{ left: `${left}px`, top: `${top}px`, '--sub-max-h': `${subMaxH}px` }"
        @click.stop
      >
        <template v-for="entry in entries" :key="entry.key">
          <div v-if="entry.divider" class="gmenu-divider" />
          <div
            v-else
            :class="['gmenu-item', { disabled: entry.disabled, danger: entry.danger }]"
            @mouseenter="hoverKey = entry.children?.length ? entry.key : ''"
            @click="onPick(entry)"
          >
            <span class="gmenu-label">{{ entry.label }}</span>
            <RightOutlined v-if="entry.children?.length" class="gmenu-arrow" />

            <!-- 二级菜单：贴在一级项右侧；出屏方向由 fit() 算出的
                 flip-sub-x（改为向左开）和 --sub-max-h（限高内部滚动）兜住。 -->
            <div
              v-if="entry.children?.length && hoverKey === entry.key"
              class="gmenu gmenu-sub"
            >
              <template v-for="child in entry.children" :key="child.key">
                <div v-if="child.divider" class="gmenu-divider" />
                <div
                  v-else
                  :class="['gmenu-item', { disabled: child.disabled, danger: child.danger }]"
                  @click.stop="onPick(child)"
                >
                  <span class="gmenu-label">{{ child.label }}</span>
                </div>
              </template>
            </div>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { RightOutlined } from '@ant-design/icons-vue'

export interface GridMenuEntry {
  key: string
  label?: string
  danger?: boolean
  disabled?: boolean
  divider?: boolean
  children?: GridMenuEntry[]
}

const props = withDefaults(
  defineProps<{
    open: boolean
    x: number
    y: number
    entries: GridMenuEntry[]
  }>(),
  { entries: () => [] },
)

const emit = defineEmits<{
  (e: 'pick', key: string): void
  (e: 'close'): void
}>()

const hoverKey = ref('')

// 菜单实际落点：初值取点击坐标，打开后由 fit() 按视口边缘修正。
const menuEl = ref<HTMLElement | null>(null)
const left = ref(props.x)
const top = ref(props.y)
const flipSubX = ref(false)
const subMaxH = ref(window.innerHeight)

// 每次重开菜单都收回悬停态，否则上次展开的子菜单会残留在新位置。
watch(
  () => props.open,
  (open) => {
    if (open) hoverKey.value = ''
  },
)

// 打开或改目标（菜单开着右键另一个单元格）都重新定位；
// 菜单尺寸要渲染出来才量得到，贴边修正放到 nextTick（与 DarkContextMenu 同一做法）。
watch(
  () => [props.open, props.x, props.y] as const,
  ([open, x, y]) => {
    if (!open) return
    left.value = x
    top.value = y
    void nextTick(fit)
  },
)

/**
 * 视口贴边修正：右键点在靠下几排时，原始坐标会让菜单被屏幕下沿切掉，
 * 这里把它拉回视口内；右缘同理，并顺带决定二级菜单的开合方向与限高。
 */
function fit() {
  const el = menuEl.value
  if (!el) return
  const pad = 6
  const rect = el.getBoundingClientRect()
  left.value = Math.max(pad, Math.min(left.value, window.innerWidth - rect.width - pad))
  if (top.value + rect.height > window.innerHeight - pad) {
    top.value = Math.max(pad, window.innerHeight - rect.height - pad)
  }
  // 横向上修正后要重新量：主菜单已贴近右缘时，二级菜单改为向左开。
  const placed = el.getBoundingClientRect()
  flipSubX.value = window.innerWidth - placed.right < 180
  // 二级菜单顶边不会高于主菜单顶边，限高取主菜单顶边到视口底的剩余距离，
  // 超出部分走子菜单自身滚动（.gmenu-sub 已有 overflow-y: auto），不再被裁切。
  subMaxH.value = Math.max(120, window.innerHeight - placed.top - pad)
}

function onPick(entry: GridMenuEntry) {
  if (entry.disabled || entry.children?.length) return
  emit('pick', entry.key)
  emit('close')
}
</script>

<style>
/* 与树节点菜单同理：teleport 到 body 后拿不到 scope id，样式必须非 scoped。 */
.gmenu-mask {
  position: fixed;
  inset: 0;
  z-index: var(--z-overlay);
}

.gmenu {
  position: fixed;
  min-width: 168px;
  padding: 4px;
  background: var(--surface-raised);
  border: 1px solid var(--hairline);
  border-radius: var(--tool-radius);
  box-shadow: var(--shadow-overlay);
}

.gmenu-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--tool-gap);
  padding: 5px var(--grid-pad-inline);
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
  color: var(--text-1);
  cursor: pointer;
  white-space: nowrap;
}

.gmenu-item:hover {
  background: var(--grid-hover);
}

.gmenu-item.disabled {
  color: var(--disabled-text);
  cursor: not-allowed;
}

.gmenu-item.danger {
  color: var(--danger);
}

.gmenu-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}

.gmenu-arrow {
  font-size: var(--fs-xs);
  color: var(--text-3);
}

.gmenu-divider {
  height: 1px;
  margin: 4px 8px;
  background: var(--hairline);
}

.gmenu-sub {
  position: absolute;
  left: 100%;
  top: -4px;
  max-height: var(--sub-max-h, 60vh);
  overflow-y: auto;
}

/* 主菜单贴近视口右缘时二级菜单改向左侧展开，避免右侧出屏。 */
.gmenu.flip-sub-x .gmenu-sub {
  left: auto;
  right: 100%;
}
</style>
