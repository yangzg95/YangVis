<template>
  <div class="show">
    <!-- 放映内容就是后端渲染的那一份 HTML（与预览、导出逐字一致）。
         翻页/全屏的交互在文档内部跑，所以 iframe 开 allow-scripts；文本已在
         服务端转义、样式走白名单，脚本是渲染器自带的那一段固定代码。 -->
    <iframe
      v-if="htmlText"
      ref="frameRef"
      class="show-frame"
      :srcdoc="htmlText"
      sandbox="allow-scripts"
      allow="fullscreen"
      title="幻灯片放映"
      @load="onFrameLoad"
    />

    <div v-else class="show-empty">
      <a-spin v-if="!loadError" tip="正在准备放映内容…" size="large" />
      <a-result v-else status="error" title="放映内容载入失败" :sub-title="loadError">
        <template #extra>
          <a-button type="primary" @click="loadAll">重试</a-button>
        </template>
      </a-result>
    </div>

    <header class="show-bar" :class="{ quiet: barQuiet }">
      <div class="bar-left">
        <a-button size="small" type="text" class="bar-btn" @click="goWorkbench">
          <EditOutlined /> 工作台
        </a-button>
        <span class="bar-title">{{ deck?.title || '幻灯片' }}</span>
        <a-tag v-if="deck && deck.status !== 'ready'" :color="statusColor(deck.status)">
          {{ statusText(deck.status) }}
        </a-tag>
      </div>
      <div class="bar-right">
        <span class="bar-page">{{ pageIndex + 1 }} / {{ pageTotal }}</span>
        <a-button size="small" type="text" :disabled="pageIndex <= 0" @click="command('prev')">
          <LeftOutlined />
        </a-button>
        <a-button
          size="small"
          type="text"
          :disabled="pageIndex >= pageTotal - 1"
          @click="command('next')"
        >
          <RightOutlined />
        </a-button>
        <a-button size="small" type="text" @click="command('fullscreen')">
          <FullscreenOutlined /> 全屏
        </a-button>
        <a-button size="small" type="text" :disabled="!current" @click="openEditor">
          <FormOutlined /> 改这一页
        </a-button>
        <a-button size="small" type="text" @click="onExport">
          <DownloadOutlined /> 导出
        </a-button>
      </div>
    </header>

    <!-- 放映时就地改内容：改的仍是结构化页面，保存后重渲染再回到同一页。 -->
    <a-drawer
      v-model:open="editorOpen"
      title="修改这一页"
      placement="right"
      :width="420"
      :mask="false"
      :mask-closable="false"
    >
      <div v-if="current" class="editor-body">
        <div class="editor-page">第 {{ pageIndex + 1 }} 页 · {{ layoutText(current.layout) }}</div>
        <a-form layout="vertical">
          <a-form-item label="版式">
            <a-select v-model:value="form.layout">
              <a-select-option v-for="item in LAYOUT_OPTIONS" :key="item.value" :value="item.value">
                {{ item.label }}
              </a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item :label="form.layout === 'quote' ? '金句 / 结论' : '标题'">
            <a-textarea v-model:value="form.title" :rows="2" :maxlength="200" />
          </a-form-item>
          <a-form-item v-if="form.layout !== 'quote'" label="副标题">
            <a-textarea v-model:value="form.subtitle" :rows="2" :maxlength="400" />
          </a-form-item>
          <a-form-item v-if="showBullets" label="要点">
            <div v-for="(_, index) in form.bullets" :key="index" class="bullet-row">
              <a-textarea
                :value="form.bullets[index]"
                :rows="2"
                :maxlength="300"
                @update:value="(value: string) => (form.bullets[index] = value)"
              />
              <a-button size="small" type="text" danger @click="form.bullets.splice(index, 1)">
                <MinusOutlined />
              </a-button>
            </div>
            <a-button size="small" :disabled="form.bullets.length >= 12" @click="form.bullets.push('')">
              <PlusOutlined /> 加一条
            </a-button>
          </a-form-item>
          <a-form-item v-if="form.layout === 'image'" label="配图">
            <a-select v-model:value="form.asset_id" allow-clear placeholder="选择配图">
              <a-select-option v-for="asset in assets" :key="asset.id" :value="asset.id">
                {{ asset.name }}
              </a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item label="讲稿备注（放映时按 S 显示）">
            <a-textarea v-model:value="form.notes" :rows="4" :maxlength="4000" />
          </a-form-item>
        </a-form>
      </div>
      <template #footer>
        <a-space class="editor-footer">
          <span class="muted">保存后放映会停在当前页</span>
          <a-button @click="editorOpen = false">关闭</a-button>
          <a-button type="primary" :loading="saving" @click="onSavePage">保存并重绘</a-button>
        </a-space>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  DownloadOutlined,
  EditOutlined,
  FormOutlined,
  FullscreenOutlined,
  LeftOutlined,
  MinusOutlined,
  PlusOutlined,
  RightOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import {
  slidesApi,
  type SlideAsset,
  type SlideDeckDetail,
  type SlideLayout,
  type SlidePage,
  type SlidePageInput,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'

// 放映页（bare 整页，从列表或工作台新开标签页进来）。
//
// 页面本身只做两件事：把后端渲染好的 HTML 塞进 iframe，以及在 iframe 和父页面
// 之间传话。sandbox 里父页面读不到 iframe 的 location，所以「当前第几页」靠
// 文档内脚本 postMessage 上报，「跳到第 N 页」靠父页面下发指令 —— 两段都是
// 渲染器里写死的消息类型，不含内容。
//
// 键盘（←→↑↓ / 空格 / PageUp/Down / Home/End / 数字跳页 / F 全屏 / S 备注）、
// 点击左右边缘、滚轮、触摸滑动都由文档内脚本处理；工具条上的按钮是父页面拿到
// 焦点时的兜底，鼠标静止 2.5s 后工具条淡出，不打断放映。

const route = useRoute()
const router = useRouter()
const deckId = Number(route.params.id)

const deck = ref<SlideDeckDetail | null>(null)
const htmlText = ref('')
const loadError = ref('')
const frameRef = ref<HTMLIFrameElement | null>(null)

const pageIndex = ref(0)
const pageTotal = ref(1)
const saving = ref(false)
const editorOpen = ref(false)

// 重绘后回到改之前那一页：iframe 载体会从第 1 页重新开始，先记下目标页码。
let pendingJump: number | null = null

const LAYOUT_OPTIONS: { value: SlideLayout; label: string }[] = [
  { value: 'bullets', label: '要点页' },
  { value: 'cover', label: '封面' },
  { value: 'section', label: '章节页' },
  { value: 'image', label: '配图页' },
  { value: 'quote', label: '金句页' },
  { value: 'closing', label: '结尾页' },
]

const assets = computed<SlideAsset[]>(() => deck.value?.assets ?? [])
const current = computed<SlidePage | undefined>(() => deck.value?.slides[pageIndex.value])

const form = reactive<{
  layout: SlideLayout
  title: string
  subtitle: string
  bullets: string[]
  asset_id: string | null
  notes: string
}>({ layout: 'bullets', title: '', subtitle: '', bullets: [], asset_id: null, notes: '' })

const showBullets = computed(() =>
  ['bullets', 'image', 'quote', 'closing'].includes(form.layout),
)

function layoutText(layout?: string) {
  return LAYOUT_OPTIONS.find((item) => item.value === layout)?.label || '要点页'
}

function statusText(value: string) {
  return { analyzing: '生成中', error: '生成失败' }[value] || value
}

function statusColor(value: string) {
  return { analyzing: 'processing', error: 'error' }[value] || 'default'
}

function errorText(err: unknown, fallback: string): string {
  return (err as { message?: string } | undefined)?.message || fallback
}

// ---- 父子页面通信 ------------------------------------------------------------

function command(action: 'goto' | 'next' | 'prev' | 'fullscreen', index?: number) {
  frameRef.value?.contentWindow?.postMessage({ __slide: 'command', action, index }, '*')
}

function onPageMessage(event: MessageEvent) {
  const frame = frameRef.value
  if (!frame || event.source !== frame.contentWindow) return
  const data = event.data as { __slide?: string; index?: number; total?: number } | null
  if (!data || data.__slide !== 'page') return
  pageIndex.value = Math.max(0, Number(data.index) || 0)
  pageTotal.value = Math.max(1, Number(data.total) || 1)
}

function onFrameLoad() {
  if (pendingJump !== null) {
    command('goto', pendingJump)
    pendingJump = null
  }
  focusFrame()
}

function focusFrame() {
  // 键盘事件要先落到 iframe 里，放映才不用鼠标点一下才能翻页。
  frameRef.value?.focus()
}

// 工具条淡出：鼠标动起来才显形，父页面有焦点时补一份键盘兜底。
let hideTimer: number | undefined

function wake() {
  barQuiet.value = false
  if (hideTimer !== undefined) window.clearTimeout(hideTimer)
  hideTimer = window.setTimeout(() => {
    barQuiet.value = true
  }, 2500)
}

const barQuiet = ref(false)

function onWindowKeydown(event: KeyboardEvent) {
  if (editorOpen.value) return // 抽屉里的输入框自己处理按键
  const target = event.target as HTMLElement | null
  if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
  if (event.key === 'ArrowRight' || event.key === ' ' || event.key === 'PageDown') {
    event.preventDefault()
    command('next')
    wake()
  } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
    event.preventDefault()
    command('prev')
    wake()
  } else if (event.key === 'f' || event.key === 'F') {
    command('fullscreen')
    wake()
  } else {
    wake()
  }
}

