<template>
  <div class="workbench">
    <header class="wb-head">
      <div class="wb-head-main">
        <a-input
          v-model:value="title"
          class="wb-title-input"
          :maxlength="128"
          :bordered="false"
          @change="dirty = true"
        />
        <a-tag :color="statusColor(state)">{{ statusText(state) }}</a-tag>
        <span v-if="dirty" class="dirty-hint">未保存</span>
      </div>
      <a-space :size="8" wrap>
        <a-button size="small" @click="assetModal = true">
          <PictureOutlined /> 配图 {{ assets.length }}
        </a-button>
        <a-button size="small" :loading="generating" @click="onRegenerate">
          <ReloadOutlined /> 重新生成
        </a-button>
        <a-button size="small" @click="onExport">
          <DownloadOutlined /> 导出 HTML
        </a-button>
        <a-button size="small" type="primary" :loading="saving" @click="onSave">
          <SaveOutlined /> 保存
        </a-button>
        <a-button size="small" type="primary" :disabled="!pages.length" @click="openShow">
          <FundProjectionScreenOutlined /> 放映
        </a-button>
      </a-space>
    </header>

    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      class="wb-alert"
      :message="loadError"
    />

    <main class="wb-body">
      <!-- 页面列表：顺序即放映顺序 -->
      <aside class="wb-pages">
        <div class="pane-title">
          页面 {{ pages.length }}
          <a-button size="small" type="text" @click="addPage"><PlusOutlined /> 加一页</a-button>
        </div>
        <ul class="page-list">
          <li
            v-for="(page, index) in pages"
            :key="page.sid || `new-${index}`"
            :class="['page-item', { active: index === selected }]"
            @click="selected = index"
          >
            <span class="page-no">{{ index + 1 }}</span>
            <span class="page-text">
              <span class="page-name">{{ page.title || '（无标题）' }}</span>
              <span class="page-layout">{{ layoutText(page.layout) }}</span>
            </span>
            <span class="page-ops">
              <a-button
                size="small"
                type="text"
                :disabled="index === 0"
                title="上移"
                @click.stop="movePage(index, -1)"
              >
                <ArrowUpOutlined />
              </a-button>
              <a-button
                size="small"
                type="text"
                :disabled="index === pages.length - 1"
                title="下移"
                @click.stop="movePage(index, 1)"
              >
                <ArrowDownOutlined />
              </a-button>
              <a-popconfirm
                title="删除这一页？"
                ok-text="删除"
                cancel-text="取消"
                @confirm="removePage(index)"
              >
                <a-button size="small" type="text" danger title="删除" @click.stop>
                  <DeleteOutlined />
                </a-button>
              </a-popconfirm>
            </span>
          </li>
        </ul>
        <div class="pane-title">外观</div>
        <div class="theme-form">
          <a-radio-group v-model:value="theme.preset" size="small" @change="onThemeChange">
            <a-radio-button value="teal">墨青</a-radio-button>
            <a-radio-button value="ink">深墨</a-radio-button>
            <a-radio-button value="paper">暖纸</a-radio-button>
            <a-radio-button value="violet">紫电</a-radio-button>
          </a-radio-group>
          <a-select v-model:value="theme.ratio" size="small" class="ratio-select" @change="onThemeChange">
            <a-select-option value="16x9">16 : 9</a-select-option>
            <a-select-option value="4x3">4 : 3</a-select-option>
          </a-select>
          <label class="accent-row">
            <span>强调色</span>
            <a-input
              v-model:value="theme.accent"
              size="small"
              placeholder="#ff8800 或 ff8800"
              @change="onThemeChange"
            />
          </label>
          <span v-if="themeSaving" class="hint">正在应用外观…</span>
          <span v-else class="hint">外观改动即时生效并保存，不影响未保存的文字改动</span>
        </div>
      </aside>

      <!-- 表单为主，源码为辅 -->
      <section class="wb-editor">
        <a-segmented
          v-model:value="editorMode"
          size="small"
          :options="[
            { label: '表单', value: 'form' },
            { label: '源码', value: 'source' },
          ]"
          class="mode-switch"
        />

        <div v-if="editorMode === 'form' && current" class="page-form">
          <a-form layout="vertical">
            <a-form-item label="版式">
              <a-select v-model:value="current.layout" @change="onLayoutChange">
                <a-select-option v-for="item in LAYOUT_OPTIONS" :key="item.value" :value="item.value">
                  {{ item.label }}
                </a-select-option>
              </a-select>
              <div class="hint">{{ layoutHint(current.layout) }}</div>
            </a-form-item>
            <a-form-item :label="titleLabel">
              <a-textarea v-model:value="current.title" :rows="2" :maxlength="200" @change="markDirty" />
            </a-form-item>
            <a-form-item v-if="showSubtitle" :label="subtitleLabel">
              <a-textarea v-model:value="current.subtitle" :rows="2" :maxlength="400" @change="markDirty" />
            </a-form-item>
            <a-form-item v-if="showBullets" label="要点">
              <div v-for="(_, index) in current.bullets" :key="index" class="bullet-row">
                <a-textarea
                  :value="current.bullets[index]"
                  :rows="1"
                  :maxlength="300"
                  @update:value="(value: string) => (current!.bullets[index] = value)"
                  @change="markDirty"
                />
                <a-button size="small" type="text" danger @click="current!.bullets.splice(index, 1)">
                  <MinusOutlined />
                </a-button>
              </div>
              <a-button size="small" :disabled="(current.bullets?.length || 0) >= 12" @click="addBullet">
                <PlusOutlined /> 加一条
              </a-button>
            </a-form-item>
            <a-form-item v-if="current.layout === 'image'" label="配图">
              <a-select v-model:value="current.asset_id" allow-clear @change="markDirty">
                <a-select-option v-for="asset in assets" :key="asset.id" :value="asset.id">
                  {{ asset.name }}
                </a-select-option>
              </a-select>
              <div v-if="currentAsset" class="asset-preview">
                <img :src="currentAsset.url" :alt="currentAsset.name" />
                <span class="hint">点击可在浏览器里查看原图</span>
              </div>
            </a-form-item>
            <a-form-item label="讲稿备注（放映时按 S 显示）">
              <a-textarea v-model:value="current.notes" :rows="3" :maxlength="4000" @change="markDirty" />
            </a-form-item>
          </a-form>
        </div>

        <div v-else-if="editorMode === 'form'" class="empty-editor">选择左侧一页开始编辑</div>

        <div v-else class="source-editor">
          <div class="source-toolbar">
            <span class="hint">整组页面的 JSON；改完点「应用」回到表单</span>
            <a-space :size="6">
              <a-button size="small" @click="syncSource">格式化</a-button>
              <a-button size="small" type="primary" @click="applySource">应用</a-button>
            </a-space>
          </div>
          <a-textarea
            v-model:value="sourceText"
            class="json-area"
            :class="{ 'json-area-bad': sourceError }"
            :auto-size="{ minRows: 18 }"
            spellcheck="false"
          />
          <div v-if="sourceError" class="source-error">{{ sourceError }}</div>
        </div>
      </section>

      <!-- 预览：服务器渲染的那份 HTML，与放映、导出逐字一致 -->
      <section class="wb-preview">
        <div class="preview-head">
          <span>预览</span>
          <a-button size="small" type="text" :loading="previewLoading" @click="refreshPreview">
            <ReloadOutlined />
          </a-button>
        </div>
        <iframe
          v-if="previewHtml"
          class="preview-frame"
          :srcdoc="previewHtml"
          sandbox="allow-scripts"
          allow="fullscreen"
          title="幻灯片预览"
        />
        <div v-else class="preview-empty">
          <a-empty :image="simpleEmpty" :description="state === 'analyzing' ? '正在生成…' : '还没有内容'" />
        </div>
      </section>
    </main>

    <!-- 配图：文件在服务器本地，浏览器可直接浏览 -->
    <a-modal v-model:open="assetModal" title="配图素材" width="720px" :footer="null">
      <a-upload
        :before-upload="onUploadAsset"
        :show-upload-list="false"
        accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
        multiple
      >
        <a-button :loading="uploading"><UploadOutlined /> 上传图片</a-button>
      </a-upload>
      <p class="hint">
        支持 PNG / JPEG / GIF / WebP / BMP，单张 5 MB 以内，一份幻灯片最多 24 张。图片存在服务器
        本地，点缩略可在新窗口查看。
      </p>
      <a-spin :spinning="uploading">
        <ul class="asset-grid">
          <li v-for="asset in assets" :key="asset.id" class="asset-cell">
            <a :href="asset.url" target="_blank" rel="noopener" :title="`在浏览器中查看 ${asset.name}`">
              <img :src="asset.url" :alt="asset.name" loading="lazy" />
            </a>
            <div class="asset-meta">
              <span class="asset-name">{{ asset.name }}</span>
              <span class="hint">{{ fmtSize(asset.size) }}</span>
            </div>
            <a-popconfirm title="删除这张配图？引用它的页面会去掉图片。" ok-text="删除" cancel-text="取消" @confirm="onRemoveAsset(asset)">
              <a-button size="small" type="text" danger><DeleteOutlined /></a-button>
            </a-popconfirm>
          </li>
        </ul>
        <a-empty v-if="!assets.length" :image="simpleEmpty" description="还没有配图" />
      </a-spin>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FundProjectionScreenOutlined,
  MinusOutlined,
  PictureOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import {
  slidesApi,
  type SlideAsset,
  type SlideDeckDetail,
  type SlideLayout,
  type SlidePage,
  type SlidePageInput,
  type SlideTheme,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { fmtSize } from '@/utils/format'

/**
 * 编辑中的页面。与服务端结构差两点：新加的页还没有 `sid`（保存时由服务端发放），
 * 文本字段在本地一律归一成字符串/数组 —— 表单直接绑它们，省掉满屏的 `|| ''`。
 */
interface EditablePage {
  sid?: string
  layout: SlideLayout
  title: string
  subtitle: string
  bullets: string[]
  asset_id: string | null
  notes: string
}

// 幻灯片工作台（bare 整页，从列表新开标签页进来，与数据库问答页同一模式）。
//
// 编辑的是结构化页面（deck.slides 是唯一事实源），预览/放映/导出都是后端从
// 这份结构渲染出来的同一份 HTML —— 所以「保存」之后必须重新取一次预览，
// 屏幕上的东西永远等于落库后的渲染结果，不做前端本地假渲染。
//
// 表单是主入口；「源码」模式给需要批量调整的人，直接改整组 JSON，
// 应用后仍然走同一套服务端校验（非法版式/越界长度会被裁掉）。

const route = useRoute()
const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const deckId = Number(route.params.id)

const title = ref('')
const state = ref<'analyzing' | 'ready' | 'error'>('ready')
const pages = ref<EditablePage[]>([])
const assets = ref<SlideAsset[]>([])
const theme = reactive<SlideTheme>({ preset: 'teal', ratio: '16x9', accent: null })
const selected = ref(0)
const dirty = ref(false)
const saving = ref(false)
const themeSaving = ref(false)
const loadError = ref('')

const editorMode = ref<'form' | 'source'>('form')
const sourceText = ref('')
const sourceError = ref('')

const previewHtml = ref('')
const previewLoading = ref(false)

const assetModal = ref(false)
const uploading = ref(false)

const LAYOUT_OPTIONS: { value: SlideLayout; label: string; hint: string }[] = [
  { value: 'bullets', label: '要点页', hint: '标题写成一句话说完的结论，下面 3-5 条要点支撑它。' },
  { value: 'cover', label: '封面', hint: '整份的第一页。标题是主题，副标题是一句话定位或汇报人。' },
  { value: 'section', label: '章节页', hint: '大标题 + 一句本章要回答的问题。' },
  { value: 'image', label: '配图页', hint: '左文右图。图从「配图」里选，没选图时会自动降级成要点页。' },
  { value: 'quote', label: '金句页', hint: '一个关键数字或结论单独成页，副标题是出处。' },
  { value: 'closing', label: '结尾页', hint: '行动号召或总结论，可带几条要点回顾。' },
]

const current = computed(() => pages.value[selected.value])
const currentAsset = computed(() =>
  assets.value.find((item) => item.id === current.value?.asset_id),
)
const showBullets = computed(() =>
  ['bullets', 'image', 'quote', 'closing'].includes(current.value?.layout || ''),
)
const showSubtitle = computed(() => current.value?.layout !== 'quote')
const titleLabel = computed(() => (current.value?.layout === 'quote' ? '金句 / 结论' : '标题'))
const subtitleLabel = computed(() => (current.value?.layout === 'image' ? '小标题' : '副标题'))
const generating = computed(() => state.value === 'analyzing')

function layoutText(layout?: string) {
  return LAYOUT_OPTIONS.find((item) => item.value === layout)?.label || '要点页'
}

function layoutHint(layout?: string) {
  return LAYOUT_OPTIONS.find((item) => item.value === layout)?.hint || ''
}

function statusText(value: string) {
  return { analyzing: '生成中', ready: '已生成', error: '生成失败' }[value] || value
}

function statusColor(value: string) {
  return { analyzing: 'processing', ready: 'success', error: 'error' }[value] || 'default'
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

// ---- 载入与轮询 --------------------------------------------------------------

async function load(quiet = false) {
  try {
    const detail = await slidesApi.detail(deckId)
    applyDetail(detail)
    loadError.value = ''
  } catch (err) {
    if (!quiet) loadError.value = errorText(err, '载入失败')
  }
}

function applyDetail(detail: SlideDeckDetail) {
  title.value = detail.title
  state.value = detail.status
  pages.value = detail.slides.map(toEditable)
  assets.value = detail.assets
  Object.assign(theme, detail.theme)
  dirty.value = false
  if (selected.value >= detail.slides.length) selected.value = Math.max(0, detail.slides.length - 1)
}

function toEditable(page: SlidePage): EditablePage {
  return {
    sid: page.sid,
    layout: page.layout,
    title: page.title || '',
    subtitle: page.subtitle || '',
    bullets: [...(page.bullets || [])],
    asset_id: page.asset_id || null,
    notes: page.notes || '',
  }
}

// 生成是后台任务，从列表页点进来时可能还在跑：状态一跳完就重取一次。
let pollTimer: number | undefined

function syncPolling() {
  if (generating.value && pollTimer === undefined) {
    pollTimer = window.setInterval(async () => {
      await load(true)
      if (!generating.value) {
        window.clearInterval(pollTimer)
        pollTimer = undefined
        void refreshPreview()
      }
    }, 2500)
  }
}

async function refreshPreview() {
  previewLoading.value = true
  try {
    previewHtml.value = await slidesApi.html(deckId)
  } catch {
    // 预览取不到不影响编辑，保存后会再试一次。
  } finally {
    previewLoading.value = false
  }
}

// ---- 页面编辑 ----------------------------------------------------------------

function markDirty() {
  dirty.value = true
}

function addPage() {
  pages.value.push({ layout: 'bullets', title: '', subtitle: '', bullets: [], asset_id: null, notes: '' })
  selected.value = pages.value.length - 1
  dirty.value = true
}

function removePage(index: number) {
  pages.value.splice(index, 1)
  if (selected.value >= pages.value.length) selected.value = Math.max(0, pages.value.length - 1)
  dirty.value = true
}

function movePage(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= pages.value.length) return
  const list = pages.value
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
  selected.value = target
  dirty.value = true
}

function addBullet() {
  const page = current.value
  if (!page) return
  page.bullets = [...(page.bullets || []), '']
  markDirty()
}

function onLayoutChange() {
  const page = current.value
  if (!page) return
  page.bullets = page.bullets || []
  markDirty()
}

// ---- 源码模式 ----------------------------------------------------------------

function syncSource() {
  sourceText.value = JSON.stringify(pages.value, null, 2)
  sourceError.value = ''
}

function applySource() {
  let parsed: unknown
  try {
    parsed = JSON.parse(sourceText.value)
  } catch (err) {
    sourceError.value = `JSON 语法错误：${(err as Error).message}`
    return
  }
  if (!Array.isArray(parsed)) {
    sourceError.value = '顶层必须是一个页面数组'
    return
  }
  const bad = parsed.findIndex(
    (item) => !item || typeof item !== 'object' || Array.isArray(item),
  )
  if (bad >= 0) {
    sourceError.value = `第 ${bad + 1} 项不是一个对象`
    return
  }
  // 手工写的 JSON 什么都有可能：字段缺省、bullets 不是数组。在这里归一成
  // 表单要的形狀，剩下的交给服务端 sanitize（非法版式/超长会被裁）。
  pages.value = parsed.map((item) => {
    const raw = item as Partial<EditablePage>
    return {
      ...(raw.sid ? { sid: String(raw.sid) } : {}),
      layout: (raw.layout || 'bullets') as SlideLayout,
      title: raw.title ?? '',
      subtitle: raw.subtitle ?? '',
      bullets: Array.isArray(raw.bullets) ? raw.bullets.map((value) => String(value ?? '')) : [],
      asset_id: raw.asset_id || null,
      notes: raw.notes ?? '',
    }
  })
  selected.value = Math.min(selected.value, pages.value.length - 1)
  sourceError.value = ''
  dirty.value = true
  editorMode.value = 'form'
  message.success('已应用到表单，记得保存')
}

watch(editorMode, (mode) => {
  if (mode === 'source') syncSource()
})

// ---- 保存 / 生成 / 导出 / 放映 ------------------------------------------------

async function onSave() {
  saving.value = true
  try {
    const detail = await slidesApi.save(deckId, {
      title: title.value.trim() || undefined,
      theme: { ...theme },
      slides: pages.value as SlidePageInput[],
    })
    // 用服务端规范化的结果对齐：新页的 sid 是它发的，长度/版式也可能被裁过。
    applyDetail(detail)
    await refreshPreview()
    message.success('已保存')
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    saving.value = false
  }
}

let themePending = false

// 外观改动即时落库并刷新预览：只 PUT theme，不带着未保存的 slides 一起提交，
// 于是「改了颜色就看到效果」和「文字还没保存」两件事互不干扰。
async function onThemeChange() {
  if (themeSaving.value) {
    // 连点两次预设时不能把后一次丢掉：标记待重跑，循环里再提交一次。
    themePending = true
    return
  }
  themeSaving.value = true
  try {
    themePending = false
    // 每一轮都重新取当前值：上一轮 in-flight 期间用户又点了一格时，提交的是新选择。
    let detail = await slidesApi.save(deckId, { theme: { ...theme } })
    while (themePending) {
      themePending = false
      detail = await slidesApi.save(deckId, { theme: { ...theme } })
    }
    // 全部提交完才回写服务端收敛过的值（非法色号在这里被清空，输入框跟着回落）：
    // 提前回写会把用户刚点下的那一格盖掉。
    Object.assign(theme, detail.theme)
    await refreshPreview()
  } catch (err) {
    message.error(errorText(err, '外观保存失败'))
  } finally {
    themeSaving.value = false
  }
}

async function onRegenerate() {
  try {
    await slidesApi.regenerate(deckId)
    state.value = 'analyzing'
    message.success('已开始重新生成，当前内容会被覆盖')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '重新生成失败'))
  }
}

