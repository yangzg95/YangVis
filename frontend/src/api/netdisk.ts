/** 百度网盘绑定域：授权跳转、绑定状态、解绑与文本转存。 */
import { request } from './http'

// ---- 智能应用 · 百度网盘绑定 ----------------------------------------------------

export interface NetdiskStatus {
  /** 后端是否已配置 AppKey；false 时前端隐藏整个网盘入口。 */
  configured: boolean
  bound: boolean
  baidu_name: string | null
  expires_at: string | null
}

export const netdiskApi = {
  status: () => request<NetdiskStatus>({ url: '/office/netdisk/status' }),
  authUrl: () => request<{ url: string }>({ url: '/office/netdisk/auth-url' }),
  bind: (code: string) =>
    request<NetdiskStatus>({
      url: '/office/netdisk/bind',
      method: 'POST',
      data: { code },
      // 换 token + 拉用户信息是两走出站请求，慢起来会超过全局 30 秒。
      timeout: 60000,
      skipErrorToast: true,
    }),
  unbind: () => request<null>({ url: '/office/netdisk/unbind', method: 'POST' }),
  /** 把一段文本（分析报告等）存进网盘的 reports/ 子目录，返回完整路径。 */
  saveText: (filename: string, content: string) =>
    request<{ path: string }>({
      url: '/office/netdisk/save-text',
      method: 'POST',
      data: { filename, content },
      timeout: 60000,
    }),
}