// ---- 载入 --------------------------------------------------------------------

async function loadDeck() {
  try {
    deck.value = await slidesApi.detail(deckId)
    loadError.value = ''
  } catch (err) {
    loadError.value = errorText(err, '放映内容载入失败')
  }
}

async function loadHtml() {
  try {
    htmlText.value = await slidesApi.html(deckId)
  } catch (err) {
    loadError.value = errorText(err, '放映内容载入失败')
  }
}

async function loadAll() {
  await loadDeck()
  if (deck.value && deck.value.status !== 'analyzing') await loadHtml()
}

// 从列表页直接点进来时生成可能还没结束：等到 ready 再取 HTML。
let pollTimer: number | undefined

function watchGeneration() {
  if (pollTimer !== undefined || !deck.value || deck.value.status !== 'analyzing') return
  pollTimer = window.setInterval(async () => {
    await loadDeck()
    if (deck.value && deck.value.status !== 'analyzing') {
      window.clearInterval(pollTimer)
      pollTimer = undefined
      await loadHtml()
    }
  }, 2500)
}

// ---- 编辑当前页 --------------------------------------------------------------

function openEditor() {
  const page = current.value
  if (!page) return
  form.layout = page.layout
  form.title = page.title || ''
  form.subtitle = page.subtitle || ''
  form.bullets = [...(page.bullets || [])]
  form.asset_id = page.asset_id || null
  form.notes = page.notes || ''
  editorOpen.value = true
}

