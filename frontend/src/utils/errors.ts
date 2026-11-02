/** 错误文案的取用口径。
 *
 * 后端的业务错误走 envelope（`{ code, message }`），网络/前置错误走 Error，
 * 两者都有 `message`；页面只关心「能给用户看哪一句」，所以统一从这里取，
 * 不要再在每个页面里复制一份同样的类型收窄。
 */
export function errorText(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'message' in err) {
    const text = (err as { message?: unknown }).message
    if (typeof text === 'string' && text) return text
  }
  return fallback
}
