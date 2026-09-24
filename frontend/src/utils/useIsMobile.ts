/** 移动端（<768px，与登录页断点一致）的共享判定。模块级单例：mql 与监听
 *  全应用只注册一次，各页面拿到的都是同一个响应式 ref。 */
import { ref } from 'vue'

const MOBILE_QUERY = '(max-width: 768px)'
const mql = window.matchMedia(MOBILE_QUERY)
const isMobile = ref(mql.matches)
mql.addEventListener('change', () => {
  isMobile.value = mql.matches
})

export function useIsMobile() {
  return isMobile
}
