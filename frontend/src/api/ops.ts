/**
 * 智能运维域：SSH 连接与数据库配置的维护、SQL 与 Redis 命令的执行入口、审计流水与 SQL 收藏，
 * 外加 opsApi 这一整个 48 成员的聚合对象，以及终端与文件下载要用的同源 URL 拼装。
 *
 * opsApi 不拆成几个文件再 spread 合并：它的成员按能力交错（ticket 打头，audit 排在 Redis 块
 * 之后，收藏在最后），拆了就得动对象字面量本身。库表浏览那批界面用的类型在 ops-browser.ts。
 */
import type { AxiosResponse } from 'axios'
import { request } from './http'
import type {
  DbAlterPreview,
  DbColumnItem,
  DbCompletionTable,
  DbErd,
  DbExportFormat,
  DbForeignKeyItem,
  DbIndexItem,
  DbRowFilter,
  DbRowKey,
  DbRowSort,
  DbRowWriteResult,
  DbRowsResult,
  DbSchemaItem,
  DbTableDdl,
  DbTableDef,
  DbTableDefUpdate,
  DbTableItem,
  OpsAuditItem,
  RedisElementAddPayload,
  RedisElementDeletePayload,
  RedisKeyCreatePayload,
  RedisKeyDetail,
  RedisScanResult,
  RedisTtlPayload,
  RedisWriteResult,
  SftpListResult,
  SftpPasteResult,
} from './ops-browser'
import type { TestResult } from './settings'

// ---- 智能运维 --------------------------------------------------------------

export const CODE_OPS_FORBIDDEN = 4101
export const CODE_OPS_CONNECT_FAILED = 4102
export const CODE_OPS_EXEC_FAILED = 4103

export type ServerAuthType = 'password' | 'key'
export type DatabaseType = 'mysql' | 'redis'

export interface OpsServer {
  id: number
  name: string
  host: string
  port: number
  username: string
  auth_type: ServerAuthType
  /** 掩码形式；原样提交回来不会改动已保存的凭据。 */
  credential: string
  credential_error?: string | null
  host_key_pinned: boolean
  remark?: string | null
  last_checked_at?: string | null
  last_check_ok: boolean
  last_check_error?: string | null
  created_at: string
  updated_at: string
}

export interface OpsServerPayload {
  name: string
  host: string
  port?: number
  username: string
  auth_type?: ServerAuthType
  password?: string
  private_key?: string
  passphrase?: string
  remark?: string | null
}

export interface OpsDatabase {
  id: number
  name: string
  db_type: DatabaseType
  host: string
  port: number
  username?: string | null
  password: string
  password_error?: string | null
  db_name?: string | null
  /** 控制台写开关：开了之后手敲通道可执行 DML/DDL（AI 通道始终只读）。 */
  writable: boolean
  /** 展示色（#rrggbb）：树节点整行与页签头的底色；null 不染。 */
  color?: string | null
  remark?: string | null
  last_checked_at?: string | null
  last_check_ok: boolean
  last_check_error?: string | null
  created_at: string
  updated_at: string
}

export interface OpsDatabasePayload {
  name: string
  db_type?: DatabaseType
  host: string
  port?: number
  username?: string | null
  password?: string
  db_name?: string | null
  writable?: boolean
  color?: string | null
  remark?: string | null
}

export interface DbExecuteResult {
  columns: string[]
  rows: unknown[][]
  /** columns 为空时读这里：不是结果集的回复（Redis 标量等）。 */
  text?: string | null
  row_count: number
  truncated: boolean
  elapsed_ms: number
}

/** 批量执行里单条语句的结果（Navicat 式逐句执行）。 */
export interface DbBatchStatement {
  /** 1-based，消息列表 / 结果页签 / 摘要按它对齐。 */
  index: number
  /** 单行预览（截断 200 字符），完整脚本在审计记录里。 */
  sql: string
  status: 'ok' | 'error'
  /** 成功："N 行" / "执行完成"；失败：错误原因。 */
  message: string
  elapsed_ms: number
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  text?: string | null
}

/** 一段脚本的批量执行结果。单句出错不中断，逐句成败都在这里。 */
export interface DbBatchResult {
  statements: DbBatchStatement[]
  total: number
  succeeded: number
  failed: number
  started_at: string
  finished_at: string
  elapsed_ms: number
}

export interface WsTicket {
  ticket: string
  expires_in: number
}

