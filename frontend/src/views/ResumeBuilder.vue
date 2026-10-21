<template>
  <div class="rb-page">
    <header class="rb-head">
      <a-button type="text" title="返回简历列表" @click="goBack">
        <ArrowLeftOutlined />
      </a-button>
      <a-input
        v-model:value="title"
        class="title-input"
        :maxlength="128"
        placeholder="简历标题"
        :bordered="false"
        @change="markDirty"
      />
      <span v-if="dirty" class="dirty-hint">未保存</span>
      <a-select v-model:value="doc.template" size="small" class="template-select">
        <a-select-option v-for="t in TEMPLATE_OPTIONS" :key="t.value" :value="t.value">
          <span>{{ t.label }}</span>
          <span class="tpl-hint">{{ t.hint }}</span>
        </a-select-option>
      </a-select>
      <span class="theme-swatches" title="主题配色">
        <button
          v-for="t in THEME_OPTIONS"
          :key="t.value"
          type="button"
          class="theme-dot"
          :class="{ active: doc.theme === t.value }"
          :style="{ backgroundColor: t.accent }"
          :title="t.label"
          @click="doc.theme = t.value"
        />
      </span>
      <span class="head-spacer" />
      <a-button size="small" @click="showPreview = !showPreview">
        <EyeOutlined /> {{ showPreview ? '隐藏预览' : '预览' }}
      </a-button>
      <a-button size="small" @click="exportPdf">
        <FilePdfOutlined /> 导出 PDF
      </a-button>
      <a-button size="small" type="primary" :loading="saving" @click="onSave">
        <SaveOutlined /> 保存
      </a-button>
    </header>

    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="rb-gate"
      message="未配置可用的对话模型，AI 帮写暂不可用"
      description="编辑、排序与导出不受影响。配置一个通过连通性测试的默认对话模型后即可使用 AI。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="router.push('/system/settings')">
          去配置
        </a-button>
      </template>
    </a-alert>

    <a-spin :spinning="loading" wrapper-class-name="rb-spin">
      <main class="rb-body" :class="{ 'preview-on': showPreview }">
        <aside class="rb-nav-pane">
          <ModuleNav
            v-model="doc.module_order"
            v-model:activeKey="activeModule"
            :counts="moduleCounts"
            @add="addEntry"
          />
        </aside>

        <section class="rb-form-pane">
          <div class="form-title">{{ MODULE_LABELS[activeModule] }}</div>
          <ProfileForm
            v-if="loaded && activeModule === 'profile'"
            :profile="doc.profile"
            :doc-id="docId"
            :chat-ready="chatReady"
            @change="markDirty"
          />
          <WorkForm
            v-else-if="loaded && activeModule === 'companies'"
            :entries="doc.companies"
            :doc-id="docId"
            :chat-ready="chatReady"
            @change="markDirty"
          />
          <ProjectForm
            v-else-if="loaded && activeModule === 'projects'"
            :entries="doc.projects"
            :doc-id="docId"
            :chat-ready="chatReady"
            @change="markDirty"
          />
          <EducationForm
            v-else-if="loaded && activeModule === 'educations'"
            :entries="doc.educations"
            :doc-id="docId"
            :chat-ready="chatReady"
            @change="markDirty"
          />
          <SkillsForm
            v-else-if="loaded && activeModule === 'skills'"
            v-model:skillsHtml="doc.skills_html"
            :doc-id="docId"
            :chat-ready="chatReady"
            @change="markDirty"
          />
        </section>

        <aside v-if="showPreview" ref="previewPaneEl" class="rb-preview-pane">
          <div class="preview-scroll">
            <div class="preview-scale" :style="scaleStyle">
              <iframe
                title="简历预览"
                class="preview-frame"
                :srcdoc="previewFrameHtml"
                :style="{ transform: `scale(${previewScale})` }"
              />
            </div>
          </div>
        </aside>
      </main>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
