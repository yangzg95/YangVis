<template>
  <a-tree
    v-model:expanded-keys="expandedKeys"
    v-model:selected-keys="selectedKeys"
    class="conn-tree"
    :tree-data="treeData"
    :load-data="onLoadData"
    block-node
    @select="onSelect"
    @right-click="onRightClick"
  >
    <template #title="{ dataRef }">
      <div class="node">
        <!-- 连接节点：状态收成左侧色点，文案交给 tooltip，与旧列表的色条一致。 -->
        <template v-if="dataRef.kind === 'conn'">
          <a-tooltip :title="STATUS_TEXT[statusOf(dataRef.conn!)]">
            <span :class="['dot', statusOf(dataRef.conn!)]" />
          </a-tooltip>
          <a-tooltip :title="isOpen(dataRef.key) ? '连接已打开，双击关闭' : '连接已关闭，双击打开'">
            <DatabaseOutlined
              v-if="dataRef.conn!.db_type === 'mysql'"
              :class="['icon', 'mysql', { off: !isOpen(dataRef.key) }]"
            />
            <DatabaseOutlined v-else :class="['icon', 'redis', { off: !isOpen(dataRef.key) }]" />
          </a-tooltip>
          <span class="label">{{ dataRef.title }}</span>
        </template>

        <template v-else-if="dataRef.kind === 'schema' || dataRef.kind === 'redisdb'">
          <FolderOutlined class="icon schema" />
          <span class="label">{{ dataRef.title }}</span>
          <span v-if="dataRef.objectCount != null" class="count">{{ dataRef.objectCount }}</span>
        </template>

        <template v-else-if="dataRef.kind === 'table'">
          <EyeOutlined v-if="dataRef.tableType === 'VIEW'" class="icon view" />
          <TableOutlined v-else class="icon table" />
          <a-tooltip :title="dataRef.title" :mouse-enter-delay="0.6">
            <span class="label">{{ dataRef.title }}</span>
          </a-tooltip>
        </template>

        <template v-else-if="dataRef.kind === 'rkey'">
          <KeyOutlined class="icon rkey" />
          <a-tooltip :title="dataRef.title" :mouse-enter-delay="0.6">
            <span class="label">{{ dataRef.title }}</span>
          </a-tooltip>
          <a-tag class="key-tag" :color="KEY_TYPE_COLORS[dataRef.keyType] || 'default'">
            {{ dataRef.keyType }}
          </a-tag>
        </template>

        <template v-else>
          <DownOutlined class="icon more" />
          <span class="label more-label">{{ dataRef.title }}</span>
        </template>
      </div>
    </template>
  </a-tree>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  DatabaseOutlined,
  DownOutlined,
  EyeOutlined,
  FolderOutlined,
  KeyOutlined,
  TableOutlined,
} from '@ant-design/icons-vue'
import { message as toast } from 'ant-design-vue'
import { opsApi, type OpsDatabase } from '@/api'
import type { TabRequest, TreeMenuEvent, TreeNode } from './tabs'

const props = defineProps<{
  connections: OpsDatabase[]
}>()

const emit = defineEmits<{
  (e: 'select', conn: OpsDatabase): void
  (e: 'open', request: TabRequest): void
  (e: 'menu', event: TreeMenuEvent): void
  /** 请求关闭连接：父页面要二次确认，并连带关掉它打开的标签页。 */
  (e: 'close-conn', node: TreeNode): void
  /** 请求关闭一个库（schema / redis db）：同上，只是维度是库。 */
  (e: 'close-db', node: TreeNode): void
}>()

const STATUS_TEXT: Record<string, string> = {
  ok: '上次测试正常',
  error: '上次测试失败',
  unknown: '尚未测试过连接',
}

const KEY_TYPE_COLORS: Record<string, string> = {
  string: 'blue',
  list: 'green',
  set: 'orange',
  zset: 'purple',
  hash: 'cyan',
  stream: 'magenta',
}

