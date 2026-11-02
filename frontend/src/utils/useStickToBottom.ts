/** 聊天页共用的贴底滚动。
 *
 * 口径：用户上翻查看历史时，新 token 不抢视图；只有原本贴着底才跟着滚。
 * Chat.vue 与 OpsChat.vue 原先各复制一份，阈值/隐藏态判断容易漂移，
 * 这里收成一处。
 */
import { nextTick, ref, type Ref } from 'vue'

/** 距底部小于这个像素数就算「贴底」。 */
export const STICK_THRESHOLD = 40

export interface StickToBottom {
  /** 当前是否贴底；模板用它决定「回到底部」浮钮是否出现。 */
  stickToBottom: Ref<boolean>
  /** 绑定到滚动容器的 @scroll。 */
  onScroll: () => void
  /** 流式期间调用：贴底才跟随，隐藏（keep-alive 缓存）时不做布局。 */
  scrollToBottom: () => void
  /** 用户主动动作（发送 / 重试 / 切换会话）：强制贴底跟随。 */
  pinToBottom: () => void
}

export function useStickToBottom(
  scrollEl: Ref<HTMLElement | null>,
  pageActive: Ref<boolean>,
): StickToBottom {
  const stickToBottom = ref(true)
  /** 已经排了一次渲染后的贴底，同一拍内的后续调用不再重复排队。 */
  let scrollScheduled = false

  function onScroll() {
    const el = scrollEl.value
    if (el) {
      stickToBottom.value =
        el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD
    }
  }

  function scrollToBottom() {
    // keep-alive 缓存期间不在隐藏 DOM 上做强制布局。
    if (!pageActive.value || scrollScheduled) return
    scrollScheduled = true
    void nextTick(() => {
      scrollScheduled = false
      const el = scrollEl.value
      if (el && stickToBottom.value) el.scrollTop = el.scrollHeight
    })
  }

  function pinToBottom() {
    stickToBottom.value = true
    scrollToBottom()
  }

  return { stickToBottom, onScroll, scrollToBottom, pinToBottom }
}
