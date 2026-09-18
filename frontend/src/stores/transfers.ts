/** SFTP 上传/下载的传输任务管理。
 *
 * 传输是后台任务：从文件面板发起后不阻塞浏览，进度在「传输」标签页里看。
 * store 是全局单例，页面切走、面板关掉都不影响正在跑的请求。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import axios from 'axios'
import { message } from 'ant-design-vue'
import { opsApi, sftpDownloadUrl } from '@/api'
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
}

/** 列表最多保留的条数，早先的完成记录会被挤掉。 */
const KEEP_LIMIT = 100

export const useTransfersStore = defineStore('transfers', () => {
  const items = ref<TransferItem[]>([])
  let seq = 0

  const runningCount = computed(() => items.value.filter((t) => t.status === 'running').length)
  /** 最近一次有任务发起的时间戳，页面用它把标签页切到「传输」。 */
  const lastStartedAt = ref(0)

  function push(kind: TransferItem['kind'], serverId: number, name: string, remotePath: string, total: number) {
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
      startedAt: Date.now(),
      controller: new AbortController(),
    }
    items.value.unshift(item)
    if (items.value.length > KEEP_LIMIT) {
      // 只挤掉已结束的；正在跑的一条都不能动。
      for (let i = items.value.length - 1; i >= 0 && items.value.length > KEEP_LIMIT; i--) {
        if (items.value[i].status !== 'running') items.value.splice(i, 1)
      }
    }
    lastStartedAt.value = Date.now()
    return item
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

  /** 上传成功 resolve，失败/取消也 resolve（状态记在任务上），调用方靠 item.status 判断。 */
  async function startUpload(serverId: number, dir: string, file: File): Promise<TransferItem> {
    const remotePath = dir === '/' ? `/${file.name}` : `${dir}/${file.name}`
    const item = push('upload', serverId, file.name, remotePath, file.size)
    try {
      await opsApi.sftpUpload(serverId, dir, file, {
        signal: item.controller.signal,
        onProgress: (loaded, total) => {
          item.loaded = loaded
          if (total) item.total = total
        },
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
            onProgress: (loaded, total) => {
              streaming.loaded = loaded
              if (total) streaming.total = total
            },
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
        onProgress: (loaded, total) => {
          item.loaded = loaded
          if (total) item.total = total
        },
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

  return { items, runningCount, lastStartedAt, startUpload, startDownload, cancel, clearFinished }
})
