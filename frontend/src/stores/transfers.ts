/** SFTP 上传/下载的传输任务管理。
 *
 * 传输是后台任务：从文件面板发起后不阻塞浏览，进度在「传输」标签页里看。
 * store 是全局单例，页面切走、面板关掉都不影响正在跑的请求。
 */
import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import axios from 'axios'
import { message } from 'ant-design-vue'
import { opsApi, sftpDownloadUrl } from '@/api'
import { fmtSize } from '@/utils/format'
import {
  pickSaveTarget,
  saveBlobResponse,
  streamToDisk,
  supportsStreamingSave,
} from '@/utils/download'

export type TransferStatus = 'running' | 'done' | 'error' | 'cancelled'

export interface TransferItem {
  id: number
  kind: 'upload' | 'download'
  serverId: number
  name: string
  remotePath: string
  /** 字节进度。total 为 0 表示后端没给 Content-Length，只显示已传量。 */
  loaded: number
  total: number
  status: TransferStatus
  error: string
  startedAt: number
  controller: AbortController
  /** 平滑后的速率（字节/秒），0 = 样本还不够。 */
  speed: number
  /** 最后一次进度前进的时刻（ms），「多久没动」由它算。 */
  lastProgressAt: number
  /** 上一个速率采样点，只用于计算，不渲染。 */
  sample: { at: number; loaded: number }
}

/** 列表最多保留的条数，早先的完成记录会被挤掉。 */
const KEEP_LIMIT = 100

/** 多久没前进就提示「无进展」。后端有 60 秒停顿看门狗，这里只是提前给人一个交代。 */
const STALL_HINT_MS = 5000

/** 两次采样至少隔这么久才算得出速率，太近的两次会被进度事件的抖动放大。 */
const SAMPLE_INTERVAL_MS = 300

