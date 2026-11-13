import type { OpsDatabase } from '@/api'

/** 树向主区发出的「打开一个标签页」请求。壳组件负责去重与激活。 */
export type TabRequest =
  | { kind: 'data'; conn: OpsDatabase; schema: string; table: string }
  | { kind: 'structure'; conn: OpsDatabase; schema: string; table: string }
  | { kind: 'design'; conn: OpsDatabase; schema: string; table: string }
  | { kind: 'erd'; conn: OpsDatabase; schema: string }
  | { kind: 'query'; conn: OpsDatabase; schema?: string; sql?: string }
  | { kind: 'redis-keys'; conn: OpsDatabase; db: number }
  | { kind: 'redis-key'; conn: OpsDatabase; db: number; rkey: string }

/** 树节点。key 里编码了类型与坐标，右键菜单靠它还原上下文。 */
export interface TreeNode {
  key: string
  title: string
  /** filter / note 是 schema 下的辅助行：表名过滤框与「少列了 N 张」的说明。 */
  kind: 'conn' | 'schema' | 'redisdb' | 'table' | 'rkey' | 'more' | 'filter' | 'note'
  conn?: OpsDatabase
  schema?: string
  db?: number
  table?: string
  tableType?: string
  keyType?: string
  objectCount?: number | null
  /** more 节点专用：下一页游标与父节点 key。 */
  cursor?: string
  parentKey?: string
  /** filter 节点专用：已生效的表名关键字（服务端 LIKE），空串表示未过滤。 */
  keyword?: string
  /** filter 节点专用：正在按关键字重拉表列表。 */
  loading?: boolean
  isLeaf?: boolean
  children?: TreeNode[]
  /** 透传给 .ant-tree-treenode 的行内样式：连接的展示色靠它染整行。 */
  style?: Record<string, string>
}

/** 右键菜单事件载荷。``open`` 仅连接节点有值：当前是否处于打开状态。 */
export interface TreeMenuEvent {
  x: number
  y: number
  node: TreeNode
  open?: boolean
}
