/**
 * 创作模块的前端字数统计，口径与后端 services/writing/words.py 完全一致：
 * CJK 字符按个计，连续拉丁字母/数字按一个词计，markdown 标记不计。
 * 服务端入库时会重算，这里只用于编辑过程中的即时显示，不信前端报数。
 */
const CJK_RE = /[一-鿿]/g
const LATIN_RE = /[A-Za-z0-9]+/g

export function countWords(text?: string | null): number {
  if (!text) return 0
  return (text.match(CJK_RE)?.length ?? 0) + (text.match(LATIN_RE)?.length ?? 0)
}