async function onExport() {
  try {
    const res = await slidesApi.exportHtml(deckId)
    await saveBlobResponse(res, `${title.value || 'slides'}.html`)
  } catch (err) {
    message.error(errorText(err, '导出失败'))
  }
}

function openShow() {
  const href = router.resolve({ name: 'SlideShow', params: { id: deckId } }).href
  window.open(href, '_blank', 'noopener')
}

// ---- 配图 --------------------------------------------------------------------

async function onUploadAsset(file: File) {
  uploading.value = true
  try {
    const detail = await slidesApi.uploadAsset(deckId, file)
    assets.value = detail.assets
    message.success(`已上传 ${file.name}`)
    await refreshPreview()
  } catch (err) {
    message.error(errorText(err, '上传失败'))
  } finally {
    uploading.value = false
  }
  return false
}

async function onRemoveAsset(asset: SlideAsset) {
  try {
    const detail = await slidesApi.removeAsset(deckId, asset.id)
    applyDetail(detail)
    dirty.value = true
    await refreshPreview()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 生命周期 ----------------------------------------------------------------

onMounted(async () => {
  await load()
  syncPolling()
  await refreshPreview()
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})

// 关标签页前提醒未保存的改动（新窗口打开，浏览器不会问第二次）。
function guard(event: BeforeUnloadEvent) {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', guard)
onUnmounted(() => window.removeEventListener('beforeunload', guard))
</script>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg, #f4f6f7);
}

.wb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  background: #fff;
  border-bottom: 1px solid var(--hairline, rgba(17, 24, 31, 0.09));
  flex-wrap: wrap;
}

.wb-head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.wb-title-input {
  font-size: 16px;
  font-weight: 600;
  max-width: 420px;
}

.dirty-hint {
  color: var(--text-3);
  font-size: 12px;
}

.wb-alert {
  margin: 12px 16px 0;
}

.wb-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 230px minmax(300px, 1fr) minmax(360px, 1.3fr);
  gap: 12px;
  padding: 12px 16px 16px;
}

