<template>
  <!-- 表格单元格的右键菜单：绝对定位的两级小菜单，点击任意处关闭。 -->
  <Teleport to="body">
    <div v-if="open" class="gmenu-mask" @click="emit('close')" @contextmenu.prevent="emit('close')">
      <div class="gmenu" :style="{ left: `${x}px`, top: `${y}px` }" @click.stop>
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

            <!-- 二级菜单：贴在一级项右侧，底部可能出屏时向上贴齐。 -->
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
import { ref, watch } from 'vue'
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

// 每次重开菜单都收回悬停态，否则上次展开的子菜单会残留在新位置。
watch(
  () => props.open,
  (open) => {
    if (open) hoverKey.value = ''
  },
)

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
  z-index: 1000;
}

.gmenu {
  position: fixed;
  min-width: 168px;
  padding: 4px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--tool-radius);
  box-shadow:
    0 6px 16px 0 rgba(0, 0, 0, 0.08),
    0 3px 6px -4px rgba(0, 0, 0, 0.12),
    0 9px 28px 8px rgba(0, 0, 0, 0.05);
}

.gmenu-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--tool-gap);
  padding: 5px var(--grid-pad-inline);
  border-radius: var(--radius-sm);
  font-size: 13px;
  color: rgba(0, 0, 0, 0.88);
  cursor: pointer;
  white-space: nowrap;
}

.gmenu-item:hover {
  background: var(--grid-hover);
}

.gmenu-item.disabled {
  color: rgba(0, 0, 0, 0.25);
  cursor: not-allowed;
}

.gmenu-item.danger {
  color: #ff4d4f;
}

.gmenu-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}

.gmenu-arrow {
  font-size: 10px;
  color: var(--text-3);
}

.gmenu-divider {
  height: 1px;
  margin: 4px 8px;
  background: rgba(5, 5, 5, 0.06);
}

.gmenu-sub {
  position: absolute;
  left: 100%;
  top: -4px;
  max-height: 60vh;
  overflow-y: auto;
}
</style>