export interface SqlFavorite {
  id: number
  /** null 表示所有连接通用的收藏。 */
  database_id: number | null
  title: string
  content: string
  remark?: string | null
  created_at: string
  updated_at: string
}

export interface SqlFavoritePayload {
  title: string
  content: string
  database_id?: number | null
  remark?: string | null
}

/** SFTP 下载地址。流式落盘走 fetch（不经 axios），URL 在这里集中拼。 */
export function sftpDownloadUrl(id: number, path: string): string {
  return `/api/ops/servers/${id}/files/download?path=${encodeURIComponent(path)}`
}

export const opsApi = {
  ticket: () => request<WsTicket>({ url: '/ops/ws-ticket', method: 'POST' }),

  listServers: (keyword?: string) =>
    request<{ items: OpsServer[]; total: number }>({
      url: '/ops/servers',
      params: keyword ? { keyword } : undefined,
    }),
  createServer: (payload: OpsServerPayload) =>
    request<OpsServer>({ url: '/ops/servers', method: 'POST', data: payload }),
  updateServer: (id: number, payload: Partial<OpsServerPayload>) =>
    request<OpsServer>({ url: `/ops/servers/${id}`, method: 'PUT', data: payload }),
  removeServer: (id: number) => request<null>({ url: `/ops/servers/${id}`, method: 'DELETE' }),
  testServer: (id: number) =>
    request<TestResult>({
      url: `/ops/servers/${id}/test`,
      method: 'POST',
      // SSH 握手 + 一次 uname，慢起来比 30 秒的全局超时还长。
      timeout: 60000,
      skipErrorToast: true,
    }),

  // ---- 远程文件（SFTP 文件浏览器） ----------------------------------------------
  // 全部 skipErrorToast：错误由文件面板在自己的错误区里呈现，不再全局弹。

  sftpList: (id: number, path?: string) =>
    request<SftpListResult>({
      url: `/ops/servers/${id}/files/list`,
      params: path ? { path } : undefined,
      timeout: 60000,
      skipErrorToast: true,
    }),
  sftpDownload: (
    id: number,
    path: string,
    opts?: { onProgress?: (loaded: number, total?: number) => void; signal?: AbortSignal },
  ) =>
    // responseType:'blob' 时响应拦截器原样返回整个 AxiosResponse（含 headers），
    // 出错时后端给的是 JSON 信封，由 saveBlobResponse 识别。
    request<AxiosResponse<Blob>>({
      url: `/ops/servers/${id}/files/download`,
      params: { path },
      responseType: 'blob',
      // 大文件下载不该被全局 30 秒超时杀掉。
      timeout: 0,
      skipErrorToast: true,
      onDownloadProgress: (event) => opts?.onProgress?.(event.loaded, event.total),
      signal: opts?.signal,
    }),
  sftpUpload: (
    id: number,
    dir: string,
    file: File,
    opts?: { onProgress?: (loaded: number, total?: number) => void; signal?: AbortSignal },
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<null>({
      url: `/ops/servers/${id}/files/upload`,
      method: 'POST',
      params: { path: dir },
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      skipErrorToast: true,
      onUploadProgress: (event) => opts?.onProgress?.(event.loaded, event.total ?? file.size),
      signal: opts?.signal,
    })
  },
  sftpMkdir: (id: number, path: string) =>
    request<null>({
      url: `/ops/servers/${id}/files/mkdir`,
      method: 'POST',
      data: { path },
      timeout: 60000,
      skipErrorToast: true,
    }),
  // 服务端复制/剪切：字节不出服务器，但一棵大树可能传很久，和递归删除一样不设超时。
  sftpCopy: (id: number, src: string, dstDir: string) =>
    request<SftpPasteResult>({
      url: `/ops/servers/${id}/files/copy`,
      method: 'POST',
      data: { src, dst_dir: dstDir },
      timeout: 0,
      skipErrorToast: true,
    }),
  sftpMove: (id: number, src: string, dstDir: string) =>
    request<SftpPasteResult>({
      url: `/ops/servers/${id}/files/move`,
      method: 'POST',
      data: { src, dst_dir: dstDir },
      timeout: 0,
      skipErrorToast: true,
    }),
  sftpRemove: (id: number, path: string) =>
    request<null>({
      url: `/ops/servers/${id}/files`,
      method: 'DELETE',
      params: { path },
      // 递归删除一棵大树可能远超 30 秒。
      timeout: 0,
      skipErrorToast: true,
    }),

  listDatabases: (params?: { keyword?: string; db_type?: DatabaseType }) =>
    request<{ items: OpsDatabase[]; total: number }>({ url: '/ops/databases', params }),
  createDatabase: (payload: OpsDatabasePayload) =>
    request<OpsDatabase>({ url: '/ops/databases', method: 'POST', data: payload }),
  updateDatabase: (id: number, payload: Partial<OpsDatabasePayload>) =>
    request<OpsDatabase>({ url: `/ops/databases/${id}`, method: 'PUT', data: payload }),
  removeDatabase: (id: number) => request<null>({ url: `/ops/databases/${id}`, method: 'DELETE' }),
  testDatabase: (id: number) =>
    request<TestResult>({
      url: `/ops/databases/${id}/test`,
      method: 'POST',
      timeout: 60000,
      skipErrorToast: true,
    }),
  execute: (id: number, command: string, schema?: string, opts?: { signal?: AbortSignal }) =>
    request<DbExecuteResult>({
      url: `/ops/databases/${id}/execute`,
      method: 'POST',
      data: { command, schema: schema || undefined },
      timeout: 60000,
      // 被安全网关挡下来也是一种正常结果，控制台自己把它打印出来。
      skipErrorToast: true,
      signal: opts?.signal,
    }),
  executeBatch: (id: number, command: string, schema?: string, opts?: { signal?: AbortSignal }) =>
    request<DbBatchResult>({
      url: `/ops/databases/${id}/execute/batch`,
      method: 'POST',
      data: { command, schema: schema || undefined },
      // 批量是逐句串行，一批可能远超单句的 60 秒。
      timeout: 0,
      skipErrorToast: true,
      signal: opts?.signal,
    }),

  // ---- 浏览（Navicat 式界面） --------------------------------------------------

  dbSchemas: (id: number) =>
    request<{ items: DbSchemaItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbTables: (id: number, schema: string) =>
    request<{ items: DbTableItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbColumns: (id: number, schema: string, table: string) =>
    request<{ items: DbColumnItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/columns`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbCompletion: (id: number, schema: string) =>
    request<{ items: DbCompletionTable[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/completion`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbTableDdl: (id: number, schema: string, table: string) =>
    request<DbTableDdl>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/ddl`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbIndexes: (id: number, schema: string, table: string) =>
    request<{ items: DbIndexItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/indexes`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbForeignKeys: (id: number, schema: string, table: string) =>
    request<{ items: DbForeignKeyItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/foreign-keys`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbErd: (id: number, schema: string) =>
    request<DbErd>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/erd`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbTableDef: (id: number, schema: string, table: string) =>
    request<DbTableDef>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/table-def`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  /** 期望定义 → 将要执行的 ALTER 语句。它自己不写数据，但和设计表执行同一个闸门。 */
  dbTableDefPreview: (id: number, schema: string, table: string, payload: DbTableDefUpdate) =>
    request<DbAlterPreview>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/table-def/preview`,
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  /** 执行预览原样回传的语句；服务端逐句重过网关，一句失败不中断其余。 */
  dbTableDefApply: (id: number, schema: string, table: string, statements: string[]) =>
    request<DbBatchResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/table-def/apply`,
      method: 'POST',
      data: { statements },
      timeout: 600000,
      skipErrorToast: true,
    }),
  dbRows: (
    id: number,
    schema: string,
    table: string,
    page: number,
    pageSize: number,
    filters?: DbRowFilter[],
    sorts?: DbRowSort[],
    where?: string,
  ) =>
    request<DbRowsResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      params: {
        page,
        page_size: pageSize,
        ...(filters?.length ? { filters: JSON.stringify(filters) } : {}),
        ...(sorts?.length ? { order_by: JSON.stringify(sorts) } : {}),
        ...(where?.trim() ? { where: where.trim() } : {}),
      },
      timeout: 60000,
      skipErrorToast: true,
    }),
  /**
   * 「导出全部」：满足这套筛选/排序条件的每一行，服务端分批取、流式写。
   *
   * 条件参数与 dbRows 完全一致，导的就是屏幕上这套条件命中的全集。响应体是文件
   * 而不是 JSON 信封，所以按 blob 收（拦截器对 blob 原样放行），失败时后端给的
   * 是 200 + 信封，由 saveBlobResponse 识别成错误。
   */
  dbRowsExport: (
    id: number,
    schema: string,
    table: string,
    format: DbExportFormat,
    opts?: { filters?: DbRowFilter[]; sorts?: DbRowSort[]; where?: string },
  ) =>
    request<AxiosResponse<Blob>>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows/export`,
      params: {
        format,
        ...(opts?.filters?.length ? { filters: JSON.stringify(opts.filters) } : {}),
        ...(opts?.sorts?.length ? { order_by: JSON.stringify(opts.sorts) } : {}),
        ...(opts?.where?.trim() ? { where: opts.where.trim() } : {}),
      },
      responseType: 'blob',
      // 几十万行在服务端要跑几分钟，全局 30 秒超时会把它误杀。
      timeout: 0,
      skipErrorToast: true,
    }),
  // 行级写：改值 / 删除记录。SQL 由服务端按主键生成，前端只回传原值与新值。
  updateRow: (id: number, schema: string, table: string, key: DbRowKey, sets: DbRowKey) =>
    request<DbRowWriteResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      method: 'PUT',
      data: { key, sets },
      timeout: 60000,
      skipErrorToast: true,
    }),
  deleteRow: (id: number, schema: string, table: string, key: DbRowKey) =>
    request<DbRowWriteResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      method: 'DELETE',
      data: { key },
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisKeys: (id: number, db: number, cursor = '0', count = 100, pattern?: string) =>
    request<RedisScanResult>({
      url: `/ops/databases/${id}/keys`,
      params: { db, cursor, count, ...(pattern ? { pattern } : {}) },
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisKeyDetail: (id: number, db: number, key: string) =>
    request<RedisKeyDetail>({
      url: `/ops/databases/${id}/key`,
      params: { db, key },
      timeout: 60000,
      skipErrorToast: true,
    }),
  // 结构化写：前端只交坐标（key / field / member / 下标），命令文本由服务端生成，
  // 值不参与命令拼接。闸门与服务端一致：连接 writable × 用户 ops_write。
  redisKeyCreate: (id: number, payload: RedisKeyCreatePayload) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/keys`,
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisStringUpdate: (id: number, db: number, key: string, value: string) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/key`,
      method: 'PUT',
      data: { db, key, value },
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisElementAdd: (id: number, payload: RedisElementAddPayload) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/key/elements`,
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisElementDelete: (id: number, payload: RedisElementDeletePayload) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/key/elements`,
      method: 'DELETE',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisTtl: (id: number, payload: RedisTtlPayload) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/key/ttl`,
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  /** 批量删除：选中什么删什么，不按模式扫。返回真正删掉的个数。 */
  redisKeysDelete: (id: number, db: number, keys: string[]) =>
    request<RedisWriteResult>({
      url: `/ops/databases/${id}/redis/keys/delete`,
      method: 'POST',
      data: { db, keys },
      timeout: 60000,
      skipErrorToast: true,
    }),

  audit: (params?: {
    target_type?: 'server' | 'database'
    target_id?: number
    limit?: number
    offset?: number
  }) => request<{ items: OpsAuditItem[]; total: number }>({ url: '/ops/audit', params }),

  // ---- SQL 收藏 ------------------------------------------------------------

  listSqlFavorites: (databaseId?: number) =>
    request<{ items: SqlFavorite[]; total: number }>({
      url: '/ops/sql-favorites',
      params: databaseId ? { database_id: databaseId } : undefined,
    }),
  createSqlFavorite: (payload: SqlFavoritePayload) =>
    request<SqlFavorite>({ url: '/ops/sql-favorites', method: 'POST', data: payload }),
  updateSqlFavorite: (id: number, payload: Partial<SqlFavoritePayload>) =>
    request<SqlFavorite>({ url: `/ops/sql-favorites/${id}`, method: 'PUT', data: payload }),
  removeSqlFavorite: (id: number) =>
    request<null>({ url: `/ops/sql-favorites/${id}`, method: 'DELETE' }),
}

/** 拼一条同源的 ws:// 或 wss:// 地址，路径沿用 axios 的 `/api` 前缀。 */
export function opsWsUrl(path: string, params: Record<string, string | number>): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) query.set(key, String(value))
  return `${scheme}://${window.location.host}/api/ops/${path}?${query.toString()}`
}
