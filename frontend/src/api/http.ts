/**
 * HTTP 底座：唯一的 axios 实例、`{ code, message, data }` envelope 的拆包、401 的统一出口，
 * 外加各域发请求时共用的 request。实例只在这里 create 一次，拦截器因此全国只有一套。
 * SSE 拉流不走这里（它没有 envelope 可拆），见 sse.ts。
 */
import axios, { type AxiosRequestConfig, type AxiosInstance } from 'axios'
import { message } from 'ant-design-vue'
import { clearAccessToken, getAccessToken } from '@/utils/auth'


export interface ApiEnvelope<T = unknown> {
  code: number
  message: string
  data: T
}

/** 在 axios 自身配置之上追加的单请求级开关。 */
export interface RequestConfig extends AxiosRequestConfig {
  /** 屏蔽全局错误提示，让调用方自己决定如何呈现这个错误。 */
  skipErrorToast?: boolean
}

const LOGIN_PATH = '/login'

/** 同一文案在窗口期内只弹一次：轮询失败时后端通常连着报同一个错，
 * 不做去重会一秒一条把整屏刷满。 */
const TOAST_DEDUPE_MS = 3000
let lastToastText = ''
let lastToastAt = 0

function errorToast(text: string) {
  const now = Date.now()
  if (text === lastToastText && now - lastToastAt < TOAST_DEDUPE_MS) return
  lastToastText = text
  lastToastAt = now
  message.error(text)
}

const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

/** token 已失效，清除后跳转回登录页。fetch 直连（流式下载/SSE）绕过 axios
 * 拦截器，也要用它，所以导出。 */
export function handleUnauthorized() {
  clearAccessToken()
  if (window.location.pathname !== LOGIN_PATH) {
    window.location.replace(LOGIN_PATH)
  }
}

http.interceptors.request.use((config) => {
  const accessToken = getAccessToken()
  if (accessToken) {
    config.headers.Authorization = accessToken
  }
  return config
})

http.interceptors.response.use(
  // 这里返回的是拆包后的 envelope payload，而不是 AxiosResponse，所以返回类型
  // 只能写成 `any`：真正的类型由下面的 `request()` 重新补上。
  (response): any => {
    const payload = response.data as ApiEnvelope | Blob | ArrayBuffer
    if (response.config.responseType === 'blob' || payload instanceof Blob) {
      return response
    }
    const envelope = payload as ApiEnvelope
    if (envelope && typeof envelope === 'object' && 'code' in envelope) {
      if (envelope.code !== 0) {
        if (envelope.code === 401) {
          handleUnauthorized()
        }
        if (!(response.config as RequestConfig).skipErrorToast) {
          errorToast(envelope.message || '请求失败')
        }
        return Promise.reject(envelope)
      }
      return envelope.data
    }
    return payload
  },
  (error) => {
    if (error?.response?.status === 401) {
      handleUnauthorized()
    }
    const detail = error?.response?.data?.message || error.message || '网络错误'
    if (!(error?.config as RequestConfig | undefined)?.skipErrorToast) {
      errorToast(detail)
    }
    return Promise.reject(error)
  },
)

export async function request<T = unknown>(config: RequestConfig): Promise<T> {
  const res = await http.request<unknown, T>(config)
  return res
}
