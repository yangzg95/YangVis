/**
 * 对话工具调用的展示文案。
 *
 * SSE step 事件与消息 timeline 里只带工具名和原始入参，界面要显示什么由这里
 * 统一决定，避免「等待中转圈文案」和「历史时间线卡片」各写一份 if-else。
 * 入参只挑能说明「在查什么」的短字段（命令、查询词、SQL 片段），超长截断。
 */

const MAX_ARG_LENGTH = 80

/**
 * 本地工具的中文标题登记表：Chat 页挂载时把后端工具目录灌进来，
 * key 是工具名（如 generate_uuid）。MCP 工具名没有目录，走默认兜底。
 */
const localToolTitles = new Map<string, string>()

/** 用后端本地工具目录刷新标题表，重复注册直接覆盖。 */
export function registerToolLabels(tools: { key: string; title: string }[]): void {
  for (const tool of tools) localToolTitles.set(tool.key, tool.title)
}

function stringArg(input: Record<string, unknown> | undefined, key: string): string {
  const value = input?.[key]
  return typeof value === 'string' ? value : ''
}

/** 命令/SQL 这类长参数：截断到一行能放下的长度。 */
function shortArg(input: Record<string, unknown> | undefined, key: string): string {
  const value = stringArg(input, key).replace(/\s+/g, ' ').trim()
  if (!value) return ''
  return value.length <= MAX_ARG_LENGTH ? value : `${value.slice(0, MAX_ARG_LENGTH)}…`
}

/**
 * 一次工具调用的名词性标题，如「检索知识库：xxx」「执行只读命令：df -h」。
 * 不带「正在……」时态：同一份文案既用于流式进行时（配转圈图标），也用于
 * 历史回放（配完成图标）。
 */
export function toolLabel(
  tool: string | undefined,
  input?: Record<string, unknown>,
): string {
  switch (tool) {
    case 'search_knowledge_base': {
      const query = shortArg(input, 'query')
      return query ? `检索知识库：${query}` : '检索知识库'
    }
    case 'current_datetime':
      return '查询当前时间'
    case 'list_servers':
      return '查看服务器列表'
    case 'run_server_command':
    case 'run_readonly_command': {
      const command = shortArg(input, 'command')
      return command ? `执行只读命令：${command}` : '执行只读命令'
    }
    case 'propose_command': {
      const command = shortArg(input, 'command')
      return command ? `提议写命令（待确认）：${command}` : '提议写命令（待确认）'
    }
    case 'list_databases':
      return '查看数据库列表'
    case 'run_database_query':
    case 'run_query': {
      const statement = shortArg(input, 'statement') || shortArg(input, 'sql')
      return statement ? `执行只读查询：${statement}` : '执行只读查询'
    }
    case 'list_tables':
      return '列出数据库表'
    case 'describe_table': {
      const table = shortArg(input, 'table')
      return table ? `查看表结构：${table}` : '查看表结构'
    }
    case 'redis_command': {
      const command = shortArg(input, 'command')
      return command ? `执行 Redis 只读命令：${command}` : '执行 Redis 只读命令'
    }
    default: {
      // 后端代码注册的本地工具按目录里的中文标题显示；MCP 与未知工具兜底。
      const localTitle = tool ? localToolTitles.get(tool) : undefined
      if (localTitle) return localTitle
      return tool ? `调用工具：${tool}` : '调用工具'
    }
  }
}
