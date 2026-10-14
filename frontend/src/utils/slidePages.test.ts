import { describe, expect, it } from 'vitest'
import {
  MAX_ARCH_BLOCKS,
  MAX_ARCH_LAYERS,
  MAX_FLOW_NODES,
  MAX_STEPS,
  MAX_TABLE_COLUMNS,
  MAX_TABLE_ROWS,
  MIN_STRUCTURED,
  MIN_TABLE_COLUMNS,
  appendArchBlock,
  appendArchLayer,
  appendBullet,
  appendColumn,
  appendFlowNode,
  appendRow,
  appendStep,
  canRemoveStructured,
  createPage,
  isLastFlowNode,
  normalizePage,
  popArchBlock,
  removeArchLayer,
  removeColumn,
  removeFlowNode,
  topUpForLayout,
  type EditablePage,
  type RawPage,
} from './slidePages'

/**
 * 这些数字的另一半在 backend/app/services/slides/html.py：MAX_TABLE_COLUMNS /
 * MAX_TABLE_ROWS / MAX_STEPS / MAX_FLOW / MAX_ARCH_LAYERS / MAX_ARCH_BLOCKS，以及
 * 「不成图就退回要点页」的 FLOW_MIN_NODES / ARCH_MIN_LAYERS。前端比它松，用户写下的
 * 字会被服务端的 sanitize_pages 裁掉——「刚写的字没了」就是这么来的，所以这里钉死。
 */
const BACKEND_CAPS = {
  MAX_FLOW_NODES: 5,
  MAX_ARCH_LAYERS: 5,
  MAX_ARCH_BLOCKS: 4,
  MAX_STEPS: 5,
  MAX_TABLE_COLUMNS: 4,
  MAX_TABLE_ROWS: 6,
  MIN_STRUCTURED: 2,
}

function page(over: Partial<EditablePage> = {}): EditablePage {
  return { ...createPage('bullets'), ...over }
}

function flowPage(count: number): EditablePage {
  const target = page({ layout: 'flow' })
  target.flow = Array.from({ length: count }, (_, i) => ({
    title: `格${i + 1}`,
    desc: '',
    via: '',
  }))
  return target
}

function archPage(layerBlockCounts: number[]): EditablePage {
  const target = page({ layout: 'arch' })
  target.layers = layerBlockCounts.map((count, index) => ({
    name: `层${index + 1}`,
    blocks: Array.from({ length: count }, (_, i) => `块${i + 1}`),
  }))
  return target
}

function tablePage(columns: number, rows: number): EditablePage {
  const target = page({ layout: 'table' })
  const names = Array.from({ length: columns }, (_, i) => `列${i + 1}`)
  target.table = { columns: names, rows: Array.from({ length: rows }, () => [...names]) }
  return target
}

describe('slidePages 的上下限与渲染器同源', () => {
  it('每个上限都等于后端那份常数', () => {
    expect(MAX_FLOW_NODES).toBe(BACKEND_CAPS.MAX_FLOW_NODES)
    expect(MAX_ARCH_LAYERS).toBe(BACKEND_CAPS.MAX_ARCH_LAYERS)
    expect(MAX_ARCH_BLOCKS).toBe(BACKEND_CAPS.MAX_ARCH_BLOCKS)
    expect(MAX_STEPS).toBe(BACKEND_CAPS.MAX_STEPS)
    expect(MAX_TABLE_COLUMNS).toBe(BACKEND_CAPS.MAX_TABLE_COLUMNS)
    expect(MAX_TABLE_ROWS).toBe(BACKEND_CAPS.MAX_TABLE_ROWS)
    expect(MIN_STRUCTURED).toBe(BACKEND_CAPS.MIN_STRUCTURED)
  })

  it('表格最少两列是「对比」本身的下限，与幻灯片架构师提示词的 2-4 列同一口径', () => {
    expect(MIN_TABLE_COLUMNS).toBe(2)
  })
})

