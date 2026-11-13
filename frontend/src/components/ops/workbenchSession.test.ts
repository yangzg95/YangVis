import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  dropWorkbenchSql,
  loadWorkbenchSql,
  loadWorkbenchState,
  restoredQuerySeq,
  saveWorkbenchSql,
  saveWorkbenchState,
  type PersistedTab,
  type WorkbenchState,
} from '@/components/ops/workbenchSession'
import { storageKeys } from '@/utils/storage'

function fakeStorage(): Storage {
  const map = new Map<string, string>()
  return {
    get length() {
      return map.size
    },
    key: (index: number) => [...map.keys()][index] ?? null,
    getItem: (name: string) => (map.has(name) ? map.get(name)! : null),
    setItem: (name: string, value: string) => {
      map.set(name, String(value))
    },
    removeItem: (name: string) => {
      map.delete(name)
    },
    clear: () => map.clear(),
  }
}

const TABLE: PersistedTab = {
  key: 'data:1:shop:orders',
  kind: 'data',
  title: 'orders @ 商城',
  connId: 1,
  schema: 'shop',
  table: 'orders',
}

const QUERY: PersistedTab = {
  key: 'query:1:2',
  kind: 'query',
  title: '查询 2 @ 商城',
  connId: 1,
  schema: 'shop',
}

function state(tabs: PersistedTab[], activeKey?: string): WorkbenchState {
  return { tabs, activeKey }
}

beforeEach(() => {
  vi.stubGlobal('sessionStorage', fakeStorage())
})

describe('工作台现场', () => {
  it('页签列表存进去原样读回来', () => {
    saveWorkbenchState(state([TABLE, QUERY], QUERY.key))
    expect(loadWorkbenchState()).toEqual({ tabs: [TABLE, QUERY], activeKey: QUERY.key })
  })

  it('页签全关掉就是把现场清掉，下次进来是干净的工作台', () => {
    saveWorkbenchState(state([TABLE]))
    saveWorkbenchState(state([]))
    expect(loadWorkbenchState()).toBeNull()
    expect(sessionStorage.getItem(storageKeys.opsWorkbenchState)).toBeNull()
  })

  it('存储被改坏、或页签缺字段时整条不认，不让半截现场把工作台带崩', () => {
    sessionStorage.setItem(storageKeys.opsWorkbenchState, '{not json')
    expect(loadWorkbenchState()).toBeNull()

    sessionStorage.setItem(
      storageKeys.opsWorkbenchState,
      JSON.stringify({ tabs: [{ key: 'x', kind: 'data' }, TABLE] }),
    )
    expect(loadWorkbenchState()).toEqual({ tabs: [TABLE], activeKey: undefined })

    sessionStorage.setItem(storageKeys.opsWorkbenchState, JSON.stringify({ tabs: [{ key: 'x' }] }))
    expect(loadWorkbenchState()).toBeNull()
  })

  it('查询序号水位线取每个连接的最大号，非查询页签不参与', () => {
    const tabs: PersistedTab[] = [
      { ...QUERY, key: 'query:1:2' },
      { ...QUERY, key: 'query:1:7' },
      { ...QUERY, connId: 2, key: 'query:2:1' },
      TABLE,
      // key 结尾不是数字的历史形状：忽略，不让它把水位线写成 NaN。
      { ...QUERY, key: 'query:3:old' },
    ]
    expect(restoredQuerySeq(tabs)).toEqual({ '1': 7, '2': 1 })
  })

  it('草稿按页签各存一条，空草稿与关页签都把它清掉', () => {
    saveWorkbenchSql(QUERY.key, 'SELECT 1')
    saveWorkbenchSql(TABLE.key, 'DBSIZE')
    expect(loadWorkbenchSql(QUERY.key)).toBe('SELECT 1')
    expect(loadWorkbenchSql(TABLE.key)).toBe('DBSIZE')

    dropWorkbenchSql(QUERY.key)
    expect(loadWorkbenchSql(QUERY.key)).toBe('')

    saveWorkbenchSql(TABLE.key, '   ')
    expect(loadWorkbenchSql(TABLE.key)).toBe('')
  })
})
