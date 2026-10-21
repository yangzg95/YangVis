<template>
  <!-- auto-insert-space-in-button 关掉：AntD 默认会在「删除」这类纯两字按钮
       中间塞一个空格，密集列表里看着像排版坏了。 -->
  <a-config-provider :locale="zhCN" :auto-insert-space-in-button="false" :theme="theme">
    <router-view v-if="isBare" />
    <a-layout v-else class="dashboard-layout">
      <!-- 手机端（<768px）导航轨整根拔掉，换成顶栏汉堡钮唤出的抽屉。 -->
      <a-layout-sider
        v-if="!isMobile"
        :collapsed="effectiveCollapsed"
        collapsible
        :width="200"
        :collapsed-width="64"
        class="dashboard-sider"
        theme="dark"
        breakpoint="lg"
        @collapse="onManualCollapse"
        @breakpoint="onSiderBreakpoint"
      >
        <SidebarNav
          ref="sidebarNavRef"
          :collapsed="effectiveCollapsed"
          :menu="MENU"
          @select="onMenuSelect"
        />
      </a-layout-sider>
      <a-layout class="dashboard-content-shell">
        <!-- 顶栏单行：标签页 + 用户区。不设面包屑——它和激活标签表达同一件事，
             属于重复信息；标签多了靠 tabs 自带的滚动箭头翻页。 -->
        <div class="global-tabbar">
          <div class="topbar-main">
            <button
              v-if="isMobile"
              type="button"
              class="icon-btn nav-drawer-trigger"
              aria-label="打开导航菜单"
              @click="drawerOpen = true"
            >
              <MenuOutlined />
            </button>
            <a-tabs
              :active-key="activeTabKey"
              hide-add
              :animated="false"
              :tab-bar-gutter="8"
              type="editable-card"
              class="header-tablist"
              @change="onTabChange"
              @edit="onTabEdit"
              @contextmenu="onTabBarContextMenu"
            >
              <a-tab-pane
                v-for="item in openTabs"
                :key="item.path"
                :closable="item.path !== DEFAULT_TAB_PATH"
              >
                <template #tab>
                  <span class="header-tab-label">
                    <component :is="item.icon" />
                    <span>{{ item.title }}</span>
                  </span>
                </template>
              </a-tab-pane>
            </a-tabs>
            <GridContextMenu
              :open="tabMenu.open"
              :x="tabMenu.x"
              :y="tabMenu.y"
              :entries="tabMenuEntries"
              @pick="onTabMenuPick"
              @close="tabMenu.open = false"
            />
            <div class="topbar-user">
              <a-dropdown placement="bottomRight" :trigger="['click']">
                <button type="button" class="user-dropdown-trigger">
                  <a-avatar :size="32" class="user-avatar">
                    <template #icon><UserOutlined /></template>
                  </a-avatar>
                  <span class="user-name">{{ auth.displayName }}</span>
                </button>
                <template #overlay>
                  <a-menu @click="onUserMenuClick">
                    <a-menu-item key="logout">
                      <template #icon><LogoutOutlined /></template>
                      退出登录
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
            </div>
          </div>
        </div>
        <a-layout-content class="dashboard-main">
          <router-view v-slot="{ Component }">
            <transition name="fade" mode="out-in">
              <!-- keep-alive：顶栏多标签的语义就是「页面一直开着」——切标签不该
                   丢掉对话草稿、数据库工作台的页签和树展开态，也不该每次切回都
                   重发一遍初始化请求。有轮询的页面自己在 onDeactivated 里停表。 -->
              <keep-alive>
                <component :is="Component" />
              </keep-alive>
            </transition>
          </router-view>
        </a-layout-content>
      </a-layout>
      <!-- 手机端导航抽屉：和导航轨同一套菜单（SidebarNav），永远展开态。
           点菜单、点遮罩、点右上角关闭钮都能收。 -->
      <!-- 自定义类名必须走 rootClassName：antd-vue 的 Drawer 是 inheritAttrs:false
           且根节点是 Teleport，普通 class 会被丢弃，抽屉的 ink 底色会失效。 -->
      <a-drawer
        v-model:open="drawerOpen"
        placement="left"
        :width="240"
        :closable="false"
        root-class-name="mobile-nav-drawer"
      >
        <button
          type="button"
          class="icon-btn mobile-nav-close"
          aria-label="关闭导航菜单"
          @click="drawerOpen = false"
        >
          <CloseOutlined />
        </button>
        <SidebarNav
          ref="drawerNavRef"
          :menu="MENU"
          search-placeholder="搜索菜单"
          @select="onMenuSelect"
        />
      </a-drawer>
    </a-layout>
  </a-config-provider>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ApiOutlined,
  AppstoreOutlined,
  BookOutlined,
  BuildOutlined,
  CloseOutlined,
  CloudServerOutlined,
  CodeOutlined,
  DatabaseOutlined,
  FundProjectionScreenOutlined,
  HomeOutlined,
  LogoutOutlined,
  MenuOutlined,
  MessageOutlined,
  PartitionOutlined,
  ProfileOutlined,
  RobotOutlined,
  SettingOutlined,
  SyncOutlined,
  FunctionOutlined,
  TeamOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import SidebarNav, { type MenuNode } from '@/components/SidebarNav.vue'
