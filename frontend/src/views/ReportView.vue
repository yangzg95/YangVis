<template>
  <div class="report-page">
    <header class="report-header">
      <div class="report-title" :title="state.title">{{ state.title || '报告' }}</div>
      <a-space :size="8">
        <a-tooltip :title="state.report ? '基于这份报告发起 AI 对话（新标签页）' : ''">
          <a-button size="small" :disabled="!state.report" @click="onStartDiscuss">
            <CommentOutlined /> 发起对话
          </a-button>
        </a-tooltip>
        <a-tooltip :title="state.report ? '保存为 Markdown 文件' : ''">
          <a-button size="small" :disabled="!state.report" @click="onDownload">
            <DownloadOutlined /> 下载报告
          </a-button>
        </a-tooltip>
        <a-tooltip v-if="netdiskBound" :title="state.report ? '保存到百度网盘的 reports/ 目录' : ''">
          <a-button
            size="small"
            :disabled="!state.report"
            :loading="savingToNetdisk"
            @click="onSaveToNetdisk"
          >
            <CloudUploadOutlined /> 存到网盘
          </a-button>
        </a-tooltip>
      </a-space>
    </header>

    <a-spin :spinning="state.loading" class="report-spin">
      <a-result v-if="state.error" status="warning" :title="state.error" />
      <a-empty
        v-else-if="!state.loading && !state.report"
        :image="simpleEmpty"
        description="报告尚未生成"
        class="report-empty"
      />
      <main v-else-if="state.report" class="report-content">
        <template v-if="state.suggestions?.length">
          <div class="section-title">改进意见</div>
          <a-list size="small" :data-source="state.suggestions" class="suggestion-list">
            <template #renderItem="{ item, index }">
              <a-list-item>
                <span class="suggestion-index">{{ index + 1 }}</span>
                <span>{{ item }}</span>
              </a-list-item>
            </template>
          </a-list>
          <a-divider />
        </template>
        <!-- 与对话页同样的安全设定：html:false，模型吐出的原始 HTML 一律转义。 -->
        <div class="markdown" v-html="renderMarkdown(state.report)" />
      </main>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Empty, message } from 'ant-design-vue'
import { CloudUploadOutlined, CommentOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import MarkdownIt from 'markdown-it'
import { netdiskApi, resumeApi } from '@/api'
import { saveTextFile } from '@/utils/download'
import { composeReportMarkdown } from '@/utils/report'

/** 三类报告共用的整页展示：/office/report/<kind>/<id>，由列表页新开标签页打开。 */

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const route = useRoute()
const router = useRouter()

const kind = String(route.params.kind)
const id = Number(route.params.id)

const state = reactive({
  loading: true,
  error: '',
  title: '',
  report: null as string | null,
  suggestions: null as string[] | null,
})

const md = new MarkdownIt({ html: false, linkify: true })
function renderMarkdown(text: string | null): string {
  return md.render(text || '')
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

async function loadReport() {
  state.loading = true
  try {
    if (kind === 'resume') {
      const detail = await resumeApi.detail(id)
      state.title = `分析报告 · ${detail.title}`
      state.report = detail.report
      state.suggestions = detail.suggestions
    } else if (kind === 'comparison') {
      const detail = await resumeApi.comparisonDetail(id)
      state.title = `对比报告 · ${detail.title || `#${detail.id}`}`
      state.report = detail.report
      state.suggestions = null
    } else {
      const detail = await resumeApi.toolkitTaskDetail(id)
      state.title = `生成报告 · ${detail.title}`
      state.report = detail.report
      state.suggestions = null
    }
    if (state.title) document.title = `${state.title} · 杨维斯`
  } catch (err) {
    state.error = errorText(err, '报告加载失败')
  } finally {
    state.loading = false
  }
}

/** 报告全文不塞进 URL：对话页拿到 kind/id 后自己拉取并组装（utils/report.ts），
 *  与「查看报告」同一 window.open 新标签页模式。 */
function onStartDiscuss() {
  if (!state.report) return
  const href = router.resolve({
    name: 'CustomerService',
    query: { report: `${kind}/${id}` },
  }).href
  window.open(href, '_blank', 'noopener')
}

/** 标题（如「生成报告 · 面试准备 · 高级前端工程师」）做文件名，
 *  清掉 Windows 与网盘路径都不允许的字符。 */
function reportFileName(): string {
  return `${state.title.replace(/[\\/:*?"<>|\s]+/g, '-') || '报告'}.md`
}

function onDownload() {
  if (!state.report) return
  saveTextFile(reportFileName(), composeReportMarkdown(state.report, state.suggestions))
  message.success('报告已开始下载')
}

/** 用户绑定了网盘才出现「存到网盘」按钮；未配置 AppKey 时整个入口都不该有。 */
const netdiskBound = ref(false)
const savingToNetdisk = ref(false)

async function loadNetdiskStatus() {
  try {
    const status = await netdiskApi.status()
    netdiskBound.value = status.configured && status.bound
  } catch {
    netdiskBound.value = false
  }
}

async function onSaveToNetdisk() {
  if (!state.report || savingToNetdisk.value) return
  savingToNetdisk.value = true
  try {
    const result = await netdiskApi.saveText(
      reportFileName(),
      composeReportMarkdown(state.report, state.suggestions),
    )
    message.success(`已保存到网盘：${result.path}`)
  } catch (err) {
    message.error(errorText(err, '保存到网盘失败'))
  } finally {
    savingToNetdisk.value = false
  }
}

onMounted(() => {
  void loadReport()
  // 网盘状态只是按钮显隐条件，不该卡住报告加载。
  void loadNetdiskStatus()
})
</script>

<style scoped>
.report-page {
  min-height: 100vh;
  background: #f5f5f5;
}
.report-header {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 24px;
  background: #fff;
  border-bottom: 1px solid #f0f0f0;
}
.report-title {
  font-size: 16px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.report-spin {
  display: block;
  min-height: 40vh;
}
.report-content {
  max-width: 880px;
  margin: 0 auto;
  padding: 24px 32px 64px;
  background: #fff;
  min-height: calc(100vh - 54px);
}
.report-empty {
  padding-top: 96px;
}
/* 排版在全局 assets/markdown.css（.markdown），这里只留正文的字号行高。 */
.report-content .markdown {
  line-height: 1.75;
  font-size: 14px;
}
.section-title {
  font-size: 15px;
  font-weight: 600;
  margin: 16px 0 8px;
}
.suggestion-list :deep(.ant-list-item) {
  padding: 6px 0;
}
.suggestion-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  margin-right: 8px;
  border-radius: 50%;
  background: rgba(10, 126, 134, 0.1);
  color: #0a7e86;
  font-size: 12px;
  flex: none;
}
</style>