/**
 * 在线简历整页工作台：bare 路由 + 列表页 window.open 新开标签页
 * （避开 App.vue keep-alive 按组件缓存的多实例问题）。
 * 左模块导航（可拖拽排序）/ 中表单 / 右 A4 实时预览；Ctrl+S 保存，
 * 未保存关页提醒；导出复用幻灯片的浏览器打印链路。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  ArrowLeftOutlined,
  EyeOutlined,
  FilePdfOutlined,
  SaveOutlined,
} from '@ant-design/icons-vue'
import ModuleNav from '@/components/resume/ModuleNav.vue'
import ProfileForm from '@/components/resume/ProfileForm.vue'
import WorkForm from '@/components/resume/WorkForm.vue'
import ProjectForm from '@/components/resume/ProjectForm.vue'
import EducationForm from '@/components/resume/EducationForm.vue'
import SkillsForm from '@/components/resume/SkillsForm.vue'
import { resumeBuilderApi, settingsApi } from '@/api'
import { printSlideDeck } from '@/utils/slidePrint'
import {
  MODULE_LABELS,
  TEMPLATE_OPTIONS,
  THEME_OPTIONS,
  createEmptyDoc,
  createEntry,
  type EntryModuleKey,
  type ResumeDocData,
  type ResumeModuleKey,
} from '@/utils/resumeDoc'
import { renderDoc } from '@/utils/resumeTemplate'

const route = useRoute()
const router = useRouter()
const docId = Number(route.params.id)

const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const dirty = ref(false)
const title = ref('')
const chatReady = ref(true)
const activeModule = ref<ResumeModuleKey>('profile')
const showPreview = ref(true)

const doc = reactive<ResumeDocData>(createEmptyDoc())

const moduleCounts = computed(() => ({
  companies: doc.companies.length,
  projects: doc.projects.length,
  educations: doc.educations.length,
}))

// 预览是整份文档的全量消毒 + iframe srcdoc 重载，条目多了以后每个按键都同步
// 重渲染会占满主线程（拖拽/滚动/输入法都会卡）。改为停顿 300ms 后只渲一次；
// 导出 PDF 时直接 renderDoc(doc)，永远取最新内容、不等防抖。
const previewFrameHtml = ref('')
let previewTimer: ReturnType<typeof setTimeout> | undefined

function schedulePreview() {
  if (!showPreview.value) return
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(() => {
    previewTimer = undefined
    previewFrameHtml.value = renderDoc(doc)
  }, 300)
}

// ---- 预览缩放（A4 794px 等比塞进右栏）----------------------------------------

const A4_WIDTH = 794
const A4_HEIGHT = 1123
const previewPaneEl = ref<HTMLElement | null>(null)
const previewScale = ref(0.6)

const scaleStyle = computed(() => ({
  width: `${A4_WIDTH * previewScale.value}px`,
  height: `${A4_HEIGHT * previewScale.value}px`,
}))

function measurePreview() {
  const width = previewPaneEl.value?.clientWidth
  if (width) previewScale.value = Math.min(1, Math.max(0.3, (width - 24) / A4_WIDTH))
}

// ---- 载入 / 保存 ---------------------------------------------------------------

function applyDetail(detail: { title: string; doc: ResumeDocData }) {
  title.value = detail.title
  doc.template = detail.doc.template
  doc.theme = detail.doc.theme
  doc.module_order = [...detail.doc.module_order]
  Object.assign(doc.profile, detail.doc.profile)
  doc.companies.splice(0, doc.companies.length, ...detail.doc.companies)
  doc.projects.splice(0, doc.projects.length, ...detail.doc.projects)
  doc.educations.splice(0, doc.educations.length, ...detail.doc.educations)
  doc.skills_html = detail.doc.skills_html
}

async function load() {
  loading.value = true
  try {
    const detail = await resumeBuilderApi.detail(docId)
    suppressDirty = true
    applyDetail(detail)
    loaded.value = true
    dirty.value = false
    await nextTick()
    suppressDirty = false
  } catch {
    // 拦截器已经弹过错误提示。
  } finally {
    loading.value = false
  }
}

function markDirty() {
  if (loaded.value) dirty.value = true
}

// 深度监听覆盖所有嵌套改动（富文本、排序、条目增删）。服务端归一化回写时
// 短暂抑制，否则刚保存完「未保存」徽标又会被回写触发点亮。
let suppressDirty = false
watch(
  doc,
  () => {
    if (!suppressDirty) markDirty()
    schedulePreview()
  },
  { deep: true },
)

// 重新打开预览立即出最新版面（隐藏期间不排渲染）；侧栏 v-if 出来后再量缩放宽度。
watch(showPreview, (visible) => {
  if (visible) {
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = undefined
    previewFrameHtml.value = renderDoc(doc)
    requestAnimationFrame(measurePreview)
  } else if (previewTimer) {
    clearTimeout(previewTimer)
    previewTimer = undefined
  }
})

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const detail = await resumeBuilderApi.update(docId, {
      title: title.value.trim() || undefined,
      doc: JSON.parse(JSON.stringify(doc)) as ResumeDocData,
    })
    // 用服务端归一化过的结果对齐（旧字段补齐、消毒后的 HTML），回写不点亮 dirty。
    suppressDirty = true
    applyDetail(detail)
    dirty.value = false
    await nextTick()
    suppressDirty = false
    message.success('已保存')
  } catch {
    message.error('保存失败，请重试')
  } finally {
    saving.value = false
  }
}

function addEntry(kind: EntryModuleKey) {
  if (kind === 'companies') doc.companies.push(createEntry('companies') as ResumeDocData['companies'][number])
  else if (kind === 'projects') doc.projects.push(createEntry('projects') as ResumeDocData['projects'][number])
  else doc.educations.push(createEntry('educations') as ResumeDocData['educations'][number])
  markDirty()
}

function exportPdf() {
  // 允许导出未保存的当前内容（打印走的是本地文档快照）。
  if (!printSlideDeck(renderDoc(doc))) {
    message.warning('浏览器拦住了新标签页，放行弹窗后再导出')
  }
}

async function goBack() {
  router.push('/office/resume')
}

// ---- 快捷键 / 离开提醒 / 模型门槛 ---------------------------------------------

function onKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void onSave()
  }
}

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}

async function checkChatReady() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态查不到不挡编辑，AI 按钮点开后由流式接口自己报错。
    chatReady.value = true
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('resize', measurePreview)
  previewFrameHtml.value = renderDoc(doc)
  await load()
  measurePreview()
  void checkChatReady()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
  window.removeEventListener('resize', measurePreview)
  if (previewTimer) clearTimeout(previewTimer)
})
</script>

<style scoped>
.rb-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f5f6f7;
}
.rb-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  background: #fff;
  border-bottom: 1px solid #ebedf0;
  flex-shrink: 0;
}
.title-input {
  width: 240px;
  font-size: 15px;
  font-weight: 600;
}
.dirty-hint {
  font-size: 12px;
  color: #fa8c16;
}
.template-select {
  width: 188px;
}
.tpl-hint {
  margin-left: 8px;
  font-size: 12px;
  color: #9ca3af;
}
.theme-swatches {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 10px;
}
.theme-dot {
  width: 18px;
  height: 18px;
  padding: 0;
  border: 2px solid #fff;
  border-radius: 50%;
  box-shadow: 0 0 0 1px #d9d9d9;
  cursor: pointer;
  transition: transform 0.12s ease;
}
.theme-dot:hover {
  transform: scale(1.15);
}
.theme-dot.active {
  box-shadow: 0 0 0 2px #1f2329;
}
.head-spacer {
  flex: 1;
}
.rb-gate {
  margin: 10px 16px 0;
  flex-shrink: 0;
}
.rb-spin {
  flex: 1;
  overflow: hidden;
  display: flex;
}
.rb-body {
  flex: 1;
  display: flex;
  gap: 12px;
  padding: 12px 16px;
  min-height: 0;
}
.rb-nav-pane {
  width: 180px;
  flex-shrink: 0;
  background: #fff;
  border-radius: 8px;
  padding: 12px;
  overflow-y: auto;
}
.rb-form-pane {
  flex: 1;
  min-width: 0;
  background: #fff;
  border-radius: 8px;
  padding: 16px 20px;
  overflow-y: auto;
}
.form-title {
  font-size: 16px;
  font-weight: 700;
  color: #1f2329;
  margin-bottom: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid #f0f0f0;
}
.rb-preview-pane {
  width: 560px;
  flex-shrink: 0;
  background: #fff;
  border-radius: 8px;
  padding: 12px 0;
}
.preview-scroll {
  height: 100%;
  overflow-y: auto;
  display: flex;
  justify-content: center;
}
.preview-scale {
  transform-origin: top center;
}
.preview-frame {
  width: 794px;
  height: 1123px;
  border: none;
  transform-origin: top left;
}
@media (max-width: 1200px) {
  .rb-preview-pane {
    display: none;
  }
}
</style>