describe('normalizePage', () => {
  it('空对象也能补成一页表单填得动的形状', () => {
    const normalized = normalizePage({})
    expect(normalized).toEqual({
      layout: 'bullets',
      kicker: '',
      title: '',
      subtitle: '',
      bullets: [],
      table: { columns: [], rows: [] },
      steps: [],
      flow: [],
      layers: [],
      takeaway: '',
      asset_id: null,
      notes: '',
    })
  })

  it('手写 JSON 里的简写与脏值都被归一', () => {
    const raw = {
      layout: 'flow',
      bullets: ['一条', null, 3],
      steps: ['纯字符串的一步', { title: '有 desc', desc: '说明' }, null],
      flow: ['也只有名字', { title: '两', desc: '', via: null }],
      layers: ['接入层', { name: '服务层', blocks: ['网关', null] }],
      table: { columns: ['甲', '乙'], rows: [['一行'], '不是数组的行'] },
      asset_id: undefined,
    }
    const normalized = normalizePage(raw as unknown as RawPage)
    expect(normalized.layout).toBe('flow')
    expect(normalized.bullets).toEqual(['一条', '', '3'])
    expect(normalized.steps).toEqual([
      { title: '纯字符串的一步', desc: '' },
      { title: '有 desc', desc: '说明' },
      { title: '', desc: '' },
    ])
    expect(normalized.flow).toEqual([
      { title: '也只有名字', desc: '', via: '' },
      { title: '两', desc: '', via: '' },
    ])
    expect(normalized.layers).toEqual([
      { name: '接入层', blocks: [] },
      { name: '服务层', blocks: ['网关', ''] },
    ])
    expect(normalized.table.rows).toEqual([['一行'], []])
    expect(normalized.asset_id).toBeNull()
  })

  it('不是数组的集合字段一律当空，而不是留到模板里报错', () => {
    const raw = {
      bullets: '要点',
      flow: { title: '不是数组' },
      layers: null,
      table: null,
    }
    const normalized = normalizePage(raw as unknown as RawPage)
    expect(normalized.bullets).toEqual([])
    expect(normalized.flow).toEqual([])
    expect(normalized.layers).toEqual([])
    expect(normalized.table).toEqual({ columns: [], rows: [] })
  })
})

describe('切版式时补骨架', () => {
  it('链路补到两格、分层补到两层，每层先给一个方块位', () => {
    const flow = topUpForLayout(page({ layout: 'flow' }))
    expect(flow.flow).toHaveLength(MIN_STRUCTURED)
    expect(flow.flow[0]).toEqual({ title: '', desc: '', via: '' })

    const arch = topUpForLayout(page({ layout: 'arch' }))
    expect(arch.layers).toHaveLength(MIN_STRUCTURED)
    expect(arch.layers[0]).toEqual({ name: '', blocks: [''] })
  })

  it('已有内容一格都不动，不足两格才补差额', () => {
    const three = topUpForLayout(flowPage(3))
    expect(three.flow).toHaveLength(3)
    expect(three.flow[0].title).toBe('格1')

    const topped = topUpForLayout(archPage([2]))
    expect(topped.layers).toHaveLength(MIN_STRUCTURED)
    expect(topped.layers[0].blocks).toEqual(['块1', '块2'])
  })

  it('切到表格先有 2 列 1 行，否则「加一行」是灰的', () => {
    expect(topUpForLayout(page({ layout: 'table' })).table).toEqual({
      columns: ['', ''],
      rows: [['', '']],
    })
  })

  it('切到步骤先有 2 步', () => {
    expect(topUpForLayout(page({ layout: 'steps' })).steps).toEqual([
      { title: '', desc: '' },
      { title: '', desc: '' },
    ])
  })

  it('要点页什么都不补', () => {
    const bullets = topUpForLayout(page({ layout: 'bullets', bullets: ['a'] }))
    expect(bullets.bullets).toEqual(['a'])
    expect(bullets.flow).toEqual([])
  })

  it('新建的一页按自己的版式给骨架', () => {
    expect(createPage('arch').layers).toHaveLength(MIN_STRUCTURED)
    expect(createPage().layout).toBe('bullets')
  })
})

describe('链路图的增删都卡在两端', () => {
  it('加到 5 格就停', () => {
    const target = flowPage(MIN_STRUCTURED)
    for (let i = 0; i < MAX_FLOW_NODES - MIN_STRUCTURED; i += 1) {
      expect(appendFlowNode(target)).toBe(true)
    }
    expect(target.flow).toHaveLength(MAX_FLOW_NODES)
    expect(appendFlowNode(target)).toBe(false)
    expect(target.flow).toHaveLength(MAX_FLOW_NODES)
  })

  it('删到 2 格就停，删的是指定那一格', () => {
    const target = flowPage(MAX_FLOW_NODES)
    expect(removeFlowNode(target, 0)).toBe(true)
    expect(removeFlowNode(target, 0)).toBe(true)
    expect(removeFlowNode(target, 0)).toBe(true)
    expect(target.flow).toHaveLength(MIN_STRUCTURED)
    expect(removeFlowNode(target, 0)).toBe(false)
    expect(target.flow.map((node) => node.title)).toEqual(['格4', '格5'])
  })

  it('只有最后一格没有箭头，条件写在它前面那一格上', () => {
    const target = flowPage(3)
    expect(isLastFlowNode(target, 1)).toBe(false)
    expect(isLastFlowNode(target, 2)).toBe(true)
  })

  it('canRemoveStructured 读的是当前版式那一组格子', () => {
    expect(canRemoveStructured(flowPage(MIN_STRUCTURED))).toBe(false)
    expect(canRemoveStructured(flowPage(3))).toBe(true)
    expect(canRemoveStructured(archPage([1, 1]))).toBe(false)
    expect(canRemoveStructured(archPage([1, 1, 1]))).toBe(true)
  })
})

