/** access token 的存取。沿用 fe-noetix-operations 的 localStorage 方案。 */

import { storageKeys } from './storage'

// 前缀统一前的旧 key（产品更名前的 jarvis 时代遗留），读一次搬家后即可删除这段迁移（2026-09 引入）。
const LEGACY_ACCESS_TOKEN_KEY = 'jarvis-access-token'

// 隐私模式 / 存储被禁用时 localStorage 访问会直接抛 SecurityError。这里在
// axios 请求拦截器的同步路径上，一次抛错就是所有 API 请求全挂——读写全部
// 降级为「没存到 / 读不到」，让流程走到正常的 401 分支。
function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // 写不进去就当没记住：本次会话仍能用，刷新后重新登录。
  }
}

function safeRemove(key: string) {
  try {
    localStorage.removeItem(key)
  } catch {
    // 同上。
  }
}

export function setAccessToken(accessToken: string) {
  safeSet(storageKeys.accessToken, accessToken)
}

export function getAccessToken(): string | null {
  const token = safeGet(storageKeys.accessToken)
  if (token) return token
  const legacy = safeGet(LEGACY_ACCESS_TOKEN_KEY)
  if (legacy) {
    safeSet(storageKeys.accessToken, legacy)
    safeRemove(LEGACY_ACCESS_TOKEN_KEY)
  }
  return legacy
}

export function clearAccessToken() {
  safeRemove(storageKeys.accessToken)
  safeRemove(LEGACY_ACCESS_TOKEN_KEY)
}

export function isAuthenticated(): boolean {
  return Boolean(getAccessToken())
}
