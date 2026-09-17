/** access token 的存取。沿用 fe-noetix-operations 的 localStorage 方案。 */

import { storageKeys } from './storage'

// 前缀统一前的旧 key（产品更名前的 jarvis 时代遗留），读一次搬家后即可删除这段迁移（2026-09 引入）。
const LEGACY_ACCESS_TOKEN_KEY = 'jarvis-access-token'

export function setAccessToken(accessToken: string) {
  localStorage.setItem(storageKeys.accessToken, accessToken)
}

export function getAccessToken(): string | null {
  const token = localStorage.getItem(storageKeys.accessToken)
  if (token) return token
  const legacy = localStorage.getItem(LEGACY_ACCESS_TOKEN_KEY)
  if (legacy) {
    localStorage.setItem(storageKeys.accessToken, legacy)
    localStorage.removeItem(LEGACY_ACCESS_TOKEN_KEY)
  }
  return legacy
}

export function clearAccessToken() {
  localStorage.removeItem(storageKeys.accessToken)
  localStorage.removeItem(LEGACY_ACCESS_TOKEN_KEY)
}

export function isAuthenticated(): boolean {
  return Boolean(getAccessToken())
}
