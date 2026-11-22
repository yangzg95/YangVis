import { describe, expect, it } from 'vitest'
import type { OutlineNode } from '@/api/writing'
import {
  appendVolume,
  applyNodeDrop,
  canDropNode,
  cloneOutline,
  countNodes,
  findLeaf,
  flattenLeaves,
  isCid,
  leafKind,
  leafTitles,
  moveLeaf,
  removeLeaf,
  type TreeDrop,
} from '@/utils/writingOutline'

function leaf(cid: string, title = cid, kind: 'chapter' | 'section' = 'chapter'): OutlineNode {
  return { cid, kind, title, synopsis: '', target_words: null, children: [] }
}

function volume(cid: string, children: OutlineNode[]): OutlineNode {
  return { cid, kind: 'volume', title: cid, synopsis: '', target_words: null, children }
}

const novelTree = (): OutlineNode[] => [
  volume('v1', [leaf('c1', '第一章'), leaf('c2', '第二章')]),
  leaf('c3', '第三章'),
]

describe('isCid', () => {
  it('认 p + 16 位 hex，其余一律不认', () => {
    expect(isCid('p0123456789abcdef')).toBe(true)
    expect(isCid('')).toBe(false)
    expect(isCid('garbage')).toBe(false)
    expect(isCid('p0123456789abcde')).toBe(false)
    expect(isCid(null)).toBe(false)
  })
})

describe('leafKind', () => {
  it('小说叶子是章，文章叶子是段落', () => {
    expect(leafKind('novel')).toBe('chapter')
    expect(leafKind('article')).toBe('section')
  })
})

describe('flattenLeaves / findLeaf / leafTitles', () => {
  it('卷节点被跳过，按阅读顺序给叶子', () => {
    expect(flattenLeaves(novelTree()).map((n) => n.cid)).toEqual(['c1', 'c2', 'c3'])
    const article = [leaf('s1', '开头', 'section'), leaf('s2', '正文', 'section')]
    expect(flattenLeaves(article).map((n) => n.cid)).toEqual(['s1', 's2'])
  })

  it('按 cid 找叶子，找不到给 undefined', () => {
    expect(findLeaf(novelTree(), 'c2')?.title).toBe('第二章')
    expect(findLeaf(novelTree(), 'v1')).toBeUndefined()
    expect(findLeaf(novelTree(), 'nope')).toBeUndefined()
  })

  it('leafTitles 展平成 cid/标题对', () => {
    expect(leafTitles(novelTree())).toEqual([
      ['c1', '第一章'],
      ['c2', '第二章'],
      ['c3', '第三章'],
    ])
  })

  it('countNodes 把卷和叶子都算上', () => {
    expect(countNodes(novelTree())).toBe(4)
  })
})

describe('removeLeaf', () => {
  it('能摘卷内章也能摘顶层章', () => {
    const tree = novelTree()
    expect(removeLeaf(tree, 'c1')).toBe(true)
    expect((tree[0] as OutlineNode).children?.map((n) => n.cid)).toEqual(['c2'])
    expect(removeLeaf(tree, 'c3')).toBe(true)
    expect(tree.map((n) => n.cid)).toEqual(['v1'])
  })

  it('摘不到返回 false 且不改树', () => {
    const tree = novelTree()
    expect(removeLeaf(tree, 'nope')).toBe(false)
    expect(flattenLeaves(tree).map((n) => n.cid)).toEqual(['c1', 'c2', 'c3'])
  })
})

