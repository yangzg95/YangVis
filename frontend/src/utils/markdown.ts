/** 全站共用的 markdown-it 基座。
 *
 * html: false 是安全底线：渲染的内容由模型生成，不能让它吐出的原始 HTML
 * 直接进 DOM，关掉之后 markdown-it 会把 <img onerror=...> 之类整段转义成
 * 文本；自带的 validateLink 也会拦掉 javascript: 这类链接协议。
 *
 * 各页面在这个基座上再加自己的渲染钩子（OpsChat 的代码块动作按钮等），
 * 而不是各自从头配一遍。
 */
import MarkdownIt from 'markdown-it'

export function createMarkdown(): MarkdownIt {
  const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

  // 让链接在新标签页打开，并加上 noopener，避免新页面拿到 window.opener。
  const defaultLinkOpen =
    md.renderer.rules.link_open ||
    ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
  md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
    return defaultLinkOpen(tokens, idx, options, env, self)
  }

  return md
}