.wb-pages,
.wb-editor,
.wb-preview {
  min-height: 0;
  background: #fff;
  border: 1px solid var(--hairline, rgba(17, 24, 31, 0.09));
  border-radius: var(--radius-md, 10px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 1px solid var(--hairline, rgba(17, 24, 31, 0.09));
}

.page-list {
  list-style: none;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  flex: 1;
}

.page-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
}

.page-item:hover {
  background: rgba(15, 181, 191, 0.06);
}

.page-item.active {
  background: rgba(15, 181, 191, 0.12);
}

.page-no {
  width: 20px;
  flex-shrink: 0;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}

.page-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.page-name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-layout {
  font-size: 11px;
  color: var(--text-3);
}

.page-ops {
  display: none;
  flex-shrink: 0;
}

.page-item:hover .page-ops,
.page-item.active .page-ops {
  display: inline-flex;
}

.theme-form {
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.accent-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-3);
}

.accent-row :deep(.ant-input) {
  font-family: var(--mono, monospace);
}

.ratio-select {
  width: 110px;
}

.mode-switch {
  align-self: flex-start;
  padding: 10px 12px 0;
}

.page-form {
  padding: 12px;
  overflow-y: auto;
  flex: 1;
}

.page-form :deep(.ant-form-item) {
  margin-bottom: 14px;
}

