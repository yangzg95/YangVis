<template>
  <div class="page-card writing-page">
    <div class="page-title">创作</div>
    <div class="subtitle">写小说、写文章：大纲与正文分块管理，自动保存，可导出 Markdown / TXT。</div>

    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="AI 生成大纲依赖一个用途为「对话」的模型。不配置也能手工写作，只是生成入口不可用。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-tabs v-model:activeKey="activeTab" class="writing-tabs">
      <a-tab-pane key="novel" tab="写小说" />
      <a-tab-pane key="article" tab="写文章" />
    </a-tabs>

    <div class="toolbar toolbar--between toolbar--snug">
      <a-input
        v-model:value="keyword"
        allow-clear
        class="search-input"
        :placeholder="activeKind === 'novel' ? '搜书名或梗概' : '搜标题或主题'"
        @press-enter="() => load()"
        @clear="() => load()"
      >
        <template #prefix><SearchOutlined class="filter-icon" /></template>
      </a-input>
      <a-button type="primary" @click="openCreate">
        <PlusOutlined /> {{ activeKind === 'novel' ? '新建小说' : '新建文章' }}
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="works"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'">
          <div class="cell-title">{{ record.title }}</div>
          <div class="cell-sub">{{ subtitle(record) }}</div>
        </template>
        <template v-else-if="column.key === 'status'">
          <!-- 状态由最近一次运行翻译，点 tag 直接看那次运行的现场；没跑过的新作不显示。 -->
          <a-tooltip :title="statusHint(record.status, record.error_msg)">
            <a-tag
              v-if="record.status !== 'uploaded'"
              :color="statusColor(record.status)"
              class="run-tag"
              @click="openRuns(record)"
            >
              <LoadingOutlined v-if="record.status === 'analyzing'" />
              {{ statusText(record.status) }}
            </a-tag>
            <span v-else class="muted">待生成</span>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'progress'">
          {{ record.part_count }} {{ activeKind === 'novel' ? '章' : '段' }} · {{ record.word_count }} 字
        </template>
        <template v-else-if="column.key === 'updated_at'">
          <span class="muted">{{ fmtDateTime(record.updated_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4" wrap>
            <a-button size="small" type="link" @click="openWorkbench(record.id)">工作台</a-button>
            <a-dropdown>
              <a-button size="small" type="link">导出 <DownOutlined /></a-button>
              <template #overlay>
                <a-menu @click="onMenuExport($event, record)">
                  <a-menu-item key="md">Markdown (.md)</a-menu-item>
                  <a-menu-item key="txt">纯文本 (.txt)</a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>
            <a-popconfirm
              :title="`确认删除《${record.title}》？全部章节与正文都会删除。`"
              ok-text="删除"
              cancel-text="取消"
              :disabled="deleteDisabled(record)"
              @confirm="onRemove(record)"
            >
              <!-- disabled 按钮不响应鼠标事件，套 span 才能让置灰原因的 tooltip 生效 -->
              <a-tooltip :title="deleteDisabled(record) ? '有生成任务在跑，请完成后再删' : ''">
                <span class="delete-cell">
                  <a-button size="small" type="link" danger :disabled="deleteDisabled(record)">
                    删除
                  </a-button>
                </span>
              </a-tooltip>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
      <template #emptyText>
        <a-empty
          :image="simpleEmpty"
          :description="keyword ? '没有匹配的作品' : activeKind === 'novel' ? '还没有小说，新建一本开写' : '还没有文章，新建一篇开写'"
        />
      </template>
    </a-table>

    <WorkCreateModal
      v-model:open="createOpen"
      :kind="activeKind"
      @created="onCreated"
    />

    <!-- 进度与失败原因都长在认领作品的运行记录上，这里就是它的入口。 -->
    <WorkflowRunsDrawer
      v-model:open="runsOpen"
      :workflow="null"
      :subject="runsSubject"
      @changed="load(false)"
    />
  </div>
</template>

<script setup lang="ts">
import { h, computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { DownOutlined, LoadingOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { Button, Empty, message, notification } from 'ant-design-vue'
import {
  settingsApi,
  writingApi,
  type WorkflowRunSubject,
  type WritingKind,
  type WritingWorkDetail,
  type WritingWorkItem,
  type WritingWorkStatus,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { errorText } from '@/utils/errors'
import { fmtDateTime } from '@/utils/format'
import { createPollLoop } from '@/utils/polling'
import WorkCreateModal from '@/components/writing/WorkCreateModal.vue'
import WorkflowRunsDrawer from '@/components/WorkflowRunsDrawer.vue'

// 智能应用 · 创作列表：一个菜单叶子 + 页内两 tab，tab 态同步 query，刷新/分享不丢。
// AI 大纲是后台任务：列表行的状态从 workflow_run 翻译，有 analyzing 行才轮询。
const route = useRoute()
const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const works = ref<WritingWorkItem[]>([])
const loading = ref(false)
const keyword = ref('')
const createOpen = ref(false)
const chatReady = ref(true)

const runsOpen = ref(false)
const runsSubject = ref<WorkflowRunSubject | null>(null)

const activeKind = computed<WritingKind>(() => (route.query.tab === 'article' ? 'article' : 'novel'))
const activeTab = computed({
  get: () => activeKind.value,
  set: (value: string) => {
    void router.replace({ query: { ...route.query, tab: value } })
  },
})

const columns = [
  { key: 'title', title: '标题', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'progress', title: '章节 / 字数', width: 150 },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 180 },
  { key: 'actions', title: '操作', width: 230 },
]

function openRuns(record: WritingWorkItem) {
  runsSubject.value = { kind: 'writing-work', ref: record.id, label: record.title }
  runsOpen.value = true
}

/** 作品级运行或任一正文块在跑都不能删：后端有同一道闸门，前端先置灰少一次被拒。 */
function deleteDisabled(record: WritingWorkItem): boolean {
  return record.status === 'analyzing' || record.part_running
}

function statusHint(status: WritingWorkStatus, errorMsg: string | null): string {
  return status === 'error' && errorMsg ? `${errorMsg} · 点击查看运行记录` : '点击查看运行记录'
}

function statusText(status: WritingWorkStatus | string): string {
  return { analyzing: '生成中', ready: '已生成', error: '失败' }[status] || status
}

function statusColor(status: WritingWorkStatus | string): string {
  return { analyzing: 'processing', ready: 'success', error: 'error' }[status] || 'default'
}

function subtitle(record: WritingWorkItem): string {
  const params = record.params || {}
  if (record.kind === 'novel') {
    return [params.genre, params.style].filter(Boolean).join(' · ') || '小说'
  }
  return [params.article_type, params.platform].filter(Boolean).join(' · ') || '文章'
}

// 上一次看到的状态，用来识别「刚生成完」这个跃迁：轮询回调不是用户手势，
// 路由跳转由通知按钮触发，不在这里自动跳工作台。
const lastStatus = new Map<number, WritingWorkStatus>()

async function load(showLoading = true) {
  if (showLoading) loading.value = true
  try {
    const data = await writingApi.list(activeKind.value, keyword.value.trim() || undefined)
    works.value = data.items
    for (const item of data.items) {
      const previous = lastStatus.get(item.id)
      if (previous === 'analyzing' && item.status !== 'analyzing') {
        notifyFinished(item)
      }
      lastStatus.set(item.id, item.status)
    }
  } catch (err) {
    message.error(errorText(err, '作品列表加载失败'))
  } finally {
    if (showLoading) loading.value = false
  }
  syncPolling()
}

function notifyFinished(item: WritingWorkItem) {
  if (item.status === 'ready') {
    notification.success({
      key: `writing-outline-done-${item.id}`,
      message: `《${item.title}》大纲已生成`,
      description: `${item.part_count} 个${item.kind === 'novel' ? '章节' : '段落'}，可在工作台里调整`,
      btn: () =>
        h(
          Button,
          { size: 'small', type: 'primary', onClick: () => openWorkbench(item.id) },
          () => '打开工作台',
        ),
    })
  } else if (item.status === 'error') {
    notification.error({
      key: `writing-outline-done-${item.id}`,
      message: `《${item.title}》大纲生成失败`,
      description: item.error_msg || '模型没有返回可用结构，可重新生成',
    })
  }
}

// 生成是后台任务，列表靠轮询跟随；失败退避、页面隐藏暂停见 utils/polling.ts。
const pollLoop = createPollLoop(() => load(false), {
  shouldContinue: () => works.value.some((item) => item.status === 'analyzing'),
})

function syncPolling() {
  if (works.value.some((item) => item.status === 'analyzing')) pollLoop.start()
  else pollLoop.stop()
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态接口失败时按可用处理，真正的报错会在生成时冒出来。
    chatReady.value = true
  }
}

function goToSettings() {
  void router.push('/system/settings')
}

function openCreate() {
  createOpen.value = true
}

function onCreated(work: WritingWorkDetail) {
  void load()
  // 创建即进工作台：M1 不触发任何生成，第一站就是空白写作现场。
  openWorkbench(work.id)
}

function openWorkbench(id: number) {
  router.push({ name: 'WritingWorkbench', params: { id } })
}

async function onRemove(record: WritingWorkItem) {
  try {
    await writingApi.remove(record.id)
    message.success('已删除')
    void load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

function onMenuExport(info: { key: string }, record: WritingWorkItem) {
  void onExport(record, info.key as 'md' | 'txt')
}

async function onExport(record: WritingWorkItem, format: 'md' | 'txt') {
  try {
    const res = await writingApi.exportWork(record.id, format)
    await saveBlobResponse(res, `${record.title}.${format}`)
  } catch (err) {
    message.error(errorText(err, '导出失败'))
  }
}

// tab query 变化（含浏览器前进后退）：重置关键词并重载；keep-alive 复活时补拉，字数才是新的。
watch(activeKind, () => {
  keyword.value = ''
  void load()
})
// keep-alive 首挂载会先 onMounted 再 onActivated，用旗子挡掉第一次激活的重复请求。
let firstActivate = true
onMounted(() => {
  void Promise.all([loadReadiness(), load()])
})
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  void load(false)
  syncPolling()
})
// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
onDeactivated(() => pollLoop.stop())
onUnmounted(() => pollLoop.stop())
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}

.writing-tabs {
  margin-top: 8px;
}

.run-tag {
  cursor: pointer;
}

.delete-cell {
  display: inline-block;
}

.search-input {
  max-width: 320px;
}

.filter-icon {
  color: var(--text-3);
}

.cell-title {
  font-weight: 600;
}

.cell-sub {
  margin-top: 2px;
  color: var(--text-3);
  font-size: 12px;
}
</style>
