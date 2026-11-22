<template>
  <div v-if="part" class="part-editor">
    <div class="title-row">
      <a-input
        v-model:value="title"
        class="title-input"
        :maxlength="128"
        :bordered="false"
        :disabled="locked"
        placeholder="章节/段落标题"
        @input="markDirty('title')"
      />
      <a-button type="primary" ghost size="small" class="ai-btn" :disabled="locked" @click="emit('generate')">
        <RobotOutlined /> AI 写作
      </a-button>
    </div>

    <a-collapse v-model:active-key="synopsisOpen" ghost class="synopsis-collapse">
      <a-collapse-panel key="synopsis">
        <template #header>
          <span class="synopsis-header">
            <EditOutlined />
            {{ work.kind === 'novel' ? '章节摘要（生成正文时的依据）' : '段落观点（生成正文时的依据）' }}
          </span>
        </template>
        <a-textarea
          v-model:value="synopsisDraft"
          :rows="3"
          :maxlength="2000"
          :bordered="false"
          :disabled="locked || structureLocked"
          placeholder="这一段要写什么、结论是什么"
          @input="scheduleSynopsisSave"
        />
        <div v-if="structureLocked && !locked" class="synopsis-locked-hint">
          有章节/段落正在生成，摘要保存会调整大纲结构，暂时锁定
        </div>
      </a-collapse-panel>
    </a-collapse>

    <textarea
      v-model="content"
      class="content-area mono"
      maxlength="30000"
      :disabled="locked"
      placeholder="开始写作，支持 Markdown；停笔 2.5 秒自动保存，Ctrl+S 立即保存"
      @input="markDirty('content')"
      @keydown="onKeydown"
    ></textarea>

    <div class="status-bar">
      <span :class="['save-status', `save-status--${saveState}`]">{{ statusText }}</span>
      <span v-if="part.status === 'analyzing'" class="part-status part-status--running">
        <LoadingOutlined /> AI 生成中…
      </span>
      <a-tooltip v-else-if="part.status === 'error'" :title="part.error_msg || '生成失败'">
        <span class="part-status part-status--error" @click="emit('showRuns')">
          <CloseCircleOutlined /> AI 生成失败，点击查看运行记录
        </span>
      </a-tooltip>
      <span v-if="locked" class="locked-hint">{{ lockedHint }}</span>
      <span class="word-count">本机统计 {{ localWords }} 字 · 已存 {{ part.word_count }} 字</span>
    </div>
  </div>

  <div v-else class="part-empty">
    <a-empty :description="emptyText">
      <a-button type="primary" @click="emit('addFirst')">
        <PlusOutlined /> {{ work.kind === 'novel' ? '添加第一章' : '添加第一段' }}
      </a-button>
    </a-empty>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import {
  CloseCircleOutlined,
  EditOutlined,
  LoadingOutlined,
  PlusOutlined,
  RobotOutlined,
} from '@ant-design/icons-vue'
import type { WritingPart, WritingWorkDetail } from '@/api'
import { countWords } from '@/utils/writingWords'
import { fmtShortDateTime } from '@/utils/format'

type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'

const props = defineProps<{
  work: WritingWorkDetail
  part: WritingPart | null
  synopsis: string
  saveState: SaveState
  savedAt: string
  /** 作品级运行或当前块生成期间锁定，避免自动保存与 AI 回写互相覆盖。 */
  locked?: boolean
  /** 任一正文块在跑：摘要保存走整树 PUT 会被结构闸门拒，只锁摘要框，标题/正文不受影响。 */
  structureLocked?: boolean
}>()

const emit = defineEmits<{
  save: [payload: { title?: string; content?: string }]
  saveSynopsis: [synopsis: string]
  addFirst: []
  dirty: []
  /** 打开 AI 动作弹窗。 */
  generate: []
  /** 块级运行失败时点开该块的运行记录。 */
  showRuns: []
}>()

const AUTOSAVE_MS = 2500

const title = ref('')
const content = ref('')
const synopsisDraft = ref('')
const synopsisOpen = ref<string[]>([])
const dirtyTitle = ref(false)
const dirtyContent = ref(false)
let saveTimer: ReturnType<typeof setTimeout> | undefined
let synopsisTimer: ReturnType<typeof setTimeout> | undefined

// 只在切换正文块时整体重载：保存返回的新对象内容不变，重载会盖掉正在输入的字。
watch(
  () => props.part?.cid,
  () => {
    clearTimers()
    title.value = props.part?.title ?? ''
    content.value = props.part?.content ?? ''
    synopsisDraft.value = props.synopsis ?? ''
    dirtyTitle.value = false
    dirtyContent.value = false
  },
  { immediate: true },
)

