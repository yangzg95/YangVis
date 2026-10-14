/**
 * 幻灯片工作台里与「版式形状」有关的纯逻辑：把任意来源的页面补成表单要的形状，
 * 以及增删格子时的那几条上下限。
 *
 * 单独成模块是因为这两件事都容易被改错而看不出：切版式时补的骨架少了，页面上就是
 * 一片填不了的空格；上下限与渲染器（``backend/app/services/slides/html.py`` 的
 * ``MAX_FLOW`` / ``MAX_ARCH_*``）不一致时，前端放行、服务端裁剪，用户看到的是
 * 「刚写的字没了」。规则集中在这一处，由同目录的 slidePages.test.ts 钉住。
 */
import type {
  SlideArchLayer,
  SlideFlowNode,
  SlideLayout,
  SlidePage,
  SlideStep,
  SlideTable,
} from '@/api'

/**
 * 编辑中的页面。与服务端结构差两点：新加的页还没有 `sid`（保存时由服务端发放），
 * 文本字段在本地一律归一成字符串/数组 —— 表单直接绑它们，省掉满屏的 `|| ''`。
 */
export interface EditablePage {
  sid?: string
  layout: SlideLayout
  kicker: string
  title: string
  subtitle: string
  bullets: string[]
  table: SlideTable
  steps: SlideStep[]
  flow: SlideFlowNode[]
  layers: SlideArchLayer[]
  takeaway: string
  asset_id: string | null
  notes: string
}

/** normalizePage 的入参：任何来源的页面片段，字段可能缺、也可能是 null。 */
export type RawPage = { [K in keyof EditablePage]?: EditablePage[K] | null }

// 与渲染器同源的几个数：超过就裁、不足就补，两边数不一样就是缺陷。
export const MAX_STEPS = 5
export const MAX_FLOW_NODES = 5
export const MAX_ARCH_LAYERS = 5
export const MAX_ARCH_BLOCKS = 4
export const MAX_TABLE_COLUMNS = 4
export const MAX_TABLE_ROWS = 6
export const MIN_TABLE_COLUMNS = 2
/** 链路和分层要两格以上才成图，所以切版式时补齐到 2、删到 2 就停。 */
export const MIN_STRUCTURED = 2

export function toEditable(page: SlidePage): EditablePage {
  return { sid: page.sid, ...normalizePage(page) }
}

/**
 * 把任意来源的页面补成表单要的形状。
 *
 * 服务端回来的数据本来就干净，走这里的是「源码」模式里手写的 JSON：字段可能整个
 * 缺失，表格可能是数组也可能是对象。缺省值集中这一处，加字段只改这里。
 */
export function normalizePage(raw: RawPage): Omit<EditablePage, 'sid'> {
  const table = raw.table
  return {
    layout: (raw.layout || 'bullets') as SlideLayout,
    kicker: raw.kicker ?? '',
    title: raw.title ?? '',
    subtitle: raw.subtitle ?? '',
    bullets: Array.isArray(raw.bullets) ? raw.bullets.map((value) => String(value ?? '')) : [],
    table: {
      columns: Array.isArray(table?.columns) ? table!.columns.map((value) => String(value ?? '')) : [],
      rows: Array.isArray(table?.rows)
        ? table!.rows.map((row) => (Array.isArray(row) ? row.map((cell) => String(cell ?? '')) : []))
        : [],
    },
    steps: Array.isArray(raw.steps)
      ? raw.steps.map((step) =>
          typeof step === 'string'
            ? { title: step, desc: '' }
            : { title: step?.title ?? '', desc: step?.desc ?? '' },
        )
      : [],
    flow: Array.isArray(raw.flow)
      ? raw.flow.map((node) =>
          typeof node === 'string'
            ? { title: node, desc: '', via: '' }
            : { title: node?.title ?? '', desc: node?.desc ?? '', via: node?.via ?? '' },
        )
      : [],
    layers: Array.isArray(raw.layers)
      ? raw.layers.map((layer) =>
          typeof layer === 'string'
            ? { name: layer, blocks: [] }
            : {
                name: layer?.name ?? '',
                blocks: Array.isArray(layer?.blocks)
                  ? layer!.blocks.map((block) => String(block ?? ''))
                  : [],
              },
        )
      : [],
    takeaway: raw.takeaway ?? '',
    asset_id: raw.asset_id || null,
    notes: raw.notes ?? '',
  }
}

/** 新加的一页：只有版式，其余按版式给一副能直接填的骨架。 */
export function createPage(layout: SlideLayout = 'bullets'): EditablePage {
  const page: EditablePage = {
    layout,
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
  }
  return topUpForLayout(page)
}