import GridContextMenu, { type GridMenuEntry } from '@/components/ops/GridContextMenu.vue'
import { useAuthStore } from '@/stores/auth'
import { leafForPath as resolveTabLeaf } from '@/utils/appTabs'
import { storageKeys } from '@/utils/storage'
import { useIsMobile } from '@/utils/useIsMobile'

/** 一个可跳转的页面：MENU 里所有叶子节点摊平后的结果。 */
interface MenuLeaf {
  path: string
  title: string
  icon: unknown
}

// 分组按「配置侧 / 使用侧」划：AI 构建装的是每个业务页都要先备好的原料与编排
// （智能体的人设与工具开关、工作流的流水线、知识库的文档与索引），AI 应用装的是
// 选一个智能体跑出结果的业务页。智能体不再单列一级——它和知识库、工作流同属配置侧，
// 放在一起反而更容易看懂它为什么被所有业务页复用。
// 分组的 path 只当 a-menu 的 key 用，永远不会被导航；/build、/apps 因此不是路由。
// 注意：叶子仍保留各自的历史前缀（/knowledge、/office/*），组 key 不再与路由前缀
// 一一对应，SidebarNav 的手风琴与图标回退都只读 MENU，不依赖前缀。
const MENU: MenuNode[] = [
  { path: '/', title: '首页', icon: HomeOutlined },
  {
    path: '/build',
    title: 'AI 构建',
    icon: BuildOutlined,
    children: [
      { path: '/agents', title: '智能体', icon: RobotOutlined },
      { path: '/workflows', title: '工作流', icon: PartitionOutlined },
      { path: '/mcp', title: 'MCP 服务', icon: ToolOutlined },
      { path: '/tools/local', title: '本地工具', icon: FunctionOutlined },
      { path: '/knowledge', title: '知识库', icon: BookOutlined },
    ],
  },
  {
    path: '/apps',
    title: 'AI 应用',
    icon: AppstoreOutlined,
    children: [
      { path: '/chat', title: '对话', icon: MessageOutlined },
      { path: '/office/resume', title: '简历', icon: ProfileOutlined },
      { path: '/office/slides', title: '幻灯片', icon: FundProjectionScreenOutlined },
    ],
  },
  {
    path: '/ops',
    title: '运维',
    icon: CloudServerOutlined,
    children: [
      { path: '/ops/server', title: '服务器' },
      { path: '/ops/database', title: '数据库', icon: DatabaseOutlined },
    ],
  },
  {
    // 面向开发流程的工具，不产生运行结果，所以单独成组而不是塞进 AI 构建。
    path: '/devtools',
    title: '开发工具',
    icon: CodeOutlined,
    children: [
      { path: '/devtools/yapi', title: 'YApi 同步', icon: SyncOutlined },
    ],
  },
  {
    path: '/system',
    title: '系统设置',
    icon: SettingOutlined,
    children: [
      { path: '/system/settings', title: '模型与集成' },
      { path: '/system/ai-gateway', title: 'AI 网关', icon: ApiOutlined, adminOnly: true },
      { path: '/system/users', title: '用户管理', icon: TeamOutlined, adminOnly: true },
    ],
  },
]

