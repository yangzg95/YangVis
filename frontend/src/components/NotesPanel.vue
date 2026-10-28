<template>
  <div>
    <div class="toolbar notes-toolbar">
      <span class="muted">
        共 {{ total }} 条笔记<template v-if="appliedKeyword">，「{{ appliedKeyword }}」匹配 {{ records.length }} 条</template>
      </span>
      <a-space>
        <a-input-search
          v-model:value="keyword"
          allow-clear
          class="keyword-input"
          placeholder="搜索标题或正文"
          @search="onSearch"
          @change="onKeywordChange"
        />
        <a-button type="primary" @click="openCreate">
          <PlusOutlined /> 新建笔记
        </a-button>
      </a-space>
    </div>

    <a-table
      :columns="columns"
      :data-source="records"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'">
          <div class="cell-title">{{ record.title }}</div>
          <div class="cell-sub note-snippet">{{ record.snippet || '（无正文）' }}</div>
        </template>
        <template v-else-if="column.key === 'updated_at'">
          <span class="muted">{{ formatTime(record.updated_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4">
            <a-button size="small" type="link" @click="openEdit(record)">编辑</a-button>
            <a-popconfirm
              title="确认删除这条笔记？删除后不可恢复。"
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
          :description="appliedKeyword ? '没有匹配的笔记' : '还没有笔记，点右上角「新建笔记」开始记录'"
        />
      </template>
    </a-table>

    <!-- ================= 编辑抽屉 ================= -->
    <!-- 笔记正文偏长，用宽抽屉而不是弹窗；宽度沿用简历预览抽屉的移动端约定。 -->
    <a-drawer
      v-model:open="drawer.open"
      :width="isMobile ? '100%' : 720"
      :title="drawer.id ? '编辑笔记' : '新建笔记'"
    >
      <div v-if="drawer.loading" class="drawer-loading">
        <a-spin />
      </div>
      <template v-else>
        <a-input
          v-model:value="drawer.title"
          :maxlength="200"
          placeholder="笔记标题"
          class="title-input"
        />
        <div class="editor-bar">
          <a-radio-group v-model:value="drawer.mode" type="button" size="small">
            <a-radio-button value="edit">编辑</a-radio-button>
            <a-radio-button value="preview">预览</a-radio-button>
          </a-radio-group>
          <span class="muted editor-hint">支持 Markdown 语法</span>
        </div>
        <a-textarea
          v-if="drawer.mode === 'edit'"
          v-model:value="drawer.content"
          :rows="20"
          :maxlength="100000"
          placeholder="记点什么：JD 片段、谈薪策略、公司调研、待办提醒……"
          class="note-textarea"
        />
        <div v-else class="markdown note-preview" v-html="renderedContent"></div>
      </template>
      <template #footer>
        <a-space>
          <a-button @click="drawer.open = false">取消</a-button>
          <a-button type="primary" :loading="drawer.saving" @click="submit">保存</a-button>
        </a-space>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Empty, message } from 'ant-design-vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import { noteApi, type NoteItem } from '@/api'
import { createMarkdown } from '@/utils/markdown'
import { useIsMobile } from '@/utils/useIsMobile'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const isMobile = useIsMobile()
const md = createMarkdown()

const columns = [
  { key: 'title', title: '标题', dataIndex: 'title', ellipsis: true },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 180 },
  { key: 'actions', title: '操作', width: 140 },
]

// ---- 列表与搜索（搜索走后端，正文不在列表行里，客户端筛不到）--------------------

const records = ref<NoteItem[]>([])
const total = ref(0)
const loading = ref(false)
const keyword = ref('')
const appliedKeyword = ref('')

async function loadNotes() {
  loading.value = true
  try {
    const result = await noteApi.list(appliedKeyword.value || undefined)
    records.value = result.items
    total.value = result.total
  } finally {
    loading.value = false
  }
}

function onSearch(value: string) {
  appliedKeyword.value = value.trim()
  void loadNotes()
}

// allowClear 的清空叉只发 change、不发 search：清空时立刻回到全量列表。
function onKeywordChange(event: Event) {
  if (!(event.target as HTMLInputElement).value && appliedKeyword.value) {
    appliedKeyword.value = ''
    void loadNotes()
  }
}

// ---- 新建 / 编辑 ---------------------------------------------------------------

const drawer = reactive({
  open: false,
  loading: false,
  saving: false,
  id: 0,
  title: '',
  content: '',
  mode: 'edit' as 'edit' | 'preview',
})

function openCreate() {
  Object.assign(drawer, {
    open: true,
    loading: false,
    saving: false,
    id: 0,
    title: '',
    content: '',
    mode: 'edit',
  })
}

async function openEdit(record: NoteItem) {
  Object.assign(drawer, {
    open: true,
    loading: true,
    saving: false,
    id: record.id,
    title: record.title,
    content: '',
    mode: 'edit',
  })
  try {
    // 列表只带摘要，编辑前拉完整正文。
    const detail = await noteApi.detail(record.id)
    drawer.title = detail.title
    drawer.content = detail.content || ''
  } catch {
    drawer.open = false
  } finally {
    drawer.loading = false
  }
}

const renderedContent = computed(() => md.render(drawer.content || ''))

async function submit() {
  if (!drawer.title.trim()) {
    message.warning('请输入标题')
    return
  }
  drawer.saving = true
  try {
    const payload = { title: drawer.title.trim(), content: drawer.content.trim() }
    if (drawer.id) {
      await noteApi.update(drawer.id, payload)
      message.success('已保存')
    } else {
      await noteApi.create(payload)
      message.success('已记录')
    }
    drawer.open = false
    await loadNotes()
  } finally {
    drawer.saving = false
  }
}

async function onRemove(record: NoteItem) {
  try {
    await noteApi.remove(record.id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadNotes()
}

// ---- 展示辅助 -----------------------------------------------------------------

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

onMounted(loadNotes)
</script>

<style scoped>
.notes-toolbar {
  margin: 4px 0 16px;
  justify-content: space-between;
}
.keyword-input {
  width: 220px;
}
.cell-title {
  font-weight: 500;
}
.cell-sub {
  color: var(--text-3);
  font-size: var(--fs-xs);
}
/* 摘要最多两行，超出省略号：表格行高度保持一致。 */
.note-snippet {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.drawer-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}
.title-input {
  font-weight: 600;
}
.editor-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 8px;
}
.editor-hint {
  font-size: var(--fs-xs);
}
.note-textarea {
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.note-preview {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  min-height: 400px;
}
</style>