export const useTransfersStore = defineStore('transfers', () => {
  const items = ref<TransferItem[]>([])
  let seq = 0

  const runningCount = computed(() => items.value.filter((t) => t.status === 'running').length)
  /** 最近一次有任务发起的时间戳，页面用它把标签页切到「传输」。 */
  const lastStartedAt = ref(0)
  /** 后端告知的上传上限（字节），0 = 还没列过目录、不知道。 */
  const maxUploadBytes = ref(0)
  /** 只在有任务在跑时走动的钟，用来渲染「已 Ns 无进展」；没有任务就不占定时器。 */
  const now = ref(Date.now())
  let ticker: ReturnType<typeof setInterval> | null = null

  watch(runningCount, (count) => {
    if (count > 0 && !ticker) {
      now.value = Date.now()
      ticker = setInterval(() => {
        now.value = Date.now()
      }, 1000)
    } else if (!count && ticker) {
      clearInterval(ticker)
      ticker = null
    }
  })

  function noteUploadLimit(value: number) {
    // 只认后端给的正数：0 是没下发，不是「上限为 0」。
    if (value > 0) maxUploadBytes.value = value
  }

  function push(kind: TransferItem['kind'], serverId: number, name: string, remotePath: string, total: number) {
    const at = Date.now()
    const item: TransferItem = {
      id: ++seq,
      kind,
      serverId,
      name,
      remotePath,
      loaded: 0,
      total,
      status: 'running',
      error: '',
      startedAt: at,
      controller: new AbortController(),
      speed: 0,
      lastProgressAt: at,
      sample: { at, loaded: 0 },
    }
    items.value.unshift(item)
    if (items.value.length > KEEP_LIMIT) {
      // 只挤掉已结束的；正在跑的一条都不能动。
      for (let i = items.value.length - 1; i >= 0 && items.value.length > KEEP_LIMIT; i--) {
        if (items.value[i].status !== 'running') items.value.splice(i, 1)
      }
    }
    lastStartedAt.value = Date.now()
    // 必须返回数组里的代理项，而不是传进来的原始对象：unshift 会把对象包成
    // 响应式代理存进数组，之后写原始对象虽然读得到新值，却不触发任何依赖更新。
    // 进度回调抓住的一直是那个原始对象，于是进度条永远停在 0——「没有进度条」
    // 的一半原因是这里。
    return items.value[0]
  }

  /** 进度事件统一入口：记量、算速率、标记「这次动过」。 */
  function track(item: TransferItem, loaded: number, total?: number) {
    item.loaded = loaded
    if (total) item.total = total
    const at = Date.now()
    item.lastProgressAt = at
    // 顺手校一下公共时钟：浏览器把后台标签页的 setInterval 节流到一分钟一次，
    // 只靠钟走会量出负的停滞时长，「无进展」就永远不亮。标签页切走后还在传的
    // 大文件正是最需要看到这句话的场景。
    if (at > now.value) now.value = at
    const elapsed = at - item.sample.at
    if (elapsed >= SAMPLE_INTERVAL_MS && loaded > item.sample.loaded) {
      const instant = ((loaded - item.sample.loaded) / elapsed) * 1000
      // 指数平滑：浏览器给的事件粒度很粗，直接用瞬时值会一跳一跳的。
      item.speed = item.speed ? item.speed * 0.6 + instant * 0.4 : instant
      item.sample = { at, loaded }
    }
  }

  function fail(item: TransferItem, err: unknown, fallback: string) {
    // AbortError 来自 fetch 路径的 signal.abort()（DOMException），和 axios
    // 的取消一样是用户主动取消，不能误报成错误。
    if (axios.isCancel(err) || (err as Error)?.name === 'AbortError') {
      item.status = 'cancelled'
      return
    }
    item.status = 'error'
    item.error = (err as any)?.message || fallback
    message.error(`${item.kind === 'upload' ? '上传' : '下载'} ${item.name} 失败：${item.error}`)
  }

  /** 上传的「尾巴」：浏览器已经交完数据，还在等网关和后端写盘。 */
  function inServerTail(item: TransferItem): boolean {
    return (
      item.status === 'running' && item.kind === 'upload' && item.total > 0 && item.loaded >= item.total
    )
  }

  /** 距上次进度前进的秒数；0 = 还在动或任务已结束。 */
  function stallSeconds(item: TransferItem): number {
    if (item.status !== 'running') return 0
    const idle = now.value - item.lastProgressAt
    return idle >= STALL_HINT_MS ? Math.round(idle / 1000) : 0
  }

  /** 上传成功 resolve，失败/取消也 resolve（状态记在任务上），调用方靠 item.status 判断。 */
  async function startUpload(serverId: number, dir: string, file: File): Promise<TransferItem> {
    const remotePath = dir === '/' ? `/${file.name}` : `${dir}/${file.name}`
    const limit = maxUploadBytes.value
    if (limit && file.size > limit) {
      // 先在本地拦下：传完一整轮再收一个超限错误，是最浪费时间的那种失败。
      const item = push('upload', serverId, file.name, remotePath, file.size)
      item.status = 'error'
      item.error = `文件 ${fmtSize(file.size)} 超过上传上限 ${fmtSize(limit)}，未开始上传`
      return item
    }
    const item = push('upload', serverId, file.name, remotePath, file.size)
    try {
      await opsApi.sftpUpload(serverId, dir, file, {
        signal: item.controller.signal,
        onProgress: (loaded, total) => track(item, loaded, total),
      })
      item.loaded = item.total || file.size
      item.status = 'done'
    } catch (err) {
      fail(item, err, '上传失败')
    }
    return item
  }

  /** 用户取消保存框时不产生任务，返回 undefined。 */
  async function startDownload(
    serverId: number,
    remotePath: string,
    name: string,
  ): Promise<TransferItem | undefined> {
    if (supportsStreamingSave()) {
      let handle: FileSystemFileHandle | null = null
      let pickerFailed = false
      try {
        // 必须是本函数第一个 await：showSaveFilePicker 要求用户手势激活
        // （点击后约 5 秒有效），前面插任何网络 await 都会让它抛 SecurityError。
        handle = await pickSaveTarget(name)
      } catch {
        // SecurityError 等：环境不允许弹框，继续往下走 blob 路径。
        pickerFailed = true
      }
      if (handle) {
        const streaming = push('download', serverId, name, remotePath, 0)
        try {
          await streamToDisk(sftpDownloadUrl(serverId, remotePath), handle, {
            signal: streaming.controller.signal,
            onProgress: (loaded, total) => track(streaming, loaded, total),
          })
          streaming.loaded = streaming.total || streaming.loaded
          streaming.status = 'done'
        } catch (err) {
          fail(streaming, err, '下载失败')
        }
        return streaming
      }
      // handle 为 null 且 picker 没抛错：用户取消了保存框。在 push() 之前返回，
      // 不留幻影条目，也不触发「传输」标签页切换。
      if (!pickerFailed) return undefined
    }

    const item = push('download', serverId, name, remotePath, 0)
    try {
      const res = await opsApi.sftpDownload(serverId, remotePath, {
        signal: item.controller.signal,
        onProgress: (loaded, total) => track(item, loaded, total),
      })
      await saveBlobResponse(res, name)
      item.status = 'done'
    } catch (err) {
      fail(item, err, '下载失败')
    }
    return item
  }

  function cancel(id: number) {
    items.value.find((t) => t.id === id)?.controller.abort()
  }

  function clearFinished() {
    items.value = items.value.filter((t) => t.status === 'running')
  }

  return {
    items,
    runningCount,
    lastStartedAt,
    maxUploadBytes,
    now,
    noteUploadLimit,
    inServerTail,
    stallSeconds,
    startUpload,
    startDownload,
    cancel,
    clearFinished,
  }
})
