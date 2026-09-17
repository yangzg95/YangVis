// 面板拖拽调大小：mousedown 记起点和初始尺寸，mousemove 把位移量交给调用方
// 换算成新尺寸，mouseup 收尾。拖拽期间给 body 挂 class 统一改光标、禁用文本
// 选择（光标样式的全局规则在 resizer.css 里）。

export type DragAxis = 'x' | 'y'

export function startDragResize(
  event: MouseEvent,
  axis: DragAxis,
  onMove: (delta: number) => void,
  onEnd?: () => void,
) {
  event.preventDefault()
  const start = axis === 'x' ? event.clientX : event.clientY
  const bodyClass = axis === 'x' ? 'dragging-col' : 'dragging-row'

  const move = (e: MouseEvent) => {
    onMove((axis === 'x' ? e.clientX : e.clientY) - start)
  }
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
    document.body.classList.remove(bodyClass)
    onEnd?.()
  }

  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
  document.body.classList.add(bodyClass)
}

export function clampSize(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

// 尺寸偏好存 localStorage：纯纯的查看者个人习惯，丢了也无妨，所以全程静默。
export function readSize(key: string, fallback: number): number {
  try {
    const value = Number(localStorage.getItem(key))
    return Number.isFinite(value) && value > 0 ? value : fallback
  } catch {
    return fallback
  }
}

export function saveSize(key: string, value: number) {
  try {
    localStorage.setItem(key, String(Math.round(value)))
  } catch {
    // 隐私模式 / 存储被禁用时，这次不记住而已。
  }
}
