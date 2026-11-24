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

/**
 * blob 响应的错误体仍是 JSON 信封：`responseType: 'blob'` 时 axios 拦截器读不到
 * blob 里的 message，这里自己拆开读；不是 JSON 就退回通用口径。
 * TTS 试听、AI 生图这类「成功回二进制、失败回 JSON」的接口共用这一份。
 */
export async function blobErrorMessage(err: unknown, fallback: string): Promise<string> {
  const data = (err as { response?: { data?: unknown } } | null)?.response?.data
  if (data instanceof Blob && data.type.includes('json')) {
    try {
      const body = JSON.parse(await data.text()) as { message?: string }
      if (body.message) return body.message
    } catch {
      // 不是 JSON 就走通用口径
    }
  }
  return errorText(err, fallback)
}
