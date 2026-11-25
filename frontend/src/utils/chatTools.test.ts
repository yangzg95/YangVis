import { describe, expect, it } from 'vitest'
import { argPreview, registerToolLabels, toolAction, toolStateText } from './chatTools'

describe('toolAction', () => {
  it('已登记的本地工具拆成动作名 + 参数摘要', () => {
    expect(toolAction('search_knowledge_base', { query: '部署流程' })).toEqual({
      label: '检索知识库',
      preview: '部署流程',
    })
    // 命令里的换行/多空格压成一段，摘要才占一行。
    expect(toolAction('run_server_command', { command: 'df   -h\n' }).preview).toBe('df -h')
    expect(toolAction('redis_command', { command: 'INFO persistence' }).label).toBe(
      '执行 Redis 命令',
    )
    expect(toolAction('list_servers')).toEqual({ label: '查看服务器列表', preview: '' })
  })

  it('命令类之外的入参按 command/statement/sql/query/table 顺序挑', () => {
    expect(toolAction('run_database_query', { statement: 'SELECT 1' }).preview).toBe('SELECT 1')
    expect(argPreview({ sql: 'SELECT 2' })).toBe('SELECT 2')
    expect(argPreview({ keyword: '订单' })).toBe('订单')
    expect(argPreview({ other: 'x' })).toBe('')
    expect(argPreview(undefined)).toBe('')
  })

  it('MCP 工具名拆成服务/原名落到摘要位，动作名统一', () => {
    expect(toolAction('amap_maps_geo', { text: '海淀区' })).toEqual({
      label: '调用 MCP 工具',
      preview: 'amap / maps_geo：海淀区',
    })
    // 参数挑不到时只留「服务 / 原名」，摘要位不留空壳。
    expect(toolAction('amap_maps_geo').preview).toBe('amap / maps_geo')
  })

  it('后端工具目录的中文标题优先于 MCP 猜测', () => {
    registerToolLabels([{ key: 'generate_uuid', title: '生成随机 ID' }])
    expect(toolAction('generate_uuid', { count: 3 })).toEqual({
      label: '生成随机 ID',
      preview: '',
    })
  })

  it('无下划线且未登记的工具名原样出现在摘要位', () => {
    expect(toolAction('fetch')).toEqual({ label: '调用工具', preview: 'fetch' })
    expect(toolAction(undefined)).toEqual({ label: '调用工具', preview: '' })
  })

  it('长摘要单行截断，不把动作名挤出视野', () => {
    const long = `echo ${'x'.repeat(90)}`
    const action = toolAction('run_server_command', { command: long })
    expect(action.preview).toHaveLength(81)
    expect(action.preview.endsWith('…')).toBe(true)
  })

  it('状态词跟随时态走：运行中/运行失败/已运行', () => {
    expect(toolStateText('running')).toBe('运行中')
    expect(toolStateText('error')).toBe('运行失败')
    expect(toolStateText('done')).toBe('已运行')
  })
})
