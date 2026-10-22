<template>
  <div class="rte" :class="{ 'rte--disabled': disabled }">
    <div v-if="!hideToolbar" class="rte-bar" @mousedown.prevent>
      <button type="button" title="撤销" @click="exec('undo')"><UndoOutlined /></button>
      <button type="button" title="重做" @click="exec('redo')"><RedoOutlined /></button>
      <span class="rte-sep" />
      <button
        type="button"
        title="加粗"
        :class="{ active: active.bold }"
        @click="exec('bold')"
      >
        <BoldOutlined />
      </button>
      <button
        type="button"
        title="斜体"
        :class="{ active: active.italic }"
        @click="exec('italic')"
      >
        <ItalicOutlined />
      </button>
      <button
        type="button"
        title="下划线"
        :class="{ active: active.underline }"
        @click="exec('underline')"
      >
        <UnderlineOutlined />
      </button>
      <span class="rte-sep" />
      <button type="button" title="左对齐" :class="{ active: active.justifyLeft }" @click="exec('justifyLeft')">
        <AlignLeftOutlined />
      </button>
      <button type="button" title="居中" :class="{ active: active.justifyCenter }" @click="exec('justifyCenter')">
        <AlignCenterOutlined />
      </button>
      <button type="button" title="右对齐" :class="{ active: active.justifyRight }" @click="exec('justifyRight')">
        <AlignRightOutlined />
      </button>
      <span class="rte-sep" />
      <button type="button" title="无序列表" :class="{ active: active.insertUnorderedList }" @click="exec('insertUnorderedList')">
        <UnorderedListOutlined />
      </button>
      <button type="button" title="有序列表" :class="{ active: active.insertOrderedList }" @click="exec('insertOrderedList')">
        <OrderedListOutlined />
      </button>
      <button type="button" title="减少缩进" @click="exec('outdent')"><VerticalAlignBottomOutlined /></button>
      <button type="button" title="增加缩进" @click="exec('indent')"><VerticalAlignTopOutlined /></button>
      <span class="rte-sep" />
      <button type="button" title="插入链接" @click="openLink"><LinkOutlined /></button>
      <button type="button" title="清除格式" @click="exec('removeFormat')"><ClearOutlined /></button>
      <span class="rte-ai-slot"><slot name="ai" /></span>
    </div>
    <div
      ref="editorEl"
      class="rte-content"
      :contenteditable="disabled ? 'false' : 'true'"
      :data-placeholder="placeholder"
      :style="minHeight ? { minHeight: `${minHeight}px` } : undefined"
      @input="onInput"
      @compositionend="onCompositionEnd"
      @paste="onPaste"
      @blur="emit('blur')"
      @keyup="onCaretMove"
      @mouseup="onCaretMove"
    />
    <a-modal
      v-model:open="linkOpen"
      title="插入链接"
      :width="420"
      ok-text="插入"
      cancel-text="取消"
      @ok="confirmLink"
    >
      <a-input
        v-model:value="linkUrl"
        placeholder="https://example.com"
        @pressEnter="confirmLink"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
/**
 * 轻量富文本：contenteditable + execCommand，零新依赖。
 *
 * 光标不跳的关键：输入产生的 innerHTML 原样 emit，父组件回写同值时 watcher
 * 识别「内容相同」就不重设 DOM；只有外部整体替换（AI 应用结果）才会重设。
 * 粘贴内容先过白名单消毒；服务端保存时还会再消毒一次（防御纵深）。
 */
import { onMounted, reactive, ref, watch } from 'vue'
import {
  AlignCenterOutlined,
  AlignLeftOutlined,
  AlignRightOutlined,
  BoldOutlined,
  ClearOutlined,
  ItalicOutlined,
  LinkOutlined,
  OrderedListOutlined,
  RedoOutlined,
  UnderlineOutlined,
  UndoOutlined,
  UnorderedListOutlined,
  VerticalAlignBottomOutlined,
  VerticalAlignTopOutlined,
} from '@ant-design/icons-vue'
import { sanitizeRichHtml } from '@/utils/resumeTemplate'

const props = withDefaults(
  defineProps<{
    html?: string
    placeholder?: string
    disabled?: boolean
    hideToolbar?: boolean
    minHeight?: number
  }>(),
  { html: '', placeholder: '', disabled: false, hideToolbar: false, minHeight: 120 },
)

const emit = defineEmits<{
  'update:html': [value: string]
  blur: []
}>()

const editorEl = ref<HTMLDivElement | null>(null)
const linkOpen = ref(false)
const linkUrl = ref('')
const active = reactive<Record<string, boolean>>({})
let savedRange: Range | null = null

function saveRange() {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorEl.value) return
  const node = sel.anchorNode
  if (node && editorEl.value.contains(node)) {
    savedRange = sel.getRangeAt(0).cloneRange()
  }
}

function restoreRange() {
  const el = editorEl.value
  if (!el) return
  el.focus()
  if (!savedRange) return
  const sel = window.getSelection()
  if (!sel) return
  try {
    sel.removeAllRanges()
    sel.addRange(savedRange)
  } catch {
    // 缓存的 Range 已经随 innerHTML 重设失效：退回光标到末尾。
    placeCaretAtEnd()
  }
}