async function onSavePage() {
  const detail = deck.value
  if (!detail) return
  const index = pageIndex.value
  const slides: SlidePageInput[] = detail.slides.map((page) => ({ ...page }))
  const target = slides[index]
  if (!target) return
  const bullets = form.bullets.map((item) => item.trim()).filter(Boolean)
  const assetId = form.layout === 'image' ? form.asset_id : null
  Object.assign(target, {
    layout: form.layout === 'image' && !assetId ? 'bullets' : form.layout,
    title: form.title.trim(),
    subtitle: form.subtitle.trim(),
    bullets: form.layout === 'cover' || form.layout === 'section' ? [] : bullets,
    asset_id: assetId,
    notes: form.notes.trim(),
  })

  saving.value = true
  try {
    const saved = await slidesApi.save(deckId, { slides })
    deck.value = saved
    pendingJump = Math.min(index, Math.max(0, saved.slides.length - 1))
    await loadHtml()
    editorOpen.value = false
    message.success('已保存并重绘')
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    saving.value = false
  }
}

// ---- 导出 / 工作台 -----------------------------------------------------------

async function onExport() {
  try {
    const res = await slidesApi.exportHtml(deckId)
    await saveBlobResponse(res, `${deck.value?.title || 'slides'}.html`)
  } catch (err) {
    message.error(errorText(err, '导出失败'))
  }
}

function goWorkbench() {
  router.push({ name: 'SlideWorkbench', params: { id: deckId } })
}

onMounted(async () => {
  window.addEventListener('message', onPageMessage)
  window.addEventListener('keydown', onWindowKeydown)
  window.addEventListener('mousemove', wake)
  wake()
  await loadAll()
  watchGeneration()
})

onUnmounted(() => {
  window.removeEventListener('message', onPageMessage)
  window.removeEventListener('keydown', onWindowKeydown)
  window.removeEventListener('mousemove', wake)
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  if (hideTimer !== undefined) window.clearTimeout(hideTimer)
})
</script>

<style scoped>
.show {
  position: fixed;
  inset: 0;
  background: #05070a;
  overflow: hidden;
}

.show-frame {
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  background: #05070a;
}

.show-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255, 255, 255, 0.65);
}

.show-bar {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 12px;
  background: linear-gradient(180deg, rgba(5, 7, 10, 0.78), rgba(5, 7, 10, 0));
  color: rgba(255, 255, 255, 0.85);
  transition: opacity 0.3s ease;
  z-index: 2;
  flex-wrap: wrap;
}

.show-bar.quiet {
  opacity: 0;
  pointer-events: none;
}

.bar-left,
.bar-right {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.bar-title {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 40vw;
}

.bar-page {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: rgba(255, 255, 255, 0.7);
  margin-right: 2px;
}

.bar-btn {
  max-width: 260px;
}

.show-bar :deep(.ant-btn-text) {
  color: rgba(255, 255, 255, 0.85);
}

.show-bar :deep(.ant-btn-text:hover) {
  color: #fff;
  background: rgba(255, 255, 255, 0.12);
}

.editor-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.editor-page {
  font-size: 12px;
  color: var(--text-3);
}

.bullet-row {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  margin-bottom: 6px;
}

.editor-footer {
  display: flex;
  justify-content: space-between;
  width: 100%;
}

.muted {
  color: var(--text-3);
  font-size: 12px;
}
</style>
