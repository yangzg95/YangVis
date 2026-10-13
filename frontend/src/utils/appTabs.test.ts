import { describe, expect, it } from 'vitest'
import { leafForPath, RUN_TAB_PREFIX, type TabLeaf } from '@/utils/appTabs'

const staticLeaves: TabLeaf[] = [
  { path: '/workflows', title: '工作流', icon: 'PartitionOutlined' },
  { path: '/workflows/editor', title: '工作流配置', icon: 'PartitionOutlined' },
  { path: '/chat', title: '对话', icon: 'MessageOutlined' },
]

describe('leafForPath', () => {
  it('静态菜单叶原样返回，且优先于动态规则', () => {
    const leaf = leafForPath('/workflows', staticLeaves)
    expect(leaf).toBe(staticLeaves[0])
  })

  it('运行现场按 id 展开成动态标签', () => {
    const leaf = leafForPath('/workflows/runs/99', staticLeaves)
    expect(leaf).toEqual({
      path: '/workflows/runs/99',
      title: '运行 #99',
      icon: expect.anything(),
    })
  })

  it('不同运行 id 各自成标签', () => {
    expect(leafForPath('/workflows/runs/16', staticLeaves)?.title).toBe('运行 #16')
    expect(leafForPath('/workflows/runs/99', staticLeaves)?.title).toBe('运行 #99')
  })

  it('非数字 id 不认，交给路由的 \\d+ 约束兜底', () => {
    expect(leafForPath(`${RUN_TAB_PREFIX}abc`, staticLeaves)).toBeUndefined()
    expect(leafForPath(`${RUN_TAB_PREFIX}`, staticLeaves)).toBeUndefined()
    expect(leafForPath(`${RUN_TAB_PREFIX}12/edit`, staticLeaves)).toBeUndefined()
  })

  it('前缀不符的路径没有标签', () => {
    expect(leafForPath('/workflowsx/runs/1', staticLeaves)).toBeUndefined()
    expect(leafForPath('/ops/server/3', staticLeaves)).toBeUndefined()
  })

  it('流程的运行记录按流程 id 展开成动态标签', () => {
    const leaf = leafForPath('/workflows/12/runs', staticLeaves)
    expect(leaf).toEqual({
      path: '/workflows/12/runs',
      title: '运行记录 #12',
      icon: expect.anything(),
    })
    expect(leafForPath('/workflows/7/runs', staticLeaves)?.title).toBe('运行记录 #7')
  })

  it('运行记录形态只有整段 id 加 /runs 才认', () => {
    expect(leafForPath('/workflows/abc/runs', staticLeaves)).toBeUndefined()
    expect(leafForPath('/workflows/12/run', staticLeaves)).toBeUndefined()
    expect(leafForPath('/workflows/12/runs/', staticLeaves)).toBeUndefined()
    expect(leafForPath('/workflows/12/runs/edit', staticLeaves)).toBeUndefined()
  })

  it('空静态叶下仍能认运行标签', () => {
    expect(leafForPath('/workflows/runs/7', [])?.title).toBe('运行 #7')
    expect(leafForPath('/workflows/7/runs', [])?.title).toBe('运行记录 #7')
  })
})