function placeCaretAtEnd() {
  const el = editorEl.value
  if (!el) return
  el.focus()
  const range = document.createRange()
  range.selectNodeContents(el)
  range.collapse(false)
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
  savedRange = range.cloneRange()
}

onMounted(() => {
  if (editorEl.value) editorEl.value.innerHTML = sanitizeRichHtml(props.html)
})

// 父组件持有同一份 html：我们输入时 emit 原样 innerHTML，回传相等就不重设，
// 避免编辑中途 innerHTML 被整体替换导致光标跳飞。
watch(
  () => props.html,
  (value) => {
    const el = editorEl.value
    if (!el) return
    if (el.innerHTML === value) return
    el.innerHTML = sanitizeRichHtml(value)
  },
)

function onInput(event?: Event) {
  saveRange()
  // 拼音组词过程中的 input（每个候选字都触发）只更新本地选区，不向上抛——
  // 否则每个按键都会引起父级重渲染与预览防抖刷新。组词结束的最终提交靠
  // compositionend（各浏览器最后一个 input 的 isComposing 取值并不一致）。
  if (event && (event as InputEvent).isComposing) {
    syncActive()
    return
  }
  emit('update:html', editorEl.value?.innerHTML ?? '')
  syncActive()
}

function onCompositionEnd() {
  saveRange()
  emit('update:html', editorEl.value?.innerHTML ?? '')
  syncActive()
}

function onCaretMove() {
  saveRange()
  syncActive()
}

function syncActive() {
  for (const cmd of [
    'bold',
    'italic',
    'underline',
    'justifyLeft',
    'justifyCenter',
    'justifyRight',
    'insertUnorderedList',
    'insertOrderedList',
  ]) {
    try {
      active[cmd] = document.queryCommandState(cmd)
    } catch {
      active[cmd] = false
    }
  }
}

function exec(cmd: string, value?: string) {
  if (props.disabled) return
  restoreRange()
  document.execCommand(cmd, false, value)
  onInput()
}

function escapePlain(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\r?\n/g, '<br>')
}

function onPaste(event: ClipboardEvent) {
  event.preventDefault()
  const clipboard = event.clipboardData
  if (!clipboard) return
  const html = clipboard.getData('text/html')
  const text = clipboard.getData('text/plain')
  const clean = html ? sanitizeRichHtml(html) : escapePlain(text)
  if (!clean) return
  restoreRange()
  document.execCommand('insertHTML', false, clean)
  onInput()
}

function openLink() {
  saveRange()
  linkUrl.value = ''
  linkOpen.value = true
}

function confirmLink() {
  const url = linkUrl.value.trim()
  restoreRange()
  if (url) {
    document.execCommand('createLink', false, url)
  } else {
    document.execCommand('unlink')
  }
  linkOpen.value = false
  onInput()
}

/** 当前在编辑器内选中的纯文本（润色用）。 */
function getSelectionText(): string {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorEl.value) return ''
  const node = sel.anchorNode
  if (!node || !editorEl.value.contains(node)) return ''
  return sel.toString()
}

/** AI 结果插到光标处（未聚焦过时插到末尾）。 */
function insertHtml(html: string) {
  if (props.disabled) return
  restoreRange()
  document.execCommand('insertHTML', false, sanitizeRichHtml(html))
  onInput()
}

function focusEditor() {
  restoreRange()
}

defineExpose({ insertHtml, getSelectionText, focusEditor, placeCaretAtEnd })
</script>

<style scoped>
.rte {
  border: 1px solid #d9d9d9;
  border-radius: 6px;
  overflow: hidden;
  background: #fff;
}
.rte--disabled {
  background: #f5f5f5;
}
.rte-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
  padding: 4px 8px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafafa;
}
.rte-bar button {
  width: 26px;
  height: 26px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: #4b5563;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.rte-bar button:hover {
  background: #e6f4f5;
  color: #0a7e86;
}
.rte-bar button.active {
  background: #d6ecee;
  color: #0a7e86;
}
.rte-sep {
  width: 1px;
  height: 16px;
  margin: 0 4px;
  background: #e0e0e0;
}
.rte-ai-slot {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
}
.rte-content {
  padding: 8px 12px;
  font-size: 13.5px;
  line-height: 1.7;
  outline: none;
  word-break: break-word;
}
/* 帮写回来的内容已过消毒器、对齐变成 class 与一层 blockquote，
   编辑器里要和导出看到同一个效果，否则「居中」在编辑时不生效。 */
.rte-content :deep(.rb-ta-center) {
  text-align: center;
}
.rte-content :deep(.rb-ta-right) {
  text-align: right;
}
.rte-content :deep(.rb-ta-justify) {
  text-align: justify;
}
/* 块间距与打印版面同一档（见 resumeTemplate 的 BASE_CSS），编辑时看到的分隔就是导出后的
   分隔；字号这里比打印大 1px 是为了好读，只有间距需要对齐。 */
.rte-content :deep(blockquote) {
  margin: 6px 0 7px;
  padding-left: 20px;
}
.rte-content:empty::before {
  content: attr(data-placeholder);
  color: #bfbfbf;
  pointer-events: none;
}
.rte-content p {
  margin: 0 0 7px;
}
.rte-content ul,
.rte-content ol {
  margin: 6px 0 7px;
  padding-left: 22px;
}
.rte-content li {
  margin-bottom: 4px;
}
.rte-content a {
  color: #0a7e86;
}
</style>
