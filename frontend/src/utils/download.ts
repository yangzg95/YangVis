/** blob 下载的保存工具。 */

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
