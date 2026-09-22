/**
 * localStorage key 注册表。
 * 全站统一 `noetix-` 前缀；新增持久化 key 一律先登记在这里，
 * 避免再出现 yangvis-/ops-/noetix- 三种前缀混用、同名撞车的局面。
 */
export const storageKeys = {
  accessToken: 'noetix-access-token',
  siderCollapsed: 'noetix-sider-collapsed',
  openTabs: 'noetix-open-tabs',
  opsChatModel: 'noetix-ops-chat-model',
  opsDbListWidth: 'noetix-ops-db-list-width',
  opsDbChatWidth: 'noetix-ops-db-chat-width',
  /** 服务器终端页右侧 AI 面板宽度。 */
  opsTermAiWidth: 'noetix-ops-term-ai-width',
  /** 数据库问答页右侧 AI 面板宽度（与工作台内嵌面板分开记）。 */
  opsDbAiWidth: 'noetix-ops-db-ai-width',
  opsDbEditorHeight: 'noetix-ops-db-editor-height',
  /** 表格列宽覆盖值：{ 表格标识: { 列key: 宽度 } } 一个 JSON 对象全装下。 */
  opsDbColWidths: 'noetix-ops-db-colwidths',
} as const
