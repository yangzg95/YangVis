/** blob 下载的保存工具。 */
import { handleUnauthorized } from '@/api'
import { getAccessToken } from '@/utils/auth'

interface BlobResponse {
  data: Blob
  // axios 的 headers 类型是 AxiosResponseHeaders，结构上是 string index + 可能 undefined。
  headers: Record<string, unknown>
}

/**
 * 保存一个 blob 响应为本地文件。
 *
 * 后端下载端点出错时返回的不是二进制而是统一信封 JSON（HTTP 200），
 * 这里按 content-type 识别出来并抛错，交给调用方提示——否则用户会
 * 下载到一个内容是 `{"code":-1,...}` 的假文件。
 */
/** 把一段文本直接保存成本地文件（报告导出这类不经后端的内容下载）。 */
export function saveTextFile(name: string, text: string, mime = 'text/markdown'): void {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}

/** 浏览器是否支持流式落盘（Chrome/Edge + 安全上下文，即 HTTPS 或 localhost）。 */
export function supportsStreamingSave(): boolean {
  return typeof window.showSaveFilePicker === 'function'
}

/**
 * 弹「另存为」对话框。用户取消返回 null；SecurityError（无用户手势激活、
 * 非安全上下文）原样上抛，由调用方回落 blob 路径。
 *
 * 注意：必须在用户手势的激活窗口内调用（点击后约 5 秒），调用点前面不能
 * 插任何网络 await。
 */
export async function pickSaveTarget(suggestedName: string): Promise<FileSystemFileHandle | null> {
  try {
    return await window.showSaveFilePicker({ suggestedName })
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return null
    throw err
  }
}

/** 删除占位文件。老 Chrome 没有 remove()，失败就算了（无非留个空文件）。 */
async function removeQuietly(handle: FileSystemFileHandle): Promise<void> {
  try {
    await handle.remove()
  } catch {
    /* 不支持或已被删，忽略 */
  }
}

/**
 * fetch 流式下载并直接写盘。不经 axios（它没有流式响应体），鉴权头手动带。
 *
 * 取消/失败不会留半截文件：FileSystemWritableFileStream 先写 swap 文件，
 * close 才落正式文件，abort 直接丢弃。
 */
export async function streamToDisk(
  url: string,
  handle: FileSystemFileHandle,
  opts: { signal: AbortSignal; onProgress: (loaded: number, total: number) => void },
): Promise<void> {
  const token = getAccessToken()
  const res = await fetch(url, {
    headers: token ? { Authorization: token } : undefined,
    signal: opts.signal,
  })
  // fetch 绕过了 axios 拦截器，401 要自己接管（对齐 streamSse 的处理）。
  if (res.status === 401) {
    handleUnauthorized()
    throw new Error('未登录或登录已过期')
  }
  // 后端出错时给的是 HTTP 200 + JSON 信封，和二进制流靠 content-type 区分。
  if (res.headers.get('content-type')?.includes('application/json')) {
    const envelope = await res.json().catch(() => null)
    await removeQuietly(handle)
    throw new Error(envelope?.message || '下载失败')
  }
  if (!res.ok || !res.body) {
    await removeQuietly(handle)
    throw new Error(`下载失败（HTTP ${res.status}）`)
  }

  // 上面的检查都通过才建 writable：否则错误响应也会留下一个 0 字节占位文件。
  const writable = await handle.createWritable()
  const total = Number(res.headers.get('content-length') || 0)
  let loaded = 0
  try {
    const reader = res.body.getReader()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      // 必须 await：背压全靠它，否则快网络会把整个文件堆进流的内部队列，
      // 等于又把 blob 缓冲 reinvent 了一遍。
      await writable.write(value)
      loaded += value.byteLength
      opts.onProgress(loaded, total)
    }
    await writable.close()
  } catch (err) {
    try {
      await writable.abort()
    } catch {
      /* 流已关闭/已出错，忽略 */
    }
    if (loaded === 0) await removeQuietly(handle)
    throw err
  }
}

export async function saveBlobResponse(res: BlobResponse, fallbackName: string): Promise<void> {
  if (res.data.type.includes('application/json')) {
    const envelope = JSON.parse(await res.data.text())
    throw new Error(envelope.message || '下载失败')
  }

  const disposition = String(res.headers['content-disposition'] || '')
  // RFC 5987：中文名在 filename* 里，优先用它；filename 只是 ASCII 兜底。
  const starMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i)
  const name =
    (starMatch ? decodeURIComponent(starMatch[1]) : '') || plainMatch?.[1] || fallbackName

  const url = URL.createObjectURL(res.data)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}
