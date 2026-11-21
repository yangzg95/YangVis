<template>
  <div class="page-card writing-page">
    <div class="page-title">创作</div>
    <div class="subtitle">写小说、写文章：大纲与正文分块管理，自动保存，可导出 Markdown / TXT。</div>

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
        @press-enter="load"
        @clear="load"
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
              @confirm="onRemove(record)"
            >
              <a-button size="small" type="link" danger>删除</a-button>
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
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { DownOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import { writingApi, type WritingKind, type WritingWorkDetail, type WritingWorkItem } from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { errorText } from '@/utils/errors'
import { fmtDateTime } from '@/utils/format'
import WorkCreateModal from '@/components/writing/WorkCreateModal.vue'

// 智能应用 · 创作列表：一个菜单叶子 + 页内两 tab，tab 态同步 query，刷新/分享不丢。
const route = useRoute()
const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const works = ref<WritingWorkItem[]>([])
const loading = ref(false)
const keyword = ref('')
const createOpen = ref(false)

const activeKind = computed<WritingKind>(() => (route.query.tab === 'article' ? 'article' : 'novel'))
const activeTab = computed({
  get: () => activeKind.value,
  set: (value: string) => {
    void router.replace({ query: { ...route.query, tab: value } })
  },
})

const columns = [
  { key: 'title', title: '标题', dataIndex: 'title', ellipsis: true },
  { key: 'progress', title: '章节 / 字数', width: 150 },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 180 },
  { key: 'actions', title: '操作', width: 230 },
]

function subtitle(record: WritingWorkItem): string {
  const params = record.params || {}
  if (record.kind === 'novel') {
    return [params.genre, params.style].filter(Boolean).join(' · ') || '小说'
  }
  return [params.article_type, params.platform].filter(Boolean).join(' · ') || '文章'
}

async function load() {
  loading.value = true
  try {
    const data = await writingApi.list(activeKind.value, keyword.value.trim() || undefined)
    works.value = data.items
  } catch (err) {
    message.error(errorText(err, '作品列表加载失败'))
  } finally {
    loading.value = false
  }
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
onMounted(load)
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  void load()
})
</script>

<style scoped>
.writing-tabs {
  margin-top: 8px;
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