describe('moveLeaf', () => {
  it('同层相邻交换', () => {
    const tree = novelTree()
    expect(moveLeaf(tree, 'c1', 1)).toBe(true)
    expect((tree[0] as OutlineNode).children?.map((n) => n.cid)).toEqual(['c2', 'c1'])
    expect(moveLeaf(tree, 'c1', -1)).toBe(true)
    expect((tree[0] as OutlineNode).children?.map((n) => n.cid)).toEqual(['c1', 'c2'])
  })

  it('边界上移动是 no-op', () => {
    const tree = novelTree()
    expect(moveLeaf(tree, 'c1', -1)).toBe(false)
    expect(moveLeaf(tree, 'c3', 1)).toBe(false)
    expect(flattenLeaves(tree).map((n) => n.cid)).toEqual(['c1', 'c2', 'c3'])
  })

  it('卷内章不跨容器移动', () => {
    const tree = novelTree()
    // c2 是卷内最后一个叶子，下移不会跳到顶层 c3 前面。
    expect(moveLeaf(tree, 'c2', 1)).toBe(false)
    expect(tree.map((n) => n.cid)).toEqual(['v1', 'c3'])
  })
})

describe('appendVolume / cloneOutline', () => {
  it('新卷无 cid 无叶子，等服务端发 cid', () => {
    const nodes: OutlineNode[] = []
    const volumeNode = appendVolume(nodes)
    expect(volumeNode.cid).toBe('')
    expect(volumeNode.children).toEqual([])
    expect(nodes).toHaveLength(1)
  })

  it('cloneOutline 是深拷贝，改副本不动原树', () => {
    const tree = novelTree()
    const copy = cloneOutline(tree)
    removeLeaf(copy, 'c1')
    expect(findLeaf(tree, 'c1')).toBeDefined()
    expect(findLeaf(copy, 'c1')).toBeUndefined()
  })
})

describe('applyNodeDrop / canDropNode（文章）', () => {
  const articleTree = (): OutlineNode[] => [
    leaf('s1', '开头', 'section'),
    leaf('s2', '正文', 'section'),
    leaf('s3', '结尾', 'section'),
  ]

  it('根层缝隙重排段落', () => {
    const tree = articleTree()
    const drop: TreeDrop = { dragCid: 's3', dropCid: 's1', placement: 'before' }
    expect(canDropNode(tree, 'article', drop)).toBe(true)
    expect(applyNodeDrop(tree, 'article', drop)).toBe(true)
    expect(tree.map((n) => n.cid)).toEqual(['s3', 's1', 's2'])
  })

  it('inside 落点对扁平段落非法，树不动', () => {
    const tree = articleTree()
    const drop: TreeDrop = { dragCid: 's1', dropCid: 's2', placement: 'inside' }
    expect(canDropNode(tree, 'article', drop)).toBe(false)
    expect(applyNodeDrop(tree, 'article', drop)).toBe(false)
    expect(tree.map((n) => n.cid)).toEqual(['s1', 's2', 's3'])
  })
})