// 摘要随选中节点切换也要刷新（cid 变化已覆盖，这里兜同一 cid 被外部改名的场景）。
watch(
  () => props.synopsis,
  (value) => {
    if (document.activeElement?.tagName !== 'TEXTAREA') synopsisDraft.value = value ?? ''
  },
)

const localWords = computed(() => countWords(content.value))
const emptyText = computed(() =>
  props.work.kind === 'novel' ? '还没有章节，先加第一章开始写' : '还没有段落，先加第一段开始写',
)

// 块级 AI 生成期间输入框是禁用的，本机不可能有未落盘的字；运行一结束就把成稿装回本地，
// 不经过 cid 切换也能看到新内容。失败时同样同步一次（后端保留原正文，不会把本地稿冲没）。
watch(
  () => props.part?.status,
  (next, previous) => {
    if (previous !== 'analyzing' || !props.part || next === 'analyzing') return
    title.value = props.part.title
    content.value = props.part.content
    synopsisDraft.value = props.synopsis ?? ''
    dirtyTitle.value = false
    dirtyContent.value = false
  },
)

const lockedHint = computed(() =>
  props.part?.status === 'analyzing'
    ? 'AI 正文生成中，编辑已锁定，完成后自动恢复'
    : '作品正在生成中，编辑已锁定，完成后自动恢复',
)

const statusText = computed(() => {
  switch (props.saveState) {
    case 'saving':
      return '保存中…'
    case 'saved':
      return props.savedAt ? `已保存 ${fmtShortDateTime(props.savedAt)}` : '已保存'
    case 'error':
      return '保存失败，将在下次停笔时重试'
    case 'dirty':
      return '未保存'
    default:
      return '就绪'
  }
})

function markDirty(field: 'title' | 'content') {
  if (field === 'title') dirtyTitle.value = true
  else dirtyContent.value = true
  emit('dirty')
  scheduleSave()
}

function scheduleSave() {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(flush, AUTOSAVE_MS)
}

function scheduleSynopsisSave() {
  if (synopsisTimer) clearTimeout(synopsisTimer)
  synopsisTimer = setTimeout(() => {
    emit('saveSynopsis', synopsisDraft.value)
    synopsisTimer = undefined
  }, AUTOSAVE_MS)
}

function flush() {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = undefined
  }
  if (!props.part || (!dirtyTitle.value && !dirtyContent.value)) return
  const payload: { title?: string; content?: string } = {}
  if (dirtyTitle.value) payload.title = title.value.trim()
  if (dirtyContent.value) payload.content = content.value
  // 标题在输入过程中可能暂时是空白（全选删除的瞬间），空标题不发——后端会拒。
  if (payload.title !== undefined && !payload.title) {
    delete payload.title
    dirtyTitle.value = false
  }
  dirtyTitle.value = false
  dirtyContent.value = false
  emit('save', payload)
}

function onKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    flush()
  }
}

function clearTimers() {
  if (saveTimer) clearTimeout(saveTimer)
  if (synopsisTimer) clearTimeout(synopsisTimer)
  saveTimer = undefined
  synopsisTimer = undefined
}

// keep-alive 切走与组件销毁都兜底落盘，避免最后 2.5 秒的字停在本机。
onDeactivated(flush)
onBeforeUnmount(flush)

defineExpose({ flush })
</script>

<style scoped>
.part-editor {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
}

.title-row {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
}

.title-input {
  flex: 1;
  min-width: 0;
  font-size: 18px;
  font-weight: 600;
}

.title-input :deep(input) {
  font-weight: 600;
}

.ai-btn {
  flex: none;
}

.part-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.part-status--running {
  color: var(--signal-text);
}

.part-status--error {
  color: var(--error, #cf1322);
  cursor: pointer;
}

.synopsis-collapse {
  flex: none;
  margin: 4px 0;
}

.synopsis-header {
  color: var(--text-3);
  font-size: 13px;
}

.synopsis-collapse :deep(.ant-collapse-header) {
  padding: 4px 0 !important;
}

.synopsis-locked-hint {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 12px;
}

.content-area {
  flex: 1;
  min-height: 240px;
  width: 100%;
  padding: 12px 4px;
  border: none;
  outline: none;
  resize: none;
  background: transparent;
  color: var(--text-1);
  font-size: 14px;
  line-height: 1.9;
}

.status-bar {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--hairline);
  color: var(--text-3);
  font-size: 12px;
}

.save-status--saving {
  color: var(--signal-text);
}

.save-status--saved {
  color: var(--signal-text);
}

.save-status--error {
  color: var(--error, #cf1322);
}

.locked-hint {
  color: var(--signal-text);
}

.part-empty {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
}

@media (max-width: 768px) {
  /* 窄屏状态条四项会挤爆，允许换行并保持行距。 */
  .status-bar {
    flex-wrap: wrap;
    row-gap: 6px;
  }
}
</style>