// 全局主题 token。注意 antd-vue 4.2.6 的坑（详见 style.css 头注）：
// - Menu/Table 用旧 token 命名，且没有 dark 菜单专属 token —— sider 深色菜单
//   的颜色在 style.css 里覆盖，这里 components.Menu 只放主题无关项，否则会
//   污染顶栏用户下拉的 light 菜单。
// - Button 组件 token 是空接口，主按钮 ink 化也在 style.css 里做。
const theme = {
  hashed: false,
  token: {
    // 深 teal 而不是亮青：白底对比度 ~4.7:1 过 AA；AntD 派生的 hover 色
    // 正好落在亮青 #0FB5BF 附近，色阶自然。
    colorPrimary: '#0a7e86',
    colorInfo: '#0a7e86',
    borderRadius: 8,
    borderRadiusLG: 10,
    borderRadiusSM: 4,
    fontFamily:
      "'IBM Plex Sans', -apple-system, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans SC', sans-serif",
    colorText: '#1c232b',
    colorTextSecondary: '#5a6572',
    colorBorder: '#d9dfe3',
    colorBorderSecondary: 'rgba(17,24,31,0.09)',
    colorBgLayout: '#f4f6f7',
    colorBgContainer: '#ffffff',
  },
  components: {
    Menu: { radiusItem: 8, itemMarginInline: 8 },
    Table: {
      tableHeaderBg: '#f4f6f7',
      tableHeaderTextColor: '#5a6572',
      tableBorderColor: 'rgba(17,24,31,0.09)',
      tableRowHoverBg: 'rgba(15,181,191,0.05)',
    },
    Tabs: { tabsHoverColor: '#0a7e86', tabsActiveColor: '#0a7e86' },
  },
}

// 子菜单没配图标时回退用父级的，标签页和左侧菜单的图标就保持一致了。
const LEAVES: MenuLeaf[] = MENU.flatMap((node) =>
  node.children?.length
    ? node.children.map((child) => ({
        path: child.path,
        title: child.title,
        icon: child.icon ?? node.icon,
      }))
    : [{ path: node.path, title: node.title, icon: node.icon }],
)
const DEFAULT_TAB_PATH = LEAVES[0].path

// 不在侧栏菜单里、但要在顶栏占标签的页面：工作流配置页从列表页点进来，
// 与「对话」同排一个标签。只进标签体系（TAB_LEAVES），不进 MENU，侧栏看不到。
const EXTRA_TABS: MenuLeaf[] = [
  { path: '/workflows/editor', title: '工作流配置', icon: PartitionOutlined },
]
const TAB_LEAVES: MenuLeaf[] = [...LEAVES, ...EXTRA_TABS]

// 运行现场是按运行 id 展开的动态标签：一个运行一枚标签，可同时开多枚互相切换。
// 识别规则本身抽到 appTabs.ts 走单测，这里只把静态菜单叶注进去。
const leafForPath = (path: string) => resolveTabLeaf(path, TAB_LEAVES)

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 登录页是独立渲染的，不带侧边栏和顶栏这些外壳。
const isBare = computed(() => Boolean(route.meta?.bare))

// 手机端（<768px，与登录页断点一致）：导航轨换成抽屉。
const isMobile = useIsMobile()
const drawerOpen = ref(false)
const sidebarNavRef = ref()
const drawerNavRef = ref()

// 导航轨收起状态：记住用户的选择，刷新后保持。
const SIDER_COLLAPSED_KEY = storageKeys.siderCollapsed
const collapsed = ref(false)
try {
  collapsed.value = localStorage.getItem(SIDER_COLLAPSED_KEY) === '1'
} catch {
  // 隐私模式等场景拿不到 localStorage，用默认值即可。
}
watch(collapsed, (value) => {
  try {
    localStorage.setItem(SIDER_COLLAPSED_KEY, value ? '1' : '0')
  } catch {
    // 同上。
  }
})

// 窄屏（< lg）强制收起，但不动用户的手动选择：窗口拉宽后恢复原状，
// 也不会把这次被动收起写进 localStorage。
const siderNarrow = ref(false)
const effectiveCollapsed = computed(() => collapsed.value || siderNarrow.value)

const onManualCollapse = (value: boolean) => {
  collapsed.value = value
}

const onSiderBreakpoint = (broken: boolean) => {
  siderNarrow.value = broken
}

// 标签页照搬参考控制台的做法：默认标签页固定常开，之后每访问一个页面就
// 往后追加一个。打开的标签页持久化到 localStorage，刷新后原样恢复；
// 注销时清掉（见 onUserMenuClick），避免下一个登录的人看到上一个人的
// 标签页——里面可能有他无权访问的管理页。
const OPEN_TABS_KEY = storageKeys.openTabs

