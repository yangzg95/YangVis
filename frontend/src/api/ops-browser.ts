/**
 * Navicat 式浏览器（库表结构、设计表、数据网格、ERD、Redis 键）的数据契约。
 * 只有类型，没有请求方法——方法都在 ops.ts 的 opsApi 里。
 */

// ---- 数据库浏览（Navicat 式界面的数据源） --------------------------------------

export interface DbSchemaItem {
  name: string
  kind: 'schema' | 'redisdb'
  object_count?: number | null
}

export interface DbTableItem {
  name: string
  table_type: string
  engine?: string | null
  rows_estimate?: number | null
  comment?: string | null
}

export interface DbColumnItem {
  name: string
  column_type: string
  nullable: boolean
  column_key: string
  default?: string | null
  extra: string
  comment: string
  /** 建表顺序（ORDINAL_POSITION），设计表页签算列位置用；结构页签忽略。 */
  position?: number
}

export interface DbIndexItem {
  name: string
  primary: boolean
  unique: boolean
  /** BTREE | FULLTEXT | HASH | SPATIAL。 */
  index_type: string
  /** 联合索引的列顺序即其可用前缀顺序，不能重排。 */
  columns: string[]
  cardinality?: number | null
  comment: string
}

export interface DbForeignKeyItem {
  name: string
  columns: string[]
  ref_schema: string
  ref_table: string
  /** 与 columns 等长且按位置一一对应（联合外键）。 */
  ref_columns: string[]
  on_update: string
  on_delete: string
}

export interface DbTableDdl {
  schema: string
  table: string
  /** SHOW CREATE TABLE 原文，服务端拼好的建表语句，前端不做二次拼装。 */
  ddl: string
}

/** 设计表里的一列期望定义。Extra 只有两种合法取值，所以收成两个布尔位。 */
export interface DbColumnDef {
  name: string
  /** 改名前的列名；留空表示没改名。缺了它改名就只能是「删一列 + 加一列」。 */
  origin_name?: string | null
  column_type: string
  nullable: boolean
  auto_increment: boolean
  on_update_current_timestamp: boolean
  /** 「没写 DEFAULT」与「DEFAULT NULL」在 MySQL 里不是一回事，必须分开表达。 */
  has_default: boolean
  default?: string | null
  comment: string
}

export interface DbIndexDef {
  name: string
  unique: boolean
  columns: string[]
}

/** 一张表的当前定义（设计表页签的初值）。外键只读，不在这里编辑。 */
export interface DbTableDef {
  schema: string
  table: string
  engine?: string | null
  collation?: string | null
  comment: string
  columns: DbColumnItem[]
  primary_key: string[]
  /** 不含主键本身，与 primary_key 分工一致。 */
  indexes: DbIndexItem[]
}

/** 整张表的期望定义：顺序即列顺序，服务端按它算 FIRST/AFTER。 */
export interface DbTableDefUpdate {
  columns: DbColumnDef[]
  primary_key: string[]
  indexes: DbIndexDef[]
  comment: string
}

export type DbAlterKind =
  | 'column-add'
  | 'column-drop'
  | 'column-modify'
  | 'column-rename'
  | 'primary'
  | 'index-add'
  | 'index-drop'
  | 'comment'

/** 一条将要执行的 ALTER 动作。 */
export interface DbAlterAction {
  kind: DbAlterKind
  sql: string
  note: string
  /** 会丢数据的动作（删列），界面标红并单独确认。 */
  destructive: boolean
}

export interface DbAlterPreview {
  actions: DbAlterAction[]
  warnings: string[]
}

export interface DbErdColumn {
  name: string
  column_type: string
  key: string
}

export interface DbErdTable {
  name: string
  table_type: string
  columns: DbErdColumn[]
}

export interface DbErdRelation {
  name: string
  from_table: string
  from_columns: string[]
  to_table: string
  to_columns: string[]
}

/** 一个库的实体关系数据。表数超上限时 truncated=true，前端如实提示。 */
export interface DbErd {
  schema: string
  tables: DbErdTable[]
  relations: DbErdRelation[]
  truncated: boolean
}

export interface DbCompletionColumn {
  name: string
  column_type: string
}