/**
 * 切到需要多格才成图的版式时补齐骨架，已有内容一律不动。
 *
 * 表格页一列都没有时「加一行」是灰的，步骤页则一格都填不了，链路和分层不足两格在
 * 渲染器里会整页退回要点页——所以这里补，而不是等用户自己找空格子。
 */
export function topUpForLayout(page: EditablePage): EditablePage {
  page.bullets = page.bullets || []
  if (page.layout === 'table' && !page.table.columns.length) {
    page.table = { columns: ['', ''], rows: [['', '']] }
  }
  if (page.layout === 'steps' && !page.steps.length) {
    page.steps = [
      { title: '', desc: '' },
      { title: '', desc: '' },
    ]
  }
  while (page.layout === 'flow' && page.flow.length < MIN_STRUCTURED) {
    page.flow.push({ title: '', desc: '', via: '' })
  }
  while (page.layout === 'arch' && page.layers.length < MIN_STRUCTURED) {
    page.layers.push({ name: '', blocks: [''] })
  }
  return page
}

/** 一格链路只有走到下一格的箭头才需要条件，最后一格没有箭头。 */
export function isLastFlowNode(page: EditablePage, index: number): boolean {
  return index === page.flow.length - 1
}

export function canAddFlowNode(page: EditablePage): boolean {
  return page.flow.length < MAX_FLOW_NODES
}

export function canAddArchLayer(page: EditablePage): boolean {
  return page.layers.length < MAX_ARCH_LAYERS
}

export function canAddArchBlock(layer: SlideArchLayer): boolean {
  return layer.blocks.length < MAX_ARCH_BLOCKS
}

/** 链路和分层删到底就只剩一格，那时这一版式已经不成立，按钮该是灰的。 */
export function canRemoveStructured(page: EditablePage): boolean {
  return (
    page.layout === 'flow'
      ? page.flow.length > MIN_STRUCTURED
      : page.layers.length > MIN_STRUCTURED
  )
}

export function canRemoveArchBlock(layer: SlideArchLayer): boolean {
  return layer.blocks.length > 1
}

export function canAddColumn(page: EditablePage): boolean {
  return page.table.columns.length < MAX_TABLE_COLUMNS
}

export function canRemoveColumn(page: EditablePage): boolean {
  return page.table.columns.length > MIN_TABLE_COLUMNS
}

export function canAddRow(page: EditablePage): boolean {
  return !!page.table.columns.length && page.table.rows.length < MAX_TABLE_ROWS
}

export function canAddStep(page: EditablePage): boolean {
  return page.steps.length < MAX_STEPS
}

export function appendBullet(page: EditablePage): void {
  page.bullets = [...(page.bullets || []), '']
}

export function appendStep(page: EditablePage): boolean {
  if (!canAddStep(page)) return false
  page.steps.push({ title: '', desc: '' })
  return true
}

/** 加一格链路：到顶返回 false，调用方据此决定要不要标脏。 */
export function appendFlowNode(page: EditablePage): boolean {
  if (!canAddFlowNode(page)) return false
  page.flow.push({ title: '', desc: '', via: '' })
  return true
}

/** 一层至少给一个方块位：只填层名的那一条在页面上就是一行字。 */
export function appendArchLayer(page: EditablePage): boolean {
  if (!canAddArchLayer(page)) return false
  page.layers.push({ name: '', blocks: [''] })
  return true
}

export function appendArchBlock(page: EditablePage, index: number): boolean {
  const layer = page.layers[index]
  if (!layer || !canAddArchBlock(layer)) return false
  layer.blocks.push('')
  return true
}

/** 删到下限不动；调用方的按钮 disabled 读的是同一组数。 */
export function removeFlowNode(page: EditablePage, index: number): boolean {
  if (!canRemoveStructured(page)) return false
  page.flow.splice(index, 1)
  return true
}

export function removeArchLayer(page: EditablePage, index: number): boolean {
  if (!canRemoveStructured(page)) return false
  page.layers.splice(index, 1)
  return true
}

export function popArchBlock(page: EditablePage, index: number): boolean {
  const layer = page.layers[index]
  if (!layer || !canRemoveArchBlock(layer)) return false
  layer.blocks.pop()
  return true
}

export function appendColumn(page: EditablePage): boolean {
  if (!canAddColumn(page)) return false
  page.table.columns.push('')
  // 行列数要始终对齐：渲染器按列数补格，编辑器里先保证形状是对的。
  page.table.rows.forEach((row) => row.push(''))
  return true
}

export function removeColumn(page: EditablePage): boolean {
  if (!canRemoveColumn(page)) return false
  page.table.columns.pop()
  page.table.rows.forEach((row) => row.pop())
  return true
}

export function appendRow(page: EditablePage): boolean {
  if (!canAddRow(page)) return false
  page.table.rows.push(page.table.columns.map(() => ''))
  return true
}