const treeData = ref<TreeNode[]>([])
const expandedKeys = ref<string[]>([])
const selectedKeys = ref<string[]>([])

/** key → 节点，给「加载更多」找父节点用。 */
const nodeIndex = new Map<string, TreeNode>()

function statusOf(conn: OpsDatabase): 'ok' | 'error' | 'unknown' {
  if (conn.last_check_ok) return 'ok'
  return conn.last_check_error ? 'error' : 'unknown'
}

/**
 * 连接的展示色 → 行内样式。treeData 上的 style 会落到 .ant-tree-treenode
 * 整行（含缩进和三角），连接和它的整棵子树都带上，与 Navicat 的染色一致。
 * 选中行 antd 自己盖一层不透明选中底色，悬停是半透明叠加，都不冲突。
 */
function tintOf(conn: OpsDatabase): Record<string, string> | undefined {
  return conn.color ? { background: conn.color } : undefined
}

function indexChildren(nodes: TreeNode[]) {
  for (const node of nodes) {
    nodeIndex.set(node.key, node)
    if (node.children) indexChildren(node.children)
  }
}

watch(
  () => props.connections,
  (connections) => {
    nodeIndex.clear()
    treeData.value = connections.map((conn) => ({
      key: `conn:${conn.id}`,
      title: conn.name,
      kind: 'conn',
      conn,
      isLeaf: false,
      style: tintOf(conn),
    }))
    indexChildren(treeData.value)
  },
  { immediate: true },
)

function schemaNodes(conn: OpsDatabase, items: { name: string; kind: string; object_count?: number | null }[]): TreeNode[] {
  return items.map((item) => ({
    key: `schema:${conn.id}:${item.name}`,
    title: item.name,
    kind: item.kind === 'redisdb' ? 'redisdb' : 'schema',
    conn,
    schema: item.name,
    db: item.kind === 'redisdb' ? Number(item.name.slice(2)) || 0 : undefined,
    objectCount: item.object_count,
    isLeaf: false,
    style: tintOf(conn),
  }))
}

function tableNodes(conn: OpsDatabase, schema: string, items: { name: string; table_type: string }[]): TreeNode[] {
  return items.map((item) => ({
    key: `table:${conn.id}:${schema}:${item.name}`,
    title: item.name,
    kind: 'table',
    conn,
    schema,
    table: item.name,
    tableType: item.table_type,
    isLeaf: true,
    style: tintOf(conn),
  }))
}

function keyNodes(conn: OpsDatabase, db: number, keys: { key: string; key_type: string }[]): TreeNode[] {
  return keys.map((item) => ({
    key: `rkey:${conn.id}:${db}:${item.key}`,
    title: item.key,
    kind: 'rkey',
    conn,
    db,
    keyType: item.key_type,
    isLeaf: true,
    style: tintOf(conn),
  }))
}

function moreNode(conn: OpsDatabase, db: number, cursor: string, parentKey: string): TreeNode {
  return {
    key: `more:${conn.id}:${db}:${cursor}`,
    title: '加载更多…',
    kind: 'more',
    conn,
    db,
    cursor,
    parentKey,
    isLeaf: true,
    style: tintOf(conn),
  }
}

/** 一级懒加载：连接 → schema / redis db → 表 / key 首页。 */
async function loadChildren(node: TreeNode): Promise<TreeNode[]> {
  const conn = node.conn!
  if (node.kind === 'conn') {
    const res = await opsApi.dbSchemas(conn.id)
    return schemaNodes(conn, res.items)
  }
  if (node.kind === 'schema') {
    const res = await opsApi.dbTables(conn.id, node.schema!)
    return tableNodes(conn, node.schema!, res.items)
  }
  if (node.kind === 'redisdb') {
    const res = await opsApi.redisKeys(conn.id, node.db ?? 0)
    const children = keyNodes(conn, node.db ?? 0, res.keys)
    if (res.cursor !== '0') children.push(moreNode(conn, node.db ?? 0, res.cursor, node.key))
    return children
  }
  return []
}