function restoreOpenTabs(): string[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(OPEN_TABS_KEY) ?? '[]')
    if (Array.isArray(raw)) {
      // 只恢复还认识的路径（菜单调整后旧路径直接丢弃；运行现场按 id 现认），并去重。
      const paths = [
        ...new Set(raw.filter((p): p is string => typeof p === 'string' && Boolean(leafForPath(p)))),
      ]
      if (paths.length) {
        return [DEFAULT_TAB_PATH, ...paths.filter((p) => p !== DEFAULT_TAB_PATH)]
      }
    }
  } catch {
    // 存储不可用或内容损坏时退回默认。
  }
  return [DEFAULT_TAB_PATH]
}

const openTabPaths = ref<string[]>(restoreOpenTabs())

watch(openTabPaths, (paths) => {
  try {
    localStorage.setItem(OPEN_TABS_KEY, JSON.stringify(paths))
  } catch {
    // 隐私模式等场景拿不到 localStorage，这次不记住而已。
  }
})
const openTabs = computed(() =>
  openTabPaths.value.map(leafForPath).filter((leaf): leaf is MenuLeaf => Boolean(leaf)),
)
const activeTabKey = computed(() =>
  leafForPath(route.path) ? route.path : DEFAULT_TAB_PATH,
)

watch(
  () => route.path,
  (path) => {
    if (leafForPath(path) && !openTabPaths.value.includes(path)) {
      openTabPaths.value = [...openTabPaths.value, path]
    }
  },
  { immediate: true },
)

onMounted(() => {
  if (auth.loggedIn && !auth.userInfo) {
    auth.fetchCurrentUser()
  }
})

const onMenuSelect = (path: string) => {
  // 手机端点完菜单顺手收抽屉；桌面端 drawerOpen 本来就是 false，无害。
  drawerOpen.value = false
  if (path !== route.path) {
    router.push(path)
  }
}

const onTabChange = (key: string | number) => {
  if (key !== route.path) {
    router.push(String(key))
  }
}

// 关闭一批标签：默认标签不可关；全关完兜底留默认。当前页被关掉时要导航到一个
// 「活下来」的标签——只能在更新数组前从旧顺序里挑，否则选中一个已被关的路径，
// 路由监听会把它重新加回去（关闭所有反而又开出一个标签）。
// 优先落在右键的目标上（浏览器「关闭其他」的语义），否则向左找最近的存活标签，
// 与点 × 回退到左邻的行为一致。
const closeTabs = (paths: string[], preferred?: string) => {
  const closable = paths.filter((p) => p !== DEFAULT_TAB_PATH)
  if (!closable.some((p) => openTabPaths.value.includes(p))) return
  const old = openTabPaths.value
  const kept = old.filter((p) => !closable.includes(p))
  const targets = kept.length ? kept : [DEFAULT_TAB_PATH]
  if (closable.includes(route.path)) {
    let fallback = preferred && targets.includes(preferred) ? preferred : undefined
    for (let i = old.indexOf(route.path) - 1; i >= 0 && !fallback; i--) {
      if (targets.includes(old[i])) fallback = old[i]
    }
    fallback ??= targets.find((p) => p !== DEFAULT_TAB_PATH) ?? DEFAULT_TAB_PATH
    if (fallback !== route.path) router.push(fallback)
  }
  openTabPaths.value = targets
}

const onTabEdit = (targetKey: string | number | MouseEvent | KeyboardEvent, action: string) => {
  if (action !== 'remove' || typeof targetKey === 'object') {
    return
  }
  closeTabs([String(targetKey)])
}

// 标签右键菜单：复用运维表格那套 GridContextMenu（teleport 到 body，遮罩收点击）。
const tabMenu = ref<{ open: boolean; x: number; y: number; path: string }>({
  open: false,
  x: 0,
  y: 0,
  path: DEFAULT_TAB_PATH,
})

// 右键整枚标签都要出菜单——包括 pill 边缘那圈 padding 和关闭钮，监听只能挂在
// tabs 根元素上委托：导航条里的标签顺序与 openTabPaths 一一对应。
const onTabBarContextMenu = (e: MouseEvent) => {
  const tab = e.target instanceof Element ? e.target.closest('.ant-tabs-tab') : null
  const list = tab?.parentElement
  if (!tab || !list) {
    return
  }
  const index = [...list.children]
    .filter((el) => el.classList.contains('ant-tabs-tab'))
    .indexOf(tab)
  const path = openTabPaths.value[index]
  if (!path) {
    return
  }
  e.preventDefault()
  tabMenu.value = { open: true, x: e.clientX, y: e.clientY, path }
}

