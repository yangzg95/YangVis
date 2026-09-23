<template>
  <div class="sidebar-brand">
    <img class="sidebar-logo-w" :src="yangvisLogo" alt="杨维斯控制台" />
  </div>
  <div class="sidebar-menu-shell">
    <div v-if="!collapsed" class="sidebar-menu-search">
      <a-input
        ref="menuSearchRef"
        v-model:value="menuKeyword"
        allow-clear
        class="sidebar-menu-search-input"
        :placeholder="searchPlaceholder"
      >
        <template #prefix><SearchOutlined /></template>
      </a-input>
    </div>
    <a-menu
      v-if="filteredMenu.length"
      mode="inline"
      theme="dark"
      class="sidebar-menu"
      :inline-collapsed="collapsed"
      :selected-keys="[route.path]"
      :open-keys="effectiveOpenKeys"
      @click="onMenuClick"
      @openChange="onOpenChange"
    >
      <template v-for="node in filteredMenu" :key="node.path">
        <a-sub-menu v-if="isGroup(node)" :key="node.path">
          <template #icon><component :is="node.icon" /></template>
          <template #title>
            <span class="sidebar-menu-label">{{ node.title }}</span>
          </template>
          <a-menu-item v-for="child in node.children" :key="child.path">
            <span class="sidebar-menu-label">{{ child.title }}</span>
          </a-menu-item>
        </a-sub-menu>
        <a-menu-item v-else :key="node.path">
          <template #icon><component :is="node.icon" /></template>
          <span class="sidebar-menu-label">{{ node.title }}</span>
        </a-menu-item>
      </template>
    </a-menu>
    <div v-else class="sidebar-menu-empty">
      <a-empty :image="Empty.PRESENTED_IMAGE_SIMPLE" description="没有匹配的菜单" />
    </div>
  </div>
</template>

<script lang="ts">
export interface MenuChild {
  path: string
  title: string
  /** 可选；缺省时标签页会回退用父级的图标。 */
  icon?: unknown
  /** 只对管理员显示。真正的权限判定在后端。 */
  adminOnly?: boolean
}

export interface MenuNode {
  path: string
  title: string
  icon: unknown
  children?: MenuChild[]
}
</script>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { SearchOutlined } from '@ant-design/icons-vue'
import { Empty } from 'ant-design-vue'
import { useAuthStore } from '@/stores/auth'
import yangvisLogo from '@/assets/yangvis-logo.svg'

// 导航内容从 App.vue 抽成独立组件：桌面端放在 a-layout-sider 里（可收起），
// 手机端放在抽屉里（永远展开）——两份壳，一份菜单。
const props = withDefaults(
  defineProps<{
    menu: MenuNode[]
    /** 收起态只有桌面导航轨会用；抽屉里始终传 false。 */
    collapsed?: boolean
    searchPlaceholder?: string
  }>(),
  { collapsed: false, searchPlaceholder: '快捷搜索菜单（Ctrl+K）' },
)

const emit = defineEmits<{ select: [path: string] }>()

const route = useRoute()
const auth = useAuthStore()

const menuKeyword = ref('')
const menuSearchRef = ref()

const isGroup = (node: MenuNode): boolean => Boolean(node.children?.length)

// 先按权限裁剪，再按关键字过滤：搜索不应该把管理员专属的菜单搜出来。
const visibleMenu = computed<MenuNode[]>(() =>
  props.menu.reduce<MenuNode[]>((acc, node) => {
    if (!node.children?.length) {
      acc.push(node)
      return acc
    }
    const children = node.children.filter((child) => !child.adminOnly || auth.isAdmin)
    if (children.length) acc.push({ ...node, children })
    return acc
  }, []),
)

const filteredMenu = computed<MenuNode[]>(() => {
  const keyword = menuKeyword.value.trim().toLowerCase()
  if (!keyword) {
    return visibleMenu.value
  }
  const matches = (text: string) => text.toLowerCase().includes(keyword)
  return visibleMenu.value.reduce<MenuNode[]>((acc, node) => {
    if (!node.children?.length) {
      if (matches(node.title)) acc.push(node)
      return acc
    }
    // 分组自身命中时保留它的全部子项；否则只保留命中的那些子项。
    if (matches(node.title)) {
      acc.push(node)
      return acc
    }
    const children = node.children.filter((child) => matches(child.title))
    if (children.length) acc.push({ ...node, children })
    return acc
  }, [])
})

const openKeys = ref<string[]>([])
// 收起态的悬停弹层也吃 openKeys：单独存一份，和展开态的分组展开状态隔开，
// 否则路由联动往 openKeys 里加分组时会把弹层顶出来（切 tab 菜单自己弹出）。
const collapsedHoverKeys = ref<string[]>([])
// 搜索过程中强制展开所有留下来的分组，保证命中项一直可见。
const effectiveOpenKeys = computed(() => {
  if (props.collapsed) return collapsedHoverKeys.value
  return menuKeyword.value.trim()
    ? filteredMenu.value.filter(isGroup).map((node) => node.path)
    : openKeys.value
})

watch(
  () => route.path,
  (path) => {
    // 展开当前页面所属的那个分组（手风琴：同时只展开一个一级菜单）。
    // 收起态不动 openKeys：弹层只跟 hover 走，否则切 tab 会把子菜单弹层顶出来。
    const parent = props.menu.find((node) =>
      node.children?.some((child) => child.path === path),
    )
    if (parent && !props.collapsed && !openKeys.value.includes(parent.path)) {
      openKeys.value = [parent.path]
    }
  },
  { immediate: true },
)

const onMenuClick = ({ key }: { key: string | number }) => {
  emit('select', String(key))
}

const onOpenChange = (keys: (string | number)[]) => {
  // 收起态的 openChange 来自弹层 hover，展开态的来自分组开合，分开记账。
  if (props.collapsed) {
    collapsedHoverKeys.value = keys.map(String)
    return
  }
  // 手风琴：新展开一个分组时收起其余分组；只是收起分组则照常记账。
  const next = keys.map(String)
  const added = next.find((key) => !openKeys.value.includes(key))
  openKeys.value = added ? [added] : next
}

// Ctrl+K 快捷键从 App.vue 调进来。
const focusSearch = () => {
  menuSearchRef.value?.focus()
}
defineExpose({ focusSearch })
</script>
