/**
 * 幻灯片「导出 PDF」：把渲染好的那份 HTML 开成独立标签页，再触发那份文档自己的打印。
 *
 * 走打印而不是后端出文件：版面是渲染器算出来的，`@media print` 已经写好一页一屏，
 * 浏览器打印面板选「另存为 PDF」就是成品，不需要在服务端再养一套无头渲染。
 * 也不在放映的 iframe 里调 print —— 那种打法浏览器会把外层应用一起排进纸里。
 */

/**
 * 给独立文档补一个指向本站的 `<base>`。
 *
 * 文档里的配图是 `/uploads/...` 这种绝对路径引用：srcdoc 的预览 iframe 继承宿主
 * 页面的地址，解析结果天然就是本站；换成 blob 地址开的标签页就没有这层继承，
 * 相对解析会指向 blob 那边，图取不到。补上本站的 base 才和预览看到的一样。
 */
export function withDocumentBase(html: string, origin: string): string {
  if (!html.includes('<head>')) return html
  return html.replace('<head>', `<head><base href="${origin}/">`)
}

export function printSlideDeck(html: string): boolean {
  const blob = new Blob([withDocumentBase(html, window.location.origin)], {
    type: 'text/html;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const tab = window.open(url, '_blank')
  if (!tab) {
    // 弹窗被拦：URL 留着也没人用。
    URL.revokeObjectURL(url)
    return false
  }
  // 等载入完再打：配图是 <img src> 外链，早一步打印面板里就是空块。
  tab.addEventListener('load', () => {
    window.setTimeout(() => {
      tab.focus()
      tab.print()
    }, 400)
    URL.revokeObjectURL(url)
  })
  return true
}