describe('applyNodeDrop / canDropNode（小说）', () => {
  // 两卷 + 一个顶层章，覆盖全部容器组合。
  const fullTree = (): OutlineNode[] => [
    volume('v1', [leaf('c1'), leaf('c2')]),
    volume('v2', [leaf('c3'), leaf('c4')]),
    leaf('c5'),
  ]

  const rootIds = (tree: OutlineNode[]) => tree.map((n) => n.cid)
  const childIds = (tree: OutlineNode[], volumeCid: string) =>
    (tree.find((n) => n.cid === volumeCid) as OutlineNode).children?.map((n) => n.cid)

  it('同卷内缝隙重排', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c1', dropCid: 'c2', placement: 'after' })).toBe(true)
    expect(childIds(tree, 'v1')).toEqual(['c2', 'c1'])
  })

  it('跨卷移动：卷一章落进卷二的缝隙', () => {
    const tree = fullTree()
    const drop: TreeDrop = { dragCid: 'c1', dropCid: 'c3', placement: 'after' }
    expect(canDropNode(tree, 'novel', drop)).toBe(true)
    expect(applyNodeDrop(tree, 'novel', drop)).toBe(true)
    expect(childIds(tree, 'v1')).toEqual(['c2'])
    expect(childIds(tree, 'v2')).toEqual(['c3', 'c1', 'c4'])
  })

  it('章落到卷上=入卷，挂到该卷末尾', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c5', dropCid: 'v2', placement: 'inside' })).toBe(true)
    expect(rootIds(tree)).toEqual(['v1', 'v2'])
    expect(childIds(tree, 'v2')).toEqual(['c3', 'c4', 'c5'])
  })

  it('卷内章落到根层卷缝隙=提为顶层章', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c1', dropCid: 'v2', placement: 'before' })).toBe(true)
    expect(rootIds(tree)).toEqual(['v1', 'c1', 'v2', 'c5'])
    expect(childIds(tree, 'v1')).toEqual(['c2'])
  })

  it('顶层章落到另一顶层章缝隙，根层重排', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c5', dropCid: 'v1', placement: 'before' })).toBe(true)
    expect(rootIds(tree)).toEqual(['c5', 'v1', 'v2'])
  })

  it('卷在根层重排', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'v2', dropCid: 'v1', placement: 'before' })).toBe(true)
    expect(rootIds(tree)).toEqual(['v2', 'v1', 'c5'])
    // 卷内顺序原样跟着走
    expect(childIds(tree, 'v2')).toEqual(['c3', 'c4'])
  })

  it('卷不能嵌套：inside 任何节点都非法', () => {
    const tree = fullTree()
    expect(canDropNode(tree, 'novel', { dragCid: 'v1', dropCid: 'v2', placement: 'inside' })).toBe(false)
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'v1', dropCid: 'v2', placement: 'inside' })).toBe(false)
    expect(canDropNode(tree, 'novel', { dragCid: 'v1', dropCid: 'c1', placement: 'inside' })).toBe(false)
    expect(rootIds(tree)).toEqual(['v1', 'v2', 'c5'])
  })

  it('卷不能挪进卷内：对卷内章的缝隙落点非法', () => {
    const tree = fullTree()
    const drop: TreeDrop = { dragCid: 'v1', dropCid: 'c3', placement: 'after' }
    expect(canDropNode(tree, 'novel', drop)).toBe(false)
    expect(applyNodeDrop(tree, 'novel', drop)).toBe(false)
    expect(rootIds(tree)).toEqual(['v1', 'v2', 'c5'])
  })

  it('章没有 children：章对章 inside 非法', () => {
    const tree = fullTree()
    const drop: TreeDrop = { dragCid: 'c1', dropCid: 'c3', placement: 'inside' }
    expect(canDropNode(tree, 'novel', drop)).toBe(false)
    expect(applyNodeDrop(tree, 'novel', drop)).toBe(false)
    expect(childIds(tree, 'v1')).toEqual(['c1', 'c2'])
  })

  it('原地不动判 no-op：拖自己、末章入本卷、未知 cid', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c1', dropCid: 'c1', placement: 'before' })).toBe(false)
    // c4 是 v2 最后一个孩子，入本卷末尾等于原位
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c4', dropCid: 'v2', placement: 'inside' })).toBe(false)
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'nope', dropCid: 'c1', placement: 'before' })).toBe(false)
    expect(rootIds(tree)).toEqual(['v1', 'v2', 'c5'])
    expect(childIds(tree, 'v1')).toEqual(['c1', 'c2'])
    expect(childIds(tree, 'v2')).toEqual(['c3', 'c4'])
  })

  it('非末位章入本卷 = 挪到卷末，不是 no-op', () => {
    const tree = fullTree()
    expect(applyNodeDrop(tree, 'novel', { dragCid: 'c3', dropCid: 'v2', placement: 'inside' })).toBe(true)
    expect(childIds(tree, 'v2')).toEqual(['c4', 'c3'])
  })

  it('树上找不到的 cid 一律拒绝', () => {
    const tree = fullTree()
    expect(canDropNode(tree, 'novel', { dragCid: 'garbage', dropCid: 'c1', placement: 'before' })).toBe(false)
    expect(canDropNode(tree, 'novel', { dragCid: 'c1', dropCid: '', placement: 'before' })).toBe(false)
  })
})