.bullet-row {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  margin-bottom: 6px;
}

.hint {
  color: var(--text-3);
  font-size: 12px;
}

.empty-editor {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
}

.asset-preview {
  margin-top: 8px;
}

.asset-preview img {
  max-width: 100%;
  max-height: 180px;
  border-radius: 8px;
  display: block;
}

.source-editor {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
}

.source-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.json-area {
  flex: 1;
  font-family: var(--mono, 'IBM Plex Mono', monospace);
  font-size: 12px;
  line-height: 1.6;
  resize: none;
  overflow: auto !important;
  height: 100%;
}

.json-area-bad {
  border-color: #d4380d;
}

.source-error {
  color: #d4380d;
  font-size: 12px;
}

.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 1px solid var(--hairline, rgba(17, 24, 31, 0.09));
}

.preview-frame {
  flex: 1;
  width: 100%;
  border: 0;
  background: #000;
}

.preview-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.asset-grid {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
}

.asset-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.asset-cell img {
  width: 100%;
  height: 92px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid var(--hairline, rgba(17, 24, 31, 0.09));
  display: block;
}

.asset-meta {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 12px;
}

.asset-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 1100px) {
  .wb-body {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .wb-pages,
  .wb-editor {
    max-height: 360px;
  }

  .preview-frame {
    min-height: 320px;
  }
}
</style>
