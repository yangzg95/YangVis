import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi, type UserInfo } from '@/api'
import { clearAccessToken, getAccessToken, isAuthenticated, setAccessToken } from '@/utils/auth'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(getAccessToken())
  const userInfo = ref<UserInfo | null>(null)

  const loggedIn = computed(() => Boolean(accessToken.value))
  const displayName = computed(
    () => userInfo.value?.nickname || userInfo.value?.username || '未登录',
  )
  // 资料还没拉回来时按非管理员处理：菜单晚一拍出现，好过闪一下再消失。
  // 真正的权限判定在后端（users 路由整个挂了 require_admin）。
  const isAdmin = computed(() => Boolean(userInfo.value?.roles?.includes('admin')))
  // 运维写通行证：确认执行、PTY、文件写等入口的置灰依据。与后端
  // can_ops_write 同义——管理员是天然超集；真正的闸门在后端四处强制。
  const canOpsWrite = computed(() => isAdmin.value || Boolean(userInfo.value?.ops_write))

  const login = async (
    username: string,
    password: string,
    captchaId: string,
    captchaCode: string,
  ) => {
    const result = await authApi.login({
      username,
      password,
      captcha_id: captchaId,
      captcha_code: captchaCode,
    })
    accessToken.value = result.access_token
    setAccessToken(result.access_token)
    userInfo.value = result.user_info ?? null
    return result
  }

  /**
   * 从服务端刷新用户资料。永远不会抛异常：这里失败不应该影响一个
   * 已经拿到 token 的会话继续使用。
   */
  const fetchCurrentUser = async () => {
    if (!isAuthenticated()) {
      return null
    }
    try {
      userInfo.value = await authApi.me()
    } catch {
      // 401 已经由 axios 拦截器统一处理了。
    }
    return userInfo.value
  }

  const logout = async () => {
    try {
      await authApi.logout()
    } catch { /* 后端可能连不上 —— 本地状态照样要清掉 */ }
    accessToken.value = null
    userInfo.value = null
    clearAccessToken()
  }

  return {
    accessToken,
    userInfo,
    loggedIn,
    displayName,
    isAdmin,
    canOpsWrite,
    login,
    fetchCurrentUser,
    logout,
  }
})