async function onLoadData(node: unknown) {
  const dataRef = (node as { dataRef: TreeNode }).dataRef
  try {
    dataRef.children = await loadChildren(dataRef)
    indexChildren(dataRef.children)
    // 能展开就说明连接是通的，把「未测试」的灰点直接翻绿（后端也已落库）。
    if (dataRef.conn) dataRef.conn.last_check_ok = true
  } catch (err: any) {
    // 加载失败给个空数组，让节点可以再次展开重试之外，原因也要讲清楚。
    dataRef.children = []
    toast.error(err?.message || '加载失败')
  }
}

/** 右键菜单里的「刷新」：清掉已加载的子节点重拉一遍。 */
async function refresh(node: TreeNode) {
  if (node.kind === 'table' || node.kind === 'rkey' || node.kind === 'more') return
  try {
    node.children = await loadChildren(node)
    nodeIndex.clear()
    indexChildren(treeData.value)
  } catch (err: any) {
    toast.error(err?.message || '刷新失败')
  }
}

// ---- 连接的打开 / 关闭 -----------------------------------------------------------
//
// 通道本身是无状态的 HTTP，这里的「打开/关闭」是树的状态：打开 = 展开并加载子
// 节点；关闭 = 只收起，已加载的子节点留在内存里（与库节点收起的行为一致）。
// 不能图省事在关闭时把 children 置回 undefined「释放内容」：a-tree 内部记着
// 这个节点已加载过（loadedKeys），children 没了它也不会再触发 loadData，
// 内部缓存和展开态对不上，表现就是双击关不掉、点三角没反应。
// 想重新拉数据用右键菜单的「刷新」。

const isOpen = (key: string) => expandedKeys.value.includes(key)

async function openConnection(node: TreeNode) {
  if (node.children === undefined) {
    try {
      node.children = await loadChildren(node)
      indexChildren(node.children)
      // 能打开就说明连接是通的，灰点直接翻绿（后端也已落库）。
      if (node.conn) node.conn.last_check_ok = true
    } catch (err: any) {
      node.children = []
      toast.error(err?.message || '打开连接失败')
      return
    }
  }
  if (!isOpen(node.key)) expandedKeys.value = [...expandedKeys.value, node.key]
}

function closeConnection(node: TreeNode) {
  expandedKeys.value = expandedKeys.value.filter((key) => key !== node.key)
}

function toggleConnection(node: TreeNode) {
  // 关闭不在这里直接做：要弹二次确认、连带关标签页，都是父页面的事，
  // 树只负责发请求；父页面确认后调 closeConn 完成收起。
  if (isOpen(node.key)) emit('close-conn', node)
  else openConnection(node)
}

defineExpose({ refresh, toggleConn: toggleConnection, closeConn: closeConnection, collapse: closeConnection })

/** SCAN 翻页：把「加载更多」节点替换成下一页 key。 */
async function loadMore(node: TreeNode) {
  const parent = node.parentKey ? nodeIndex.get(node.parentKey) : undefined
  if (!parent || !node.conn) return
  try {
    const res = await opsApi.redisKeys(node.conn.id, node.db ?? 0, node.cursor ?? '0')
    const existing = new Set(
      (parent.children || []).filter((c) => c.kind === 'rkey').map((c) => c.title),
    )
    const fresh = keyNodes(node.conn, node.db ?? 0, res.keys).filter(
      // SCAN 不保证不重复，翻到重名的 key 直接跳过。
      (child) => !existing.has(child.title),
    )
    const rest = (parent.children || []).filter((c) => c.kind !== 'more')
    parent.children = [...rest, ...fresh]
    if (res.cursor !== '0') {
      parent.children.push(moreNode(node.conn, node.db ?? 0, res.cursor, parent.key))
    }
    indexChildren(parent.children)
  } catch (err: any) {
    toast.error(err?.message || '加载失败')
  }
}

