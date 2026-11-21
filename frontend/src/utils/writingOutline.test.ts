import { describe, expect, it } from 'vitest'
import type { OutlineNode } from '@/api/writing'
import {
  appendVolume,
  cloneOutline,
  countNodes,
  findLeaf,
  flattenLeaves,
  isCid,
  leafKind,
  leafTitles,
  moveLeaf,
  removeLeaf,
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