describe('分层图的增删都卡在两端', () => {
  it('加到 5 层就停，新层自带一个方块位', () => {
    const target = archPage([1, 1])
    for (let i = 0; i < MAX_ARCH_LAYERS - 2; i += 1) {
      expect(appendArchLayer(target)).toBe(true)
    }
    expect(target.layers).toHaveLength(MAX_ARCH_LAYERS)
    expect(appendArchLayer(target)).toBe(false)
    expect(target.layers[4].blocks).toHaveLength(1)
  })

  it('删到 2 层就停', () => {
    const target = archPage([1, 1, 1])
    expect(removeArchLayer(target, 2)).toBe(true)
    expect(removeArchLayer(target, 2)).toBe(false)
    expect(target.layers).toHaveLength(MIN_STRUCTURED)
  })

  it('一层加到 4 块就停、删到 1 块就停，层与层之间不串', () => {
    const target = archPage([1, 2])
    expect(appendArchBlock(target, 0)).toBe(true)
    expect(appendArchBlock(target, 0)).toBe(true)
    expect(appendArchBlock(target, 0)).toBe(true)
    expect(target.layers[0].blocks).toHaveLength(MAX_ARCH_BLOCKS)
    expect(appendArchBlock(target, 0)).toBe(false)
    expect(target.layers[1].blocks).toHaveLength(2)

    expect(popArchBlock(target, 0)).toBe(true)
    expect(popArchBlock(target, 0)).toBe(true)
    expect(popArchBlock(target, 0)).toBe(true)
    expect(target.layers[0].blocks).toHaveLength(1)
    expect(popArchBlock(target, 0)).toBe(false)
    expect(appendArchBlock(target, 9)).toBe(false)
    expect(popArchBlock(target, 9)).toBe(false)
  })
})

describe('对比表的形状始终对齐', () => {
  it('加列给每一行补一个空格，加行按当前列数给格子', () => {
    const target = tablePage(2, 1)
    expect(appendColumn(target)).toBe(true)
    expect(target.table.columns).toHaveLength(3)
    expect(target.table.rows).toEqual([['列1', '列2', '']])

    expect(appendRow(target)).toBe(true)
    expect(target.table.rows[1]).toEqual(['', '', ''])
  })

  it('列 2-4、行最多 6，越界一律不改形状', () => {
    const wide = tablePage(MAX_TABLE_COLUMNS, 1)
    expect(appendColumn(wide)).toBe(false)
    expect(wide.table.columns).toHaveLength(MAX_TABLE_COLUMNS)
    expect(wide.table.rows[0]).toHaveLength(MAX_TABLE_COLUMNS)

    const narrow = tablePage(MIN_TABLE_COLUMNS, 1)
    expect(removeColumn(narrow)).toBe(false)
    expect(narrow.table.columns).toHaveLength(MIN_TABLE_COLUMNS)

    const tall = tablePage(2, MAX_TABLE_ROWS)
    expect(appendRow(tall)).toBe(false)
    expect(tall.table.rows).toHaveLength(MAX_TABLE_ROWS)

    const empty = page({ layout: 'table' })
    expect(appendRow(empty)).toBe(false)
    expect(empty.table.rows).toEqual([])
  })

  it('删列把每一行的末格一起删掉', () => {
    const target = tablePage(3, 2)
    expect(removeColumn(target)).toBe(true)
    expect(target.table.columns).toHaveLength(2)
    expect(target.table.rows).toEqual([
      ['列1', '列2'],
      ['列1', '列2'],
    ])
  })
})

describe('步骤页与要点条', () => {
  it('步骤加到 5 就停', () => {
    const target = createPage('steps')
    expect(target.steps).toHaveLength(MIN_STRUCTURED)
    for (let i = 0; i < MAX_STEPS - MIN_STRUCTURED; i += 1) {
      expect(appendStep(target)).toBe(true)
    }
    expect(target.steps).toHaveLength(MAX_STEPS)
    expect(appendStep(target)).toBe(false)
  })

  it('要点条数前端不设限（服务端才裁剪），脏值补成数组', () => {
    const target = page({ layout: 'bullets' })
    target.bullets = undefined as unknown as string[]
    appendBullet(target)
    expect(target.bullets).toEqual([''])
  })
})