// 双击打开。a-tree 没有 dblclick 事件，用两次 click 的间隔自己判。
let lastKey = ''
let lastTime = 0

/**
 * 双击 schema / redis db：未展开时点三角一样展开（需要时先懒加载）；
 * 已展开时视为「关闭数据库」——要二次确认、连带关标签页，发事件给父页面，
 * 确认后父页面调 collapse 完成收起（三角单击仍是纯收起，不走确认）。
 */
async function toggleExpand(node: TreeNode) {
  if (isOpen(node.key)) {
    emit('close-db', node)
    return
  }
  if (node.children === undefined) {
    try {
      node.children = await loadChildren(node)
      indexChildren(node.children)
      if (node.conn) node.conn.last_check_ok = true
    } catch (err: any) {
      node.children = []
      toast.error(err?.message || '加载失败')
      return
    }
  }
  expandedKeys.value = [...expandedKeys.value, node.key]
}

function openNode(node: TreeNode) {
  if (node.kind === 'conn') {
    // 双击连接 = 打开/关闭，单击只选中不展开。
    toggleConnection(node)
    return
  }
  if (node.kind === 'schema' || node.kind === 'redisdb') {
    // 双击库节点 = 点三角，展开/收起。
    toggleExpand(node)
    return
  }
  if (node.kind === 'table' && node.conn) {
    emit('open', { kind: 'data', conn: node.conn, schema: node.schema!, table: node.table! })
  } else if (node.kind === 'rkey' && node.conn) {
    emit('open', { kind: 'redis-key', conn: node.conn, db: node.db ?? 0, rkey: node.title })
  }
}

function onSelect(_keys: unknown, info: { node: { dataRef: TreeNode } }) {
  const node = info.node.dataRef
  if (node.kind === 'more') {
    selectedKeys.value = []
    loadMore(node)
    return
  }
  if (node.kind === 'conn' && node.conn) emit('select', node.conn)

  const now = Date.now()
  if (node.key === lastKey && now - lastTime < 400) openNode(node)
  lastKey = node.key
  lastTime = now
}

function onRightClick(info: { event: MouseEvent; node: { dataRef: TreeNode } }) {
  const node = info.node.dataRef
  if (node.kind === 'conn' && node.conn) emit('select', node.conn)
  emit('menu', {
    x: info.event.clientX,
    y: info.event.clientY,
    node,
    open: node.kind === 'conn' ? isOpen(node.key) : undefined,
  })
}
</script>

<style>
/* a-tree 的根节点在 antdv 内部渲染，拿不到本组件的 scope id，scoped 样式里
   以 .conn-tree 开头的规则全部不会命中，这几条必须非 scoped。 */
.conn-tree {
  height: 100%;
  overflow: auto;
  padding: 4px;
}

.conn-tree .ant-tree-node-content-wrapper {
  display: flex;
  min-width: 0;
}
</style>

<style scoped>
.node {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
}

.label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.icon {
  flex-shrink: 0;
  font-size: 13px;
}

.icon.mysql {
  color: var(--signal-text);
}

.icon.redis {
  color: #ff4d4f;
}

/* 连接未打开时图标压灰，一眼区分打开状态。 */
.icon.off {
  color: #bfbfbf;
}

.icon.schema {
  color: #faad14;
}

.icon.table {
  color: #52c41a;
}

.icon.view {
  color: #722ed1;
}

.icon.rkey {
  color: var(--text-3);
}

.icon.more {
  color: var(--signal-text);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: #d9d9d9;
}

.dot.ok {
  background: #52c41a;
}

.dot.error {
  background: #ff4d4f;
}

.count {
  flex-shrink: 0;
  color: var(--text-3);
  font-size: 12px;
}

.key-tag {
  flex-shrink: 0;
  margin-inline-end: 0;
  font-size: 11px;
  line-height: 16px;
  padding: 0 4px;
}

.more-label {
  color: var(--signal-text);
}
</style>
