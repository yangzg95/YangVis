/**
 * 正文 AI 动作的前端可用性规则。
 *
 * 后端 service 另有同一套闸门（kind 白名单、编辑动作要求非空、换语气要 tone），
 * 这里只负责把选项在弹窗里置灰并给出原因，让用户不必等一次被拒的请求。
 */
import {
  ACTION_LABELS,
  ARTICLE_ACTIONS,
  NOVEL_ACTIONS,
  WRITER_ACTIONS,
  type WritingKind,
  type WritingPartAction,
} from '@/api'

export interface PartActionChoice {
  action: WritingPartAction
  label: string
  disabled: boolean
  /** 置灰原因，喂给 tooltip。 */
  reason?: string
}

const EMPTY_BLOCK_HINT = '空的章节/段落只能先起草或手工写作，有正文后才能做编辑加工'

export function partActionsFor(kind: WritingKind, hasContent: boolean): PartActionChoice[] {
  const actions = kind === 'novel' ? NOVEL_ACTIONS : ARTICLE_ACTIONS
  return actions.map((action) => {
    const isEditorAction = !WRITER_ACTIONS.includes(action)
    const disabled = isEditorAction && !hasContent
    return {
      action,
      label: ACTION_LABELS[action],
      disabled,
      reason: disabled ? EMPTY_BLOCK_HINT : undefined,
    }
  })
}

/** 弹窗打开时的默认动作：优先起草；文章没有起草动作，回落第一个（重写，空块时置灰）。 */
export function defaultAction(kind: WritingKind): WritingPartAction {
  return kind === 'novel' ? 'draft' : ARTICLE_ACTIONS[0]
}