const tabMenuEntries = computed<GridMenuEntry[]>(() => {
  const { path } = tabMenu.value
  const paths = openTabPaths.value
  const closableOthers = paths.filter((p) => p !== path && p !== DEFAULT_TAB_PATH)
  const closableAll = paths.filter((p) => p !== DEFAULT_TAB_PATH)
  return [
    {
      key: 'close',
      label: '关闭当前',
      disabled: path === DEFAULT_TAB_PATH,
    },
    {
      key: 'close-others',
      label: '关闭其他',
      disabled: closableOthers.length === 0,
    },
    {
      key: 'close-all',
      label: '关闭所有',
      danger: closableAll.length > 0,
      disabled: closableAll.length === 0,
    },
  ]
})

const onTabMenuPick = (key: string) => {
  const { path } = tabMenu.value
  if (key === 'close') {
    closeTabs([path])
    return
  }
  if (key === 'close-others') {
    closeTabs(openTabPaths.value.filter((p) => p !== path), path)
    return
  }
  if (key === 'close-all') {
    closeTabs(openTabPaths.value)
  }
}

// ---- 全局快捷键 --------------------------------------------------------------
// Ctrl+W / Ctrl+Tab / Ctrl+PageUp 都被浏览器保留（拦不住，硬拦只会表现不一致），
// 标签页操作因此用 Alt+W 关闭、Ctrl+Shift+[/] 前后切换——和常见 IDE 的键位一致。
const isEditableTarget = (el: EventTarget | null): boolean =>
  el instanceof HTMLElement &&
  (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)

const onGlobalKeydown = (e: KeyboardEvent) => {
  // Ctrl/Cmd+K：聚焦菜单搜索（收起态先展开，框才存在）。输入框里也可用。
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    // 手机端搜索框在抽屉里：先拉开抽屉再聚焦。
    if (isMobile.value) {
      drawerOpen.value = true
      void nextTick(() => drawerNavRef.value?.focusSearch())
      return
    }
    if (effectiveCollapsed.value) collapsed.value = false
    void nextTick(() => sidebarNavRef.value?.focusSearch())
    return
  }
  // 在输入框里打字时不响应标签页操作键。
  if (isEditableTarget(e.target)) return

  if (e.altKey && !e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'w') {
    e.preventDefault()
    // 关闭当前标签；默认标签不可关的逻辑在 onTabEdit 里。
    onTabEdit(route.path, 'remove')
    return
  }

  if (e.ctrlKey && e.shiftKey && !e.altKey && (e.key === '[' || e.key === ']')) {
    e.preventDefault()
    const tabs = openTabPaths.value
    if (tabs.length < 2) return
    const index = tabs.indexOf(route.path)
    const delta = e.key === ']' ? 1 : -1
    const next = tabs[(index + delta + tabs.length) % tabs.length]
    if (next && next !== route.path) router.push(next)
  }
}

onMounted(() => window.addEventListener('keydown', onGlobalKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKeydown))

// 跨手机断点变化时收抽屉（桌面没有抽屉，手机没有导航轨）。
watch(isMobile, () => {
  drawerOpen.value = false
})

const onUserMenuClick = async ({ key }: { key: string | number }) => {
  if (key === 'logout') {
    try {
      localStorage.removeItem(OPEN_TABS_KEY)
    } catch {
      // 清不掉也无所谓：恢复时只认菜单里还存在的路径。
    }
    await auth.logout()
    router.replace('/login')
  }
}
</script>

<style>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.18s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ---- 手机端抽屉导航 -----------------------------------------------------
   抽屉 teleport 到 body，这里的全局样式才够得着。内容复用 style.css 里
   的 sidebar-* 全局类，只补抽屉壳本身的差异：ink 底、去掉 body 内边距、
   关闭钮用浅色。 */
.nav-drawer-trigger {
  flex-shrink: 0;
}

.mobile-nav-drawer .ant-drawer-content {
  background: var(--ink);
}

.mobile-nav-drawer .ant-drawer-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 0;
  overflow: hidden;
}

.mobile-nav-close {
  position: absolute;
  top: 16px;
  right: 12px;
  z-index: var(--z-raised);
  color: rgba(255, 255, 255, 0.68);
}

.mobile-nav-close:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

/* 窄屏顶栏：用户名让位给标签页，只留头像（退出登录还在下拉里）。 */
@media (max-width: 480px) {
  .user-name {
    display: none;
  }
}
</style>
