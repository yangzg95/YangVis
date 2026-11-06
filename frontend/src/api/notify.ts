/**
 * 消息通知域：Webhook 机器人渠道（飞书/企微/钉钉/自定义）的增删改、连通测试与发送记录。
 * 事件词表由后端给出，前端只负责渲染勾选组——订阅一个不存在的事件等于永远收不到通知，
 * 所以词表不在这里抄一份。
 */
import { request } from './http'
import type { TestResult } from './settings'

// ---- 通知渠道 ----------------------------------------------------------------

export type NotifyKind = 'feishu' | 'wecom' | 'dingtalk' | 'generic' | 'huawei'

/** 华为 HMS Push 的推送目标：主题广播或单设备定向。 */
export type HuaweiTargetType = 'topic' | 'token'

export interface NotifyEventInfo {
  key: string
  label: string
  /** 新建渠道时的默认勾选：失败类默认开，成功类默认关。 */
  default_on: boolean
}

export interface NotifyChannelItem {
  id: number
  name: string
  kind: NotifyKind
  /** 掩码形式的 webhook 地址；编辑时留空表示不改动已保存的地址。 */
  webhook_url: string
  /** 地址解不开（ENCRYPTION_KEY 换过）时的原因，非空即该渠道发不出消息。 */
  webhook_url_error?: string | null
  has_secret: boolean
  secret_error?: string | null
  events: string[]
  enabled: boolean
  last_test_at?: string | null
  last_test_ok?: boolean | null
  last_test_error?: string | null
  created_at: string
  updated_at: string
}

export interface NotifyChannelPayload {
  name?: string
  kind?: NotifyKind
  /** 新建必填；编辑时未传或留空都保持原地址。 */
  webhook_url?: string
  /** 仅钉钉加签机器人与飞书（可选签名）用得上。 */
  secret?: string
  events?: string[]
  enabled?: boolean
}

/** 华为渠道的序列化视图：client_id 是掩码，secret 只知道有没有。 */
export interface HuaweiChannelItem {
  id: number
  name: string
  kind: 'huawei'
  app_id: string
  client_id_masked: string
  has_client_secret: boolean
  client_id_error?: string | null
  client_secret_error?: string | null
  target_type: HuaweiTargetType
  /** topic 名明文返回；token 模式下是设备 Push Token 的掩码。 */
  target: string
  events: string[]
  enabled: boolean
  last_test_at?: string | null
  last_test_ok?: boolean | null
  last_test_error?: string | null
  created_at: string
  updated_at: string
}

export interface HuaweiChannelCreatePayload {
  name: string
  app_id: string
  client_id: string
  client_secret: string
  target_type: HuaweiTargetType
  target: string
  events: string[]
  enabled: boolean
}

/** 编辑华为渠道：所有字段可选；凭据/目标留空或回传掩码都表示保持原样。 */
export interface HuaweiChannelUpdatePayload {
  name?: string
  app_id?: string
  client_id?: string
  client_secret?: string
  target_type?: HuaweiTargetType
  target?: string
  events?: string[]
  enabled?: boolean
}

export interface NotifyLogItem {
  id: number
  channel_id: number
  channel_name: string
  event: string
  title: string
  status: 'sent' | 'failed'
  error?: string | null
  elapsed_ms?: number | null
  created_at: string
}

export const notifyApi = {
  events: () => request<{ items: NotifyEventInfo[]; total: number }>({ url: '/notify/events' }),
  listChannels: () =>
    request<{ items: NotifyChannelItem[]; total: number }>({ url: '/notify/channels' }),
  createChannel: (payload: NotifyChannelPayload) =>
    request<NotifyChannelItem>({ url: '/notify/channels', method: 'POST', data: payload }),
  updateChannel: (id: number, payload: NotifyChannelPayload) =>
    request<NotifyChannelItem>({ url: `/notify/channels/${id}`, method: 'PUT', data: payload }),
  removeChannel: (id: number) =>
    request<null>({ url: `/notify/channels/${id}`, method: 'DELETE' }),
  testChannel: (id: number) =>
    request<TestResult>({
      url: `/notify/channels/${id}/test`,
      method: 'POST',
      // 机器人所在群可能在外网，发送器最坏还要重试一次；全局 30 秒太紧。
      timeout: 60000,
      skipErrorToast: true,
    }),
  // ---- 华为 HMS Push 渠道（字段与 webhook 渠道完全不同，单列一组端点）----
  createHuaweiChannel: (payload: HuaweiChannelCreatePayload) =>
    request<HuaweiChannelItem>({ url: '/notify/channels/huawei', method: 'POST', data: payload }),
  updateHuaweiChannel: (id: number, payload: HuaweiChannelUpdatePayload) =>
    request<HuaweiChannelItem>({
      url: `/notify/channels/${id}/huawei`,
      method: 'PUT',
      data: payload,
    }),
  // silent 用于列表加载时的后台补拉：个别渠道详情失败不该刷一串错误 toast。
  getHuaweiChannel: (id: number, silent = false) =>
    request<HuaweiChannelItem>({
      url: `/notify/channels/${id}/huawei`,
      skipErrorToast: silent,
    }),
  logs: (limit = 50) =>
    request<{ items: NotifyLogItem[]; total: number }>({ url: '/notify/logs', params: { limit } }),
}
