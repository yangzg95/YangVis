/**
 * mermaid 全局单例的主题配置。
 *
 * mermaid.initialize 改的是全局单例（MermaidCard 和 DbErdTab 各自懒加载，
 * 拿到的是同一个实例），所以配置不能只在首次加载时给一次：主题切换后要
 * 重新 initialize 并重绘。图背景统一透明，卡片/画布自己的 --surface 透出来；
 * 暗色节点色对齐设计系统的 surface-raised / hairline / text-1。
 */
export function applyMermaidTheme(
  mermaid: { initialize: (config: Record<string, unknown>) => void },
  isDark: boolean,
): void {
  mermaid.initialize({
    startOnLoad: false,
    // strict：标签里的 HTML 一律转义。模型输出不可信，这条不能松。
    securityLevel: 'strict',
    theme: isDark ? 'dark' : 'default',
    themeVariables: isDark
      ? {
          darkMode: true,
          background: 'transparent',
          primaryColor: '#232e38',
          primaryBorderColor: '#4a5866',
          primaryTextColor: '#d8dee4',
          lineColor: '#8b98a5',
          secondaryColor: '#1b2530',
          tertiaryColor: '#1b2530',
          fontFamily:
            "'IBM Plex Sans', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif",
        }
      : {
          background: 'transparent',
        },
  })
}
