/**
 * 对话工具调用的展示文案。
 *
 * SSE step 事件与消息 timeline 里只带工具名和原始入参，界面要显示什么由这里
 * 统一决定，避免「等待中转圈文案」「历史时间线卡片」「运维面板步骤行」各写一份
 * if-else。
 *
 * 一次调用拆成两段：动作名（「检索知识库」）与参数摘要（「nginx 502」）。
 * 分开放是为了让轨迹行排成「图标 + 动作名 + 状态词 + 摘要」的单行形态，
 * 摘要超长时只截摘要，动作名始终完整可读。
 */

const MAX_PREVIEW_LENGTH = 80

/**
 * 本地工具的中文标题登记表：Chat 页挂载时把后端工具目录灌进来，
 * key 是工具名（如 generate_uuid）。MCP 工具名没有目录，走默认兜底。
 */
const localToolTitles = new Map<string, string>()

/** 用后端本地工具目录刷新标题表，重复注册直接覆盖。 */
export function registerToolLabels(tools: { key: string; title: string }[]): void {
  for (const tool of tools) localToolTitles.set(tool.key, tool.title)
}

export interface ToolAction {
  /** 动作名，如「检索知识库」「执行命令」，恒非空。 */
  label: string
  /** 参数摘要，如命令本体、检索词；无参数的工具为空串。 */
  preview: string
}

function stringArg(input: Record<string, unknown> | undefined, key: string): string {
  const value = input?.[key]
  return typeof value === 'string' ? value : ''
}

/** 命令/SQL 这类长参数：压掉换行后截到一行能放下的长度。 */
function shortArg(input: Record<string, unknown> | undefined, ...keys: string[]): string {
  for (const key of keys) {
    const value = stringArg(input, key).replace(/\s+/g, ' ').trim()
    if (!value) continue
    return value.length <= MAX_PREVIEW_LENGTH ? value : `${value.slice(0, MAX_PREVIEW_LENGTH)}…`
  }
  return ''
}

/** 没有专门登记时的参数挑选顺序：先命令类、再查询类、最后关键词/表名。 */
export function argPreview(input: Record<string, unknown> | undefined): string {
  return shortArg(input, 'command', 'statement', 'sql', 'query', 'table', 'keyword', 'text')
}

/**
 * 一次工具调用的动作名 + 参数摘要。
 *
 * 不带「正在……」时态：同一份文案既用于流式进行时（配转圈图标），也用于历史
 * 回放（配完成图标），时态由调用方按状态词补。
 */
export function toolAction(
  tool: string | undefined,
  input?: Record<string, unknown>,
): ToolAction {
  switch (tool) {
    case 'search_knowledge_base':
      return { label: '检索知识库', preview: shortArg(input, 'query') }
    case 'current_datetime':
      return { label: '查询当前时间', preview: '' }
    case 'list_servers':
      return { label: '查看服务器列表', preview: '' }
    case 'run_server_command':
    case 'run_readonly_command':
      return { label: '执行命令', preview: shortArg(input, 'command') }
    case 'propose_command':
      return { label: '提议写命令', preview: shortArg(input, 'command') }
    case 'list_databases':
      return { label: '查看数据库列表', preview: '' }
    case 'run_database_query':
    case 'run_query':
      return { label: '执行查询', preview: shortArg(input, 'statement', 'sql') }
    case 'list_tables':
      return { label: '列出数据表', preview: '' }
    case 'describe_table':
      return { label: '查看表结构', preview: shortArg(input, 'table') }
    case 'redis_command':
      return { label: '执行 Redis 命令', preview: shortArg(input, 'command') }
    default: {
      // 后端代码注册的本地工具按目录里的中文标题显示，摘要从入参里猜。
      const localTitle = tool ? localToolTitles.get(tool) : undefined
      if (localTitle) return { label: localTitle, preview: argPreview(input) }
      if (tool) {
        // MCP 工具名是「服务前缀_工具原名」（见后端 server_prefix），动作名统一
        // 成「调用 MCP 工具」，服务与原名落到摘要位，免得长前缀挤掉动作名。
        const splitAt = tool.indexOf('_')
        if (splitAt > 0) {
          const service = tool.slice(0, splitAt)
          const name = tool.slice(splitAt + 1)
          const args = argPreview(input)
          return {
            label: '调用 MCP 工具',
            preview: args ? `${service} / ${name}：${args}` : `${service} / ${name}`,
          }
        }
        return { label: '调用工具', preview: tool }
      }
      return { label: '调用工具', preview: '' }
    }
  }
}

/** 工具调用的状态词：跟在动作名后面，与图标一起表达进度。 */
export function toolStateText(status: 'running' | 'done' | 'error'): string {
  if (status === 'running') return '运行中'
  if (status === 'error') return '运行失败'
  return '已运行'
}
