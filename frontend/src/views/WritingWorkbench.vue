<template>
  <div class="page-card studio-workbench">
    <header class="wb-topbar">
      <div class="wb-title-row">
        <a-button type="text" class="back-btn" @click="goList">
          <ArrowLeftOutlined />
        </a-button>
        <a-typography-text
          v-if="detail"
          class="wb-title"
          :editable="{ onChange: onTitleChange, tooltip: '点击重命名' }"
        >{{ detail.title }}</a-typography-text>
        <a-tag v-if="detail" color="blue">{{ detail.kind === 'novel' ? '小说' : '文章' }}</a-tag>
        <span v-if="detail" class="wb-total muted">{{ detail.word_count }} 字 · {{ detail.part_count }} 个{{ detail.kind === 'novel' ? '章节' : '段落' }}</span>
      </div>
      <div class="wb-actions">
        <a-button @click="infoOpen = true"><InfoCircleOutlined /> 作品信息</a-button>
        <a-dropdown>
          <a-button type="primary">导出 <DownOutlined /></a-button>
          <template #overlay>
            <a-menu @click="onMenuExport">
              <a-menu-item key="md">Markdown (.md)</a-menu-item>
              <a-menu-item key="txt">纯文本 (.txt)</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </header>

    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      class="wb-alert"
      message="作品加载失败"
      :description="loadError"
    >
      <template #action><a-button size="small" danger @click="load(true)">重试</a-button></template>
    </a-alert>

    <a-spin :spinning="loading">
      <div v-if="detail" class="wb-body">
        <!-- 窄屏大纲收成抽屉 -->
        <a-drawer
          v-if="isMobile"
          v-model:open="outlineDrawerOpen"
          :title="detail.kind === 'novel' ? '大纲' : '段落'"
          placement="left"
          width="300"
          :body-style="{ display: 'flex', flexDirection: 'column', minHeight: '0' }"
        >
          <OutlineTree
            :work="detail"
            :selected-cid="selectedCid"
            @select="onSelectCid"
            @add="onAddPart"
            @remove="onRemovePart"
            @remove-volume="onRemoveVolume"
            @move="onMoveLeaf"
            @add-volume="onAddVolume"
          />
        </a-drawer>

        <aside v-else class="wb-outline">
          <div class="pane-title">{{ detail.kind === 'novel' ? '大纲' : '段落结构' }}</div>
          <OutlineTree
            :work="detail"
            :selected-cid="selectedCid"
            @select="onSelectCid"
            @add="onAddPart"
            @remove="onRemovePart"
            @remove-volume="onRemoveVolume"
            @move="onMoveLeaf"
            @add-volume="onAddVolume"
          />
        </aside>

        <main class="wb-main">
          <div v-if="isMobile" class="mobile-switch">
            <a-button size="small" @click="outlineDrawerOpen = true">
              <MenuOutlined /> 大纲
            </a-button>
            <a-segmented v-model:value="mobileMode" :options="['编辑', '预览']" />
          </div>

          <div v-show="!isMobile || mobileMode === '编辑'" class="wb-editor pane">
            <PartEditor
              ref="editorRef"
              :work="detail"
              :part="currentPart"
              :synopsis="currentSynopsis"
              :save-state="saveState"
              :saved-at="savedAt"
              @save="onSavePart"
              @save-synopsis="onSaveSynopsis"
              @add-first="onAddPart(null)"
              @dirty="saveState = 'dirty'"
            />
          </div>

          <div v-show="!isMobile || mobileMode === '预览'" class="wb-preview pane">
            <div class="pane-title">预览</div>
            <!-- createMarkdown 固定 html:false，模型/用户写的原始 HTML 只会转义成文本 -->
            <div class="markdown-body" v-html="renderedMarkdown" />
          </div>
        </main>
      </div>
    </a-spin>

    <!-- 作品信息：brief / requirement 可改，params 只读展示 -->
    <a-modal v-model:open="infoOpen" title="作品信息" ok-text="保存" cancel-text="取消" @ok="onSaveInfo">
      <a-form layout="vertical" v-if="detail">
        <a-form-item :label="detail.kind === 'novel' ? '灵感 / 梗概' : '主题 / 素材'">
          <a-textarea v-model:value="infoDraft.brief" :rows="4" :maxlength="20000" show-count />
        </a-form-item>
        <a-form-item label="补充要求">
          <a-textarea v-model:value="infoDraft.requirement" :rows="2" :maxlength="512" />
        </a-form-item>
        <a-form-item v-if="paramEntries.length" label="创作参数（在新建时设定）">
          <a-descriptions size="small" :column="1" bordered>
            <a-descriptions-item v-for="[key, value] in paramEntries" :key="key">
              {{ paramLabel(key) }}：{{ value }}
            </a-descriptions-item>
          </a-descriptions>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  DownOutlined,
  InfoCircleOutlined,
  MenuOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import {
  ARTICLE_PARAM_FIELDS,
  NOVEL_PARAM_FIELDS,
  writingApi,
  type OutlineNode,
  type WritingWorkDetail,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { errorText } from '@/utils/errors'
import { createMarkdown } from '@/utils/markdown'
import { useIsMobile } from '@/utils/useIsMobile'
import {
  appendVolume,
  cloneOutline,
  findLeaf,
  flattenLeaves,
  isCid,
  moveLeaf,
} from '@/utils/writingOutline'
import OutlineTree from '@/components/writing/OutlineTree.vue'
import PartEditor from '@/components/writing/PartEditor.vue'

type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'

const route = useRoute()
const router = useRouter()
const isMobile = useIsMobile()
const markdown = createMarkdown({ breaks: true })

const workId = ref(Number(route.params.id) || 0)
const detail = ref<WritingWorkDetail | null>(null)
const loading = ref(false)
const loadError = ref('')
const selectedCid = ref<string | null>(null)
const saveState = ref<SaveState>('idle')
const savedAt = ref('')
const outlineDrawerOpen = ref(false)
const mobileMode = ref('编辑')
const infoOpen = ref(false)
const infoDraft = ref({ brief: '', requirement: '' })
const editorRef = ref<InstanceType<typeof PartEditor> | null>(null)

// 并发响应只接受最新一次：旧快照晚回来不能盖掉新结果。
let requestSeq = 0
let applySeq = 0

const currentPart = computed(() =>
  selectedCid.value ? detail.value?.parts.find((p) => p.cid === selectedCid.value) ?? null : null,
)
const currentSynopsis = computed(() => {
  if (!detail.value || !selectedCid.value) return ''
  return findLeaf(detail.value.outline, selectedCid.value)?.synopsis ?? ''
})
const renderedMarkdown = computed(() => markdown.render(currentPart.value?.content || ''))
const paramEntries = computed(() => Object.entries(detail.value?.params ?? {}))

function paramLabel(key: string): string {
  const fields = detail.value?.kind === 'novel' ? NOVEL_PARAM_FIELDS : ARTICLE_PARAM_FIELDS
  return fields.find((f) => f.key === key)?.label ?? key
}

async function load(showLoading = true) {
  if (!workId.value) return
  if (showLoading) loading.value = true
  loadError.value = ''
  const seq = ++requestSeq
  try {
    const next = await writingApi.detail(workId.value)
    if (seq < applySeq) return
    applySeq = seq
    adopt(next)
  } catch (err) {
    if (seq === requestSeq) loadError.value = errorText(err, '请检查网络后重试')
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function adopt(next: WritingWorkDetail) {
  detail.value = next
  const leaves = flattenLeaves(next.outline)
  if (!selectedCid.value || !leaves.some((n) => n.cid === selectedCid.value)) {
    selectedCid.value = leaves[0]?.cid ?? null
  }
}

/** 所有会返回完整详情的变更都走它，靠 seq 挡住乱序响应。 */
async function mutate(fn: () => Promise<WritingWorkDetail>): Promise<WritingWorkDetail> {
  const seq = ++requestSeq
  const next = await fn()
  if (seq >= applySeq) {
    applySeq = seq
    adopt(next)
  }
  return next
}

// ---- 大纲与正文块 --------------------------------------------------------------

function onSelectCid(cid: string) {
  selectedCid.value = cid
  outlineDrawerOpen.value = false
}

async function onAddPart(afterCid: string | null) {
  if (!detail.value) return
  const oldCids = new Set(flattenLeaves(detail.value.outline).map((n) => n.cid))
  try {
    const next = await mutate(() => writingApi.addPart(workId.value, '', afterCid))
    const created = flattenLeaves(next.outline).find((n) => isCid(n.cid) && !oldCids.has(n.cid))
    if (created) selectedCid.value = created.cid
  } catch (err) {
    message.error(errorText(err, '添加失败'))
  }
}

async function onRemovePart(cid: string) {
  try {
    await mutate(() => writingApi.deletePart(workId.value, cid))
    if (selectedCid.value === cid) selectedCid.value = null
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

async function onMoveLeaf(cid: string, dir: -1 | 1) {
  if (!detail.value) return
  const tree = cloneOutline(detail.value.outline)
  if (!moveLeaf(tree, cid, dir)) return
  try {
    await mutate(() => writingApi.saveOutline(workId.value, tree))
  } catch (err) {
    message.error(errorText(err, '排序失败'))
    void load(false)
  }
}

async function onAddVolume() {
  if (!detail.value) return
  const tree = cloneOutline(detail.value.outline)
  appendVolume(tree)
  try {
    await mutate(() => writingApi.saveOutline(workId.value, tree))
  } catch (err) {
    message.error(errorText(err, '加卷失败'))
  }
}

async function onRemoveVolume(node: OutlineNode) {
  if (!detail.value || (node.children?.length ?? 0) > 0) return
  const tree = cloneOutline(detail.value.outline).filter((n) => n.cid !== node.cid)
  try {
    await mutate(() => writingApi.saveOutline(workId.value, tree))
  } catch (err) {
    message.error(errorText(err, '删卷失败'))
  }
}

async function onSavePart(payload: { title?: string; content?: string }) {
  const cid = selectedCid.value
  if (!cid || Object.keys(payload).length === 0) return
  saveState.value = 'saving'
  try {
    await mutate(() => writingApi.savePart(workId.value, cid, payload))
    saveState.value = 'saved'
    savedAt.value = new Date().toISOString()
  } catch {
    // 全局拦截器已弹错误提示；状态切失败，下次停笔会再次触发保存。
    saveState.value = 'error'
  }
}

async function onSaveSynopsis(synopsis: string) {
  if (!detail.value || !selectedCid.value) return
  const tree = cloneOutline(detail.value.outline)
  const leaf = findLeaf(tree, selectedCid.value)
  if (!leaf || leaf.synopsis === synopsis) return
  leaf.synopsis = synopsis
  try {
    await mutate(() => writingApi.saveOutline(workId.value, tree))
  } catch (err) {
    message.error(errorText(err, '摘要保存失败'))
    void load(false)
  }
}

// ---- 作品级 -------------------------------------------------------------------

async function onTitleChange(title: string) {
  const clean = title.trim()
  if (!detail.value || !clean || clean === detail.value.title) return
  try {
    await mutate(() => writingApi.update(workId.value, { title: clean }))
  } catch (err) {
    message.error(errorText(err, '重命名失败'))
    void load(false)
  }
}

function onOpenInfo() {
  if (!detail.value) return
  infoDraft.value = {
    brief: detail.value.brief ?? '',
    requirement: detail.value.requirement ?? '',
  }
}

async function onSaveInfo() {
  try {
    await mutate(() =>
      writingApi.update(workId.value, {
        brief: infoDraft.value.brief,
        requirement: infoDraft.value.requirement,
      }),
    )
    message.success('已保存')
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  }
}

function onMenuExport(info: { key: string }) {
  void onExport(info.key as 'md' | 'txt')
}

async function onExport(format: 'md' | 'txt') {
  try {
    const res = await writingApi.exportWork(workId.value, format)
    await saveBlobResponse(res, `${detail.value?.title ?? 'writing'}.${format}`)
  } catch (err) {
    message.error(errorText(err, '导出失败'))
  }
}

function goList() {
  router.push({ path: '/studio', query: { tab: detail.value?.kind ?? 'novel' } })
}

// 同一 keep-alive 实例换作品 id：自己重载，否则还是上一本书的现场。
watch(
  () => route.params.id,
  (value) => {
    if (route.name !== 'WritingWorkbench') return
    const id = Number(value)
    if (!id || id === workId.value) return
    workId.value = id
    selectedCid.value = null
    saveState.value = 'idle'
    void load()
  },
)

// 关页/刷新前提醒未落盘的字。
function beforeUnload(event: BeforeUnloadEvent) {
  if (saveState.value === 'dirty' || saveState.value === 'saving') {
    event.preventDefault()
    event.returnValue = ''
  }
}

watch(infoOpen, (open) => {
  if (open) onOpenInfo()
})

// keep-alive 首挂载会先 onMounted 再 onActivated，挡掉第一次激活的重复请求。
let firstActivate = true
onMounted(() => void load())
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  // 从列表/别的标签切回：补拉一次，字数与外部改动都是新的。
  // PartEditor 只在 cid 变化时重载本地草稿，正在输入的字不会被覆盖。
  void load(false)
})
onDeactivated(() => editorRef.value?.flush())
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
window.addEventListener('beforeunload', beforeUnload)
</script>

<style scoped>
.studio-workbench {
  display: flex;
  min-height: calc(100vh - 120px);
  flex-direction: column;
}

.wb-topbar {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--hairline);
}

.wb-title-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.back-btn {
  flex: none;
}

.wb-title {
  font-size: 18px;
  font-weight: 600;
}

.wb-total {
  flex: none;
  font-size: 13px;
}

.wb-actions {
  display: flex;
  flex: none;
  gap: 8px;
}

.wb-alert {
  margin: 12px 0;
}

.wb-body {
  display: flex;
  min-height: 0;
  flex: 1;
  gap: 16px;
  margin-top: 14px;
}

.wb-outline {
  display: flex;
  flex: none;
  width: 260px;
  min-height: 0;
  flex-direction: column;
  padding-right: 16px;
  border-right: 1px solid var(--hairline);
}

.pane-title {
  flex: none;
  margin-bottom: 10px;
  color: var(--text-2);
  font-size: 13px;
  font-weight: 600;
}

.wb-main {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 16px;
}

.pane {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.wb-editor {
  flex: 1;
}

.wb-preview {
  flex: 1;
  padding-left: 16px;
  border-left: 1px solid var(--hairline);
  overflow: auto;
}

.markdown-body {
  font-size: 14px;
  line-height: 1.9;
}

.mobile-switch {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

@media (max-width: 768px) {
  .wb-topbar {
    flex-wrap: wrap;
  }

  .wb-body {
    flex-direction: column;
  }
}
</style>
