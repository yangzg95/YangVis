import { storageKeys } from '@/utils/storage'
import type { TabRequest } from './tabs'

/**
 * 工作台现场：页签列表 + 当前激活项 + 查询页编辑器里的 SQL。
 *
 * 存在 sessionStorage 而不是 localStorage——现场属于「这一次浏览」：刷新、误关
 * 浏览器标签页要能原样回来，但换个人登录不该看到上一个人开的表。
 */

/** 落盘的页签：conn 只记 id，恢复时按 id 现认台账里的连接快照。 */
export interface PersistedTab {
  key: string
  kind: TabRequest['kind']
  title: string
  connId: number
  schema?: string
  table?: string
  db?: number
  rkey?: string
}

export interface WorkbenchState {
  tabs: PersistedTab[]
  activeKey?: string
}

function store(): Storage | undefined {
  try {
    return globalThis.sessionStorage
  } catch {
    // 隐私模式之类的极端环境直接拒绝访问 sessionStorage：现场存不下去，功能照旧。
    return undefined
  }
}

export function saveWorkbenchState(state: WorkbenchState): void {
  const box = store()
  if (!box) return
  try {
    if (!state.tabs.length) {
      box.removeItem(storageKeys.opsWorkbenchState)
      return
    }
    box.setItem(storageKeys.opsWorkbenchState, JSON.stringify(state))
  } catch {
    // 配额满了就放弃这一次记录，别把正常操作带崩。
  }
}

export function loadWorkbenchState(): WorkbenchState | null {
  const box = store()
  if (!box) return null
  try {
    const raw = box.getItem(storageKeys.opsWorkbenchState)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<WorkbenchState>
    if (!Array.isArray(parsed.tabs) || !parsed.tabs.length) return null
    // 只认还带 key/kind/connId 的项：形状不对的（版本改动、手改存储）整条丢。
    const tabs = parsed.tabs.filter(
      (item): item is PersistedTab =>
        !!item
        && typeof item.key === 'string'
        && typeof item.kind === 'string'
        && typeof item.connId === 'number',
    )
    if (!tabs.length) return null
    return { tabs, activeKey: parsed.activeKey }
  } catch {
    return null
  }
}

/**
 * 查询序号水位线：恢复的页签里每个连接已开到第几号。
 * 不补这一步，重开页面后新建查询会从「查询 1」起算，key 撞上已恢复的那张，
 * 结果是新页签开不出来、只把旧的激活。
 */
export function restoredQuerySeq(tabs: PersistedTab[]): Record<string, number> {
  const seq: Record<string, number> = {}
  for (const tab of tabs) {
    if (tab.kind !== 'query') continue
    const parts = tab.key.split(':')
    const last = Number(parts[parts.length - 1])
    if (!Number.isFinite(last)) continue
    const connId = String(tab.connId)
    if (last > (seq[connId] ?? 0)) seq[connId] = last
  }
  return seq
}

// ---- 查询页编辑器内容 --------------------------------------------------------------

function sqlKey(tabKey: string): string {
  return `${storageKeys.opsWorkbenchSql}:${tabKey}`
}

export function saveWorkbenchSql(tabKey: string, text: string): void {
  const box = store()
  if (!box) return
  try {
    if (!text.trim()) {
      box.removeItem(sqlKey(tabKey))
      return
    }
    box.setItem(sqlKey(tabKey), text)
  } catch {
    // 同上：记不上就算了。
  }
}

export function loadWorkbenchSql(tabKey: string): string {
  const box = store()
  if (!box) return ''
  try {
    return box.getItem(sqlKey(tabKey)) ?? ''
  } catch {
    return ''
  }
}

/** 页签关掉就把它的草稿一起删掉，别在存储里留一份没人认领的 SQL。 */
export function dropWorkbenchSql(tabKey: string): void {
  const box = store()
  if (!box) return
  try {
    box.removeItem(sqlKey(tabKey))
  } catch {
    // 删不掉也只是留了条脏记录，恢复时按 key 现认，不会复活页签。
  }
}
