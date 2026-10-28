import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { storageKeys } from '@/utils/storage'

/** 外观模式：纯前端偏好，只落 localStorage，不进后端用户设置。 */
export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = storageKeys.themeMode

function readStoredMode(): ThemeMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    // 隐私模式等场景拿不到 localStorage，落回浅色默认。
    return 'light'
  }
}

// 模块级单例 + 立即上 DOM：本模块在 main.ts 挂载前就被 import，
// <html data-theme> 先于首屏渲染就位，不会先闪一下白底再翻暗。
const mode = ref<ThemeMode>(readStoredMode())

function applyMode(value: ThemeMode) {
  document.documentElement.dataset.theme = value
}

applyMode(mode.value)

export const useThemeStore = defineStore('theme', () => {
  const isDark = computed(() => mode.value === 'dark')

  function setMode(value: ThemeMode) {
    if (value === mode.value) return
    mode.value = value
    applyMode(value)
    try {
      localStorage.setItem(STORAGE_KEY, value)
    } catch {
      // 存不下就只保活本次会话。
    }
  }

  function toggle() {
    setMode(mode.value === 'dark' ? 'light' : 'dark')
  }

  return { mode, isDark, setMode, toggle }
})