/** 查询编辑器补全用的「表 → 列」映射，一个库一次拉齐。 */
export interface DbCompletionTable {
  name: string
  columns: DbCompletionColumn[]
}

export interface DbRowsResult {
  columns: string[]
  rows: unknown[][]
  total: number
  page: number
  page_size: number
  elapsed_ms: number
  /** 展示用：产生这一页的 SELECT 文本（参数已代回字面量），给底部 SQL 栏用。 */
  sql: string
}

/** 行级写的行定位：整行原值（列名 → 原值，null 表示 NULL），服务端自取主键子集。 */
export type DbRowKey = Record<string, unknown>

export interface DbRowWriteResult {
  affected: number
  elapsed_ms: number
}

export type DbRowFilterOp =
  | 'eq'
  | 'ne'
  | 'like'
  | 'not_like'
  | 'lt'
  | 'lte'
  | 'gt'
  | 'gte'
  | 'is_null'
  | 'is_not_null'

export interface DbRowFilter {
  column: string
  op: DbRowFilterOp
  value?: string | null
}

export interface DbRowSort {
  column: string
  direction: 'asc' | 'desc'
}

/** 全量导出的格式，取值与后端 ``/rows/export`` 的 format 参数一致。 */
export type DbExportFormat = 'csv' | 'json' | 'markdown' | 'insert'

export interface RedisKeyItem {
  key: string
  key_type: string
}

export interface RedisScanResult {
  /** "0" 表示扫完了，否则把它带回下一页。 */
  cursor: string
  keys: RedisKeyItem[]
}

export interface RedisKeyDetail {
  key: string
  key_type: string
  /** -1 永不过期，-2 不存在。 */
  ttl: number
  /** string→字符串；list/set→string[]；hash/zset/stream→两列数组。 */
  value: unknown
  truncated: boolean
}

export type RedisKeyType = 'string' | 'list' | 'set' | 'zset' | 'hash' | 'stream'

/** 集合类的新建载荷里能出现的类型（stream 的 id 由服务端生成，不手工建）。 */
export type RedisWritableKeyType = Exclude<RedisKeyType, 'stream'>

/**
 * 一次结构化写的回执。``command`` 是服务端拼出来的命令文本（与审计同源），
 * 提示语直接引用它，用户事后对得上自己到底改了什么。
 */
export interface RedisWriteResult {
  command: string
  elapsed_ms: number
  /** 受影响的个数：删除类是删掉的条数，其余为 0/1。 */
  deleted: number
}

export interface RedisKeyCreatePayload {
  db: number
  key: string
  key_type: Exclude<RedisKeyType, 'stream'>
  /** string→文本；list/set→string[]；hash/zset→[[值, 值], …]。 */
  value: unknown
  ttl?: number | null
}

export interface RedisElementAddPayload {
  db: number
  key: string
  key_type: 'list' | 'set' | 'zset' | 'hash'
  /** 列表/集合/有序集合的元素值；hash 是 value。 */
  value?: string | null
  field?: string | null
  score?: number | null
  position?: 'head' | 'tail'
}

export interface RedisElementDeletePayload {
  db: number
  key: string
  key_type: RedisKeyType
  /** hash=field，set/zset=member，stream=条目 id，list=元素下标。 */
  target: string
}

export interface RedisTtlPayload {
  db: number
  key: string
  action: 'expire' | 'persist'
  seconds?: number | null
}

export interface OpsAuditItem {
  id: number
  target_type: string
  target_id: number
  target_name?: string | null
  actor: string
  command: string
  verdict: string
  success: boolean
  error?: string | null
  created_at: string
}

/** 远程文件浏览器里的一行。 */
export interface SftpEntry {
  name: string
  is_dir: boolean
  size: number
  mtime: number
  mode: number
}

export interface SftpListResult {
  path: string
  items: SftpEntry[]
  truncated: boolean
  /** 后端允许的上传上限（字节），0 表示没告知。 */
  max_upload_bytes?: number
}

/** 服务端粘贴的结果：落地路径与是否被自动改名（同名时后端给 ``xxx_副本.ext``）。 */
export interface SftpPasteResult {
  path: string
  renamed: boolean
}
