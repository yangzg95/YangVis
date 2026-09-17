/** 运维写命令确认的公共动作：approve（SSE：exec → token* → done|error）与
 * reject（普通 JSON）。CustomerService 与 OpsChat 两处确认卡片共用，避免
 * 各写一份状态流转。
 *
 * 状态归属约定：OpsAction 是页面持有的响应式对象，这里直接改它的字段
 * （status/result/exit_status），卡片跟着变；真正裁决始终在服务端 CAS，
 * 前端的一切预设都只是展示层。
 *
 * streamSse 的所有失败（连不上、流中断、服务端 error 事件）都汇进
 * onError 回调、返回的只是中断函数，所以 acting 的收尾只能挂在
 * onDone/onError 里，不能 await 返回值。
 */
import { reactive } from 'vue'
import { chatApi, type ConfirmEvent, type OpsAction, type StreamHandlers } from '@/api'

/** confirm 事件 → 本地 action 对象。服务端事件不带 created_at，提议就是
 *  此刻发生的，本地取 now 做倒计时起点，与 180s 窗口的误差可忽略。 */
export function actionFromConfirm(data: ConfirmEvent, conversationId: number | null): OpsAction {
  return {
    id: data.action_id,
    conversation_id: conversationId ?? 0,
    message_id: null,
    target_type: data.target_type,
    target_id: data.target_id,
    target_name: data.target_name,
    command: data.command,
    reason: data.reason,
    status: 'pending',
    result: null,
    exit_status: null,
    timeout_seconds: data.timeout_seconds,
    created_at: new Date().toISOString(),
    resolved_at: null,
  }
}

export function useOpsConfirm() {
  // 正在等服务端回话（点过按钮、还没出终态）的 action：用来挡重复点击。
  const actingIds = reactive(new Set<number>())

  const isActing = (action: OpsAction) => actingIds.has(action.id)

  /**
   * 同意执行。乐观把卡片置成 approved（按钮消失），exec 事件回来再落
   * 真实终态；续答的 token/step/citations/done 原样透传给调用方（主对话
   * 把它们接进一个新气泡，OpsChat 接进自己的流式渲染）。
   * 返回值是中断函数（同 streamSse），调用方要支持「停止」就接住它。
   */
  function approve(action: OpsAction, handlers: StreamHandlers = {}): () => void {
    if (actingIds.has(action.id) || action.status !== 'pending') return () => {}
    actingIds.add(action.id)
    action.status = 'approved'
    const settle = () => actingIds.delete(action.id)
    return chatApi.confirmAction(action.id, {
      ...handlers,
      onExec: (ev) => {
        action.status = ev.success ? 'executed' : 'failed'
        action.result = ev.output
        action.exit_status = ev.exit_status
        handlers.onExec?.(ev)
      },
      onDone: (data) => {
        settle()
        handlers.onDone?.(data)
      },
      onError: (err) => {
        settle()
        // 409/404 一定出现在 exec 之前（CAS 没抢到、已超时或不存在），
        // 说明命令没在这次请求里执行：把卡片钉在 expired 终态。其余错误
        // 码（流没建起来、续答中途断）恢复 pending，让用户能再点——若
        // 服务端其实已执行，再点会被 CAS 挡下走 expired 分支。
        if (action.status === 'approved') {
          action.status = err.code === 409 || err.code === 404 ? 'expired' : 'pending'
        }
        handlers.onError?.(err)
      },
    })
  }

  /** 拒绝执行。失败提示由 axios 拦截器统一弹，这里只把成功的新状态合回去。 */
  async function reject(action: OpsAction): Promise<void> {
    if (actingIds.has(action.id) || action.status !== 'pending') return
    actingIds.add(action.id)
    try {
      const updated = await chatApi.rejectAction(action.id)
      Object.assign(action, updated)
    } catch {
      // 409（已被处理/已超时）等情况拦截器已提示；卡片留原样，下次
      // 进会话拉 listActions 会拿到真实状态。
    } finally {
      actingIds.delete(action.id)
    }
  }

  return { isActing, approve, reject }
}
