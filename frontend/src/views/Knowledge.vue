<template>
  <div class="page-card">
    <a-alert
      v-if="!loading && !embeddingReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的向量模型"
      description="知识库需要向量模型将文档转成可检索的向量。请先添加一个用途为「向量」的模型并通过连通性测试。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="indexState.model_mismatch"
      type="error"
      show-icon
      class="gate-alert"
      message="索引与当前向量模型不一致"
      :description="`知识库索引是用另一个向量模型建立的，检索结果不可靠。需要用当前模型重建全部索引（共 ${indexState.doc_count} 篇文档 / ${indexState.chunk_count} 个分片），期间会产生向量调用费用。`"
    >
      <template #action>
        <a-button size="small" danger :loading="rebuilding" @click="onRebuild">重建索引</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="indexState.status === 'error'"
      type="error"
      show-icon
      class="gate-alert"
      message="索引状态异常"
      :description="indexState.error_msg || '上次索引操作失败，可尝试重建。'"
    >
      <template #action>
        <a-button size="small" :loading="rebuilding" @click="onRebuild">重建索引</a-button>
      </template>
    </a-alert>

    <div class="toolbar">
      <a-space size="small" wrap class="kb-stats">
        <span>项目</span>
        <a-select
          :value="activeProjectId"
          style="width: 200px"
          placeholder="请选择项目"
          :options="projectOptions"
          :loading="loadingProjects"
          @change="selectProject"
        />
        <a-button @click="openProjects">管理项目</a-button>
        <a-divider type="vertical" />
        <a-tag :color="indexStatusColor">{{ indexStatusText }}</a-tag>
        <span class="muted">{{ indexState.doc_count }} 篇文档 · {{ indexState.chunk_count }} 个分片</span>
        <span v-if="indexState.vector_size" class="muted">· {{ indexState.vector_size }} 维</span>
      </a-space>
      <a-space>
        <a-tooltip :title="activeProjectId ? '' : '请先选择项目'">
          <a-button :disabled="!embeddingReady || !activeProjectId" @click="openSearch">
            检索预览
          </a-button>
        </a-tooltip>
        <a-popconfirm
          title="将删除并重建全部索引，期间检索不可用，且会产生向量调用费用。确认继续？"
          ok-text="确认重建"
          cancel-text="取消"
          @confirm="onRebuild"
        >
          <a-button :disabled="!embeddingReady || !indexState.doc_count" :loading="rebuilding">
            重建索引
          </a-button>
        </a-popconfirm>
      </a-space>
    </div>

    <a-row :gutter="16">
      <a-col :xs="24" :md="10" :lg="8">
        <a-card title="知识类型" size="small">
          <template #extra>
            <a-tooltip :title="activeProjectId ? '' : '请先选择项目'">
              <a-button
                type="primary"
                size="small"
                :disabled="!activeProjectId"
                @click="openCreateType"
              >
                <PlusOutlined /> 新建类型
              </a-button>
            </a-tooltip>
          </template>
          <!-- 首屏骨架占位；已有内容时刷新直接留旧列表。 -->
          <a-skeleton v-if="loadingTypes && !types.length" active :paragraph="{ rows: 4 }" />
          <template v-else>
            <a-empty v-if="!activeProjectId" description="请先选择一个项目" />
            <a-empty v-else-if="!types.length" description="该项目下暂无知识类型" />
            <a-list
              v-else
              :data-source="types"
              size="small"
              :pagination="types.length > 8 ? { pageSize: 8, size: 'small' } : false"
            >
              <template #renderItem="{ item }">
                <a-list-item
                  :class="['type-item', 'list-row', { active: item.id === activeTypeId }]"
                  @click="selectType(item.id)"
                >
                  <a-list-item-meta>
                    <template #title>
                      <span>{{ item.name }}</span>
                      <a-tag color="blue" style="margin-left: 8px">
                        {{ item.document_count }}
                      </a-tag>
                    </template>
                    <template #description>
                      <span class="muted">{{ item.description || '—' }}</span>
                    </template>
                  </a-list-item-meta>
                  <a-space>
                    <a-button size="small" @click.stop="openEditType(item)">编辑</a-button>
                    <a-popconfirm
                      title="确认删除该知识类型？其下文档与索引也会被删除。"
                      @confirm="onDeleteType(item.id)"
                    >
                      <a-button size="small" danger @click.stop>删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </a-list-item>
              </template>
            </a-list>
          </template>
        </a-card>
      </a-col>

      <a-col :xs="24" :md="14" :lg="16">
        <a-card :title="activeType ? `文档 · ${activeType.name}` : '文档'" size="small">
          <template #extra>
            <a-space>
              <a-button size="small" :disabled="!activeType" @click="refreshDocuments">
                <ReloadOutlined /> 刷新
              </a-button>
              <!-- 直接禁用，而不是让人点了再失败：一个收下文件、之后才
                   报错的上传按钮是最糟糕的做法。 -->
              <a-tooltip :title="uploadDisabledReason">
                <a-upload
                  :show-upload-list="false"
                  :before-upload="handleBeforeUpload"
                  :accept="acceptAttr"
                  :disabled="!canUpload || uploading"
                >
                  <a-button type="primary" :disabled="!canUpload || uploading">
                    <UploadOutlined /> 上传文档
                  </a-button>
                </a-upload>
              </a-tooltip>
              <a-tooltip :title="uploadDisabledReason">
                <!-- 单独一个 input：同一个文件选择器没办法既选文件又选目录，
                     所以上传文件夹这种情况只能自己配一个。 -->
                <a-upload
                  :show-upload-list="false"
                  :before-upload="handleBeforeFolder"
                  directory
                  multiple
                  :disabled="!canUpload || uploading"
                >
                  <a-button :disabled="!canUpload || uploading" :loading="uploading">
                    <FolderOpenOutlined /> 上传文件夹
                  </a-button>
                </a-upload>
              </a-tooltip>
            </a-space>
          </template>

          <div class="hint upload-hint">
            仅支持 {{ acceptLabel }}，单个文件不超过 10 MB。上传后在后台切片与向量化，状态会自动刷新。
            上传文件夹会递归读取子目录，其余格式自动跳过。
            <span v-if="batch.total" class="batch-progress">
              正在上传 {{ batch.done }} / {{ batch.total }}…
            </span>
          </div>

          <a-skeleton v-if="loadingDocs && !documents.length" active :paragraph="{ rows: 5 }" />
          <template v-else>
            <a-empty v-if="!activeType" description="请先选择一个知识类型" />
            <a-empty v-else-if="!documents.length" description="该类型下暂无文档" />
            <template v-else>
              <!-- 批量操作条只在有勾选时占位，平时不占纵向空间。 -->
              <div v-if="selectedDocIds.length" class="doc-batch-bar">
                <span>已选 {{ selectedDocIds.length }} 篇</span>
                <a-button
                  size="small"
                  :loading="batchOperating"
                  :disabled="!embeddingReady"
                  @click="onBatchReindex"
                >
                  重建索引
                </a-button>
                <a-button size="small" danger :disabled="batchOperating" @click="onBatchDelete">
                  删除
                </a-button>
                <a-button size="small" type="text" @click="selectedDocIds = []">取消</a-button>
              </div>
              <a-table
                :data-source="documents"
                :columns="docColumns"
                :row-selection="docRowSelection"
                row-key="id"
                size="small"
                :pagination="docPagination"
                @change="onDocPageChange"
              >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'size'">
                  {{ fmtSize(record.size) }}
                </template>

                <template v-else-if="column.key === 'status'">
                  <a-tooltip v-if="record.error_msg" :title="record.error_msg">
                    <a-tag color="error">失败</a-tag>
                  </a-tooltip>
                  <a-tag v-else :color="statusMeta(record.status).color">
                    {{ statusMeta(record.status).label }}
                    <template v-if="record.status === 'ready' && record.chunk_count">
                      · {{ record.chunk_count }} 片
                    </template>
                  </a-tag>
                </template>

                <template v-else-if="column.key === 'action'">
                  <a-space>
                    <a-button
                      size="small"
                      :disabled="!embeddingReady || record.status === 'indexing'"
                      @click="onReindex(record.id)"
                    >
                      重新索引
                    </a-button>
                    <a-popconfirm title="确认删除该文档？" @confirm="onDeleteDoc(record.id)">
                      <a-button size="small" danger>删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </template>
              </template>
              </a-table>
            </template>
          </template>
        </a-card>
      </a-col>
    </a-row>

    <a-modal
      v-model:open="typeModal.open"
      :title="typeModal.editing ? '编辑知识类型' : '新建知识类型'"
      @ok="submitType"
      :confirm-loading="typeModal.loading"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="typeModal.form.name" placeholder="例如：产品文档" />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea
            v-model:value="typeModal.form.description"
            :rows="3"
            placeholder="可选，描述该知识类型的用途"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="projectModal.open"
      title="项目管理"
      :footer="null"
      width="640px"
    >
      <div class="hint" style="margin-bottom: 12px">
        项目是检索的边界：对话时先选项目，只会读到该项目下的文档。
      </div>
      <a-form layout="inline" class="project-form">
        <a-form-item>
          <a-input v-model:value="projectModal.form.name" placeholder="项目名称" style="width: 180px" />
        </a-form-item>
        <a-form-item>
          <a-input
            v-model:value="projectModal.form.description"
            placeholder="描述（可选）"
            style="width: 220px"
          />
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button type="primary" :loading="projectModal.loading" @click="submitProject">
              {{ projectModal.editingId ? '保存' : '新建' }}
            </a-button>
            <a-button v-if="projectModal.editingId" @click="resetProjectForm">取消编辑</a-button>
          </a-space>
        </a-form-item>
      </a-form>

      <a-empty v-if="!projects.length" description="还没有项目" />
      <a-list v-else :data-source="projects" size="small">
        <template #renderItem="{ item }">
          <a-list-item>
            <a-list-item-meta>
              <template #title>
                <span>{{ item.name }}</span>
                <a-tag color="blue" style="margin-left: 8px">
                  {{ item.type_count }} 类型 · {{ item.document_count }} 文档
                </a-tag>
              </template>
              <template #description>
                <span class="muted">{{ item.description || '—' }}</span>
              </template>
            </a-list-item-meta>
            <a-space>
              <a-button size="small" @click="editProject(item)">编辑</a-button>
              <a-popconfirm
                title="删除项目会连同其下的知识类型、文档与索引一起删除，确认继续？"
                ok-text="确认删除"
                cancel-text="取消"
                @confirm="onDeleteProject(item.id)"
              >
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </a-list-item>
        </template>
      </a-list>
    </a-modal>

    <a-modal
      v-model:open="searchModal.open"
      title="检索预览"
      :footer="null"
      width="720px"
    >
      <div class="hint" style="margin-bottom: 12px">
        在「{{ activeProject?.name || '—' }}」范围内检索。这里返回的分片，就是对话时模型会读到的上下文。
      </div>
      <a-input-search
        v-model:value="searchModal.query"
        placeholder="输入一个问题，看看会命中哪些内容"
        enter-button="检索"
        :loading="searchModal.loading"
        @search="runSearch"
      />
      <div v-if="searchModal.searched" class="search-results">
        <a-empty
          v-if="!searchModal.hits.length"
          description="没有命中任何内容。相关度低于阈值的分片会被丢弃，避免用无关内容拼出看似合理的错误回答。"
        />
        <a-list v-else :data-source="searchModal.hits" size="small">
          <template #renderItem="{ item, index }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>
                  <a-tag>[{{ index + 1 }}]</a-tag>
                  <span>{{ item.filename }}</span>
                  <span class="muted"> · 第 {{ item.seq + 1 }} 片 · 相关度 {{ item.score.toFixed(3) }}</span>
                </template>
                <template #description>
                  <div class="hit-content">{{ item.content }}</div>
                </template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { message, Modal, type UploadProps } from 'ant-design-vue'
import {
  FolderOpenOutlined,
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import { fmtSize } from '@/utils/format'
import {
  knowledgeApi,
  settingsApi,
  SUPPORTED_UPLOAD_EXTENSIONS,
  type DocumentStatus,
  type IndexState,
  type KbProject,
  type KnowledgeDocument,
  type KnowledgeType,
  type SearchHit,
} from '@/api'

const router = useRouter()

const STATUS_META: Record<DocumentStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'default' },
  indexing: { label: '索引中', color: 'processing' },
  ready: { label: '就绪', color: 'success' },
  error: { label: '失败', color: 'error' },
}

const INDEX_STATUS_META: Record<string, { label: string; color: string }> = {
  uninitialized: { label: '未建立索引', color: 'default' },
  indexing: { label: '索引中', color: 'processing' },
  ready: { label: '索引就绪', color: 'success' },
  rebuilding: { label: '重建中', color: 'processing' },
  error: { label: '索引异常', color: 'error' },
}

function statusMeta(status: DocumentStatus) {
  return STATUS_META[status] ?? { label: status, color: 'default' }
}

const acceptAttr = SUPPORTED_UPLOAD_EXTENSIONS.join(',')
const acceptLabel = SUPPORTED_UPLOAD_EXTENSIONS.join(' / ')
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

const projects = ref<KbProject[]>([])
const activeProjectId = ref<number | null>(null)
const types = ref<KnowledgeType[]>([])
const documents = ref<KnowledgeDocument[]>([])
const activeTypeId = ref<number | null>(null)
// 文档表格走服务端分页：页码/总数都以后端为准，不再全量拉回来前端切。
const docPage = ref(1)
const docPageSize = 8
const docTotal = ref(0)
const loading = ref(true)
const loadingProjects = ref(false)
const loadingTypes = ref(false)
const loadingDocs = ref(false)
const uploading = ref(false)
const rebuilding = ref(false)
const embeddingReady = ref(false)

const indexState = ref<IndexState>({
  status: 'uninitialized',
  collection_name: null,
  vector_size: 0,
  doc_count: 0,
  chunk_count: 0,
  error_msg: null,
  model_mismatch: false,
  updated_at: null,
})

const docColumns = [
  { title: '名称', dataIndex: 'name', ellipsis: true },
  { title: '大小', key: 'size', dataIndex: 'size', width: 100 },
  { title: '状态', key: 'status', dataIndex: 'status', width: 130 },
  { title: '上传时间', dataIndex: 'created_at', width: 180 },
  { title: '操作', key: 'action', width: 180 },
]

const activeProject = computed(
  () => projects.value.find((p) => p.id === activeProjectId.value) || null,
)

const projectOptions = computed(() =>
  projects.value.map((p) => ({ value: p.id, label: p.name })),
)

const activeType = computed(
  () => types.value.find((t) => t.id === activeTypeId.value) || null,
)

const canUpload = computed(() => embeddingReady.value && activeType.value !== null)

const uploadDisabledReason = computed(() => {
  if (!embeddingReady.value) return '请先在「设置」中配置并测试通过一个向量模型'
  if (!activeProjectId.value) return '请先选择一个项目'
  if (!activeType.value) return '请先选择一个知识类型'
  return ''
})

const indexStatusText = computed(
  () => INDEX_STATUS_META[indexState.value.status]?.label ?? indexState.value.status,
)
const indexStatusColor = computed(
  () => INDEX_STATUS_META[indexState.value.status]?.color ?? 'default',
)

/** 只要还有文档在后台处理中就为 true。 */
const hasRunningJobs = computed(() =>
  documents.value.some((d) => d.status === 'pending' || d.status === 'indexing'),
)

// ---- 加载 ------------------------------------------------------------------

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    embeddingReady.value = status.embedding.ready
  } catch {
    embeddingReady.value = false
  }
}

async function loadIndexState() {
  try {
    indexState.value = await knowledgeApi.indexState()
  } catch {
    /* 拦截器已经提示过了 */
  }
}

async function loadProjects() {
  loadingProjects.value = true
  try {
    const data = await knowledgeApi.listProjects()
    projects.value = data.items
    if (!projects.value.some((p) => p.id === activeProjectId.value)) {
      activeProjectId.value = projects.value[0]?.id ?? null
    }
  } catch {
    /* 拦截器已经提示过了 */
  } finally {
    loadingProjects.value = false
  }
}

async function loadTypes() {
  const projectId = activeProjectId.value
  if (!projectId) {
    // 这个接口必须带项目；没有项目也就没什么可列的。
    types.value = []
    activeTypeId.value = null
    documents.value = []
    docPage.value = 1
    docTotal.value = 0
    return
  }
  loadingTypes.value = true
  try {
    const data = await knowledgeApi.listTypes(projectId)
    // 等响应期间换了项目就整包丢弃，别把旧项目的类型灌进来。
    if (projectId !== activeProjectId.value) return
    types.value = data.items
    if (types.value.length && !types.value.some((t) => t.id === activeTypeId.value)) {
      selectType(types.value[0].id)
    } else if (!types.value.length) {
      activeTypeId.value = null
      documents.value = []
      docPage.value = 1
      docTotal.value = 0
    }
  } catch {
    /* 拦截器已经提示过了 */
  } finally {
    // 同上：只对「还是当前项目」的那次加载收尾。
    if (projectId === activeProjectId.value) loadingTypes.value = false
  }
}

function selectProject(id: number) {
  activeProjectId.value = id
  // 类型是挂在单个项目下面的，所以旧项目里选中的东西一个都不能留。
  activeTypeId.value = null
  documents.value = []
  docPage.value = 1
  docTotal.value = 0
  void loadTypes()
}

// 切换类型的竞态守卫：快速连点时慢响应不得覆盖新选择；被顶掉的请求
// 也不能碰 loadingDocs（否则会把新请求亮着的加载态误关掉）。
let docsLoadSeq = 0

async function loadDocuments(typeId: number, { silent = false } = {}) {
  const seq = ++docsLoadSeq
  if (!silent) loadingDocs.value = true
  try {
    const data = await knowledgeApi.listDocuments(typeId, docPage.value, docPageSize)
    if (seq !== docsLoadSeq || typeId !== activeTypeId.value) return
    documents.value = data.items
    docTotal.value = data.total
    // 删除把当前页清空了就往回退一页再拉，别把人留在空白页上。
    if (!data.items.length && data.total > 0 && docPage.value > 1) {
      docPage.value -= 1
      await loadDocuments(typeId, { silent: true })
    }
  } catch {
    /* 拦截器已经提示过了；轮询（silent）失败下次轮询会再试。 */
  } finally {
    if (seq === docsLoadSeq) loadingDocs.value = false
  }
}

const docPagination = computed(() => ({
  current: docPage.value,
  pageSize: docPageSize,
  total: docTotal.value,
  size: 'small' as const,
  showTotal: (total: number) => `共 ${total} 篇`,
}))

function onDocPageChange(pagination: { current?: number }) {
  docPage.value = pagination.current ?? 1
  // 翻页也意味着上一页的勾选项看不见了，留着只会误伤。
  selectedDocIds.value = []
  if (activeTypeId.value) loadDocuments(activeTypeId.value)
}

function refreshDocuments() {
  if (activeTypeId.value) loadDocuments(activeTypeId.value)
}

function selectType(id: number) {
  activeTypeId.value = id
  // 选中态与页码都不跨类型带过去：那些 id 在另一个列表里不存在，
  // 旧列表翻到第 5 页也不代表新列表有第 5 页。
  selectedDocIds.value = []
  docPage.value = 1
  loadDocuments(id)
}

// ---- 文档批量操作 -------------------------------------------------------------
// 表格多选后批量删除/重建索引，各走后端一个批量端点（不刷 N 次单条请求）。

const selectedDocIds = ref<number[]>([])
const batchOperating = ref(false)

const docRowSelection = computed(() => ({
  selectedRowKeys: selectedDocIds.value,
  onChange: (keys: (string | number)[]) => {
    selectedDocIds.value = keys.map(Number)
  },
}))

async function onBatchReindex() {
  if (!selectedDocIds.value.length || batchOperating.value) return
  batchOperating.value = true
  try {
    const res = await knowledgeApi.batchReindexDocuments(selectedDocIds.value)
    message.success(`已重新排队索引 ${res.done} 篇文档`)
    selectedDocIds.value = []
    refreshDocuments()
  } catch (err) {
    message.error(errorText(err, '批量重建索引失败'))
  } finally {
    batchOperating.value = false
  }
}

function onBatchDelete() {
  const ids = [...selectedDocIds.value]
  if (!ids.length) return
  Modal.confirm({
    title: `删除选中的 ${ids.length} 篇文档？`,
    content: '文档与其向量索引会一并删除，不可恢复。',
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: async () => {
      const res = await knowledgeApi.batchDeleteDocuments(ids)
      message.success(`已删除 ${res.done} 篇文档`)
      selectedDocIds.value = []
      if (activeTypeId.value) {
        await Promise.all([loadDocuments(activeTypeId.value), loadTypes(), loadIndexState()])
      }
    },
  })
}

// 索引是在后台任务里跑的，没有推送通道，所以只要还有任务在进行，表格就轮询，
// 全部落定之后立刻停下来——没有任务时连定时器都不挂，不空转。keep-alive 下
// 切去别的标签页（deactivated）也要停表，回来时先补一轮再决定要不要继续。
let pollTimer: number | undefined

async function pollOnce() {
  if (!activeTypeId.value || !hasRunningJobs.value) return
  await loadDocuments(activeTypeId.value, { silent: true })
  if (!hasRunningJobs.value) {
    await Promise.all([loadIndexState(), loadTypes()])
  }
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

/** 有后台任务才挂表；hasRunningJobs 翻边时由 watch 调进来，落定即停。 */
function syncPolling() {
  if (hasRunningJobs.value && pollTimer === undefined) {
    pollTimer = window.setInterval(pollOnce, 2500)
  } else if (!hasRunningJobs.value) {
    stopPolling()
  }
}

watch(hasRunningJobs, syncPolling)

// ---- 项目 ------------------------------------------------------------------

const projectModal = reactive({
  open: false,
  loading: false,
  editingId: 0,
  form: { name: '', description: '' },
})

function openProjects() {
  resetProjectForm()
  projectModal.open = true
}

function resetProjectForm() {
  projectModal.editingId = 0
  projectModal.form = { name: '', description: '' }
}

function editProject(p: KbProject) {
  projectModal.editingId = p.id
  projectModal.form = { name: p.name, description: p.description || '' }
}

async function submitProject() {
  const name = projectModal.form.name.trim()
  if (!name) {
    message.warning('请输入项目名称')
    return
  }
  projectModal.loading = true
  try {
    const payload = { name, description: projectModal.form.description.trim() || undefined }
    if (projectModal.editingId) {
      await knowledgeApi.updateProject(projectModal.editingId, payload)
      message.success('已更新')
    } else {
      const created = await knowledgeApi.createProject(payload)
      message.success('已创建')
      // 刚建好的项目，八成就是接下来要操作的那个。
      activeProjectId.value = created.id
    }
    resetProjectForm()
    await loadProjects()
    await loadTypes()
  } finally {
    projectModal.loading = false
  }
}

async function onDeleteProject(id: number) {
  await knowledgeApi.deleteProject(id)
  message.success('已删除')
  if (projectModal.editingId === id) resetProjectForm()
  if (activeProjectId.value === id) {
    activeProjectId.value = null
    activeTypeId.value = null
    documents.value = []
  }
  await loadProjects()
  await Promise.all([loadTypes(), loadIndexState()])
}

// ---- 知识类型 --------------------------------------------------------------

const typeModal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: { id: 0, name: '', description: '' },
})

function openCreateType() {
  if (!activeProjectId.value) {
    message.warning('请先选择项目')
    return
  }
  typeModal.editing = false
  typeModal.form = { id: 0, name: '', description: '' }
  typeModal.open = true
}

function openEditType(t: KnowledgeType) {
  typeModal.editing = true
  typeModal.form = { id: t.id, name: t.name, description: t.description || '' }
  typeModal.open = true
}

async function submitType() {
  const name = typeModal.form.name.trim()
  if (!name) {
    message.warning('请输入名称')
    return
  }
  typeModal.loading = true
  try {
    const payload = {
      name,
      description: typeModal.form.description.trim() || undefined,
    }
    if (typeModal.editing) {
      await knowledgeApi.updateType(typeModal.form.id, payload)
      message.success('已更新')
    } else {
      if (!activeProjectId.value) {
        message.warning('请先选择项目')
        return
      }
      await knowledgeApi.createType({ ...payload, project_id: activeProjectId.value })
      message.success('已创建')
    }
    typeModal.open = false
    await Promise.all([loadTypes(), loadProjects()])
  } finally {
    typeModal.loading = false
  }
}

async function onDeleteType(id: number) {
  await knowledgeApi.deleteType(id)
  message.success('已删除')
  if (activeTypeId.value === id) {
    activeTypeId.value = null
    documents.value = []
  }
  await Promise.all([loadTypes(), loadProjects(), loadIndexState()])
}

// ---- 文档 ------------------------------------------------------------------

/** 说明这个文件为什么不能上传；没问题时返回 null。 */
function rejectReason(file: File): string | null {
  const dot = file.name.lastIndexOf('.')
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
  if (!SUPPORTED_UPLOAD_EXTENSIONS.includes(ext)) {
    return `暂不支持 ${ext || '该'} 格式，目前仅支持 ${acceptLabel}`
  }
  if (!file.size) return '文件为空'
  if (file.size > MAX_UPLOAD_BYTES) return '文件过大，上限 10 MB'
  return null
}

/**
 * 文档最终存下来时用的名字。
 *
 * 上传文件夹时会保留相对路径，这样不同子目录下的两个 `README.md` 还能区分
 * 得开。截断是从前面截的，因为真正能标识它的是最里层的目录和文件名；字段
 * 长度上限是 255。
 */
function documentName(file: File): string {
  const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
  return path.length <= 255 ? path : path.slice(-255)
}

const handleBeforeUpload: UploadProps['beforeUpload'] = async (file) => {
  const target = file as unknown as File
  const reason = rejectReason(target)
  if (reason) {
    message.warning(reason)
    return false
  }
  await uploadBatch([target], { confirm: false })
  // 一律返回 false：请求是在这里自己发的，不走 a-upload。
  return false
}

const handleBeforeFolder: UploadProps['beforeUpload'] = async (file, fileList) => {
  // beforeUpload 会对选中的每个文件各触发一次，而每次拿到的都是同一份完整
  // 列表。只在最后一次调用时动手，就把它变成了一个批次，而不是 N 个互相
  // 抢着刷新表格的并发请求。
  if (file !== fileList[fileList.length - 1]) return false
  await uploadBatch(fileList as unknown as File[], { confirm: true })
  return false
}

const batch = reactive({ done: 0, total: 0 })

async function uploadBatch(files: File[], { confirm }: { confirm: boolean }) {
  const typeId = activeTypeId.value
  if (!typeId) {
    message.warning('请先选择知识类型')
    return
  }

  // 选文件夹是盲选的 —— 里面有什么就一股脑全带过来 —— 所以在这里先把不支持
  // 的文件筛掉，而不是让它们一个一个地失败。
  const accepted: File[] = []
  let skipped = 0
  for (const file of files) {
    if (rejectReason(file)) skipped += 1
    else accepted.push(file)
  }

  if (!accepted.length) {
    message.warning(`没有可上传的文件，已跳过 ${skipped} 个不支持或过大的文件`)
    return
  }

  if (confirm && !(await confirmBatch(accepted.length, skipped))) return

  uploading.value = true
  batch.total = accepted.length
  batch.done = 0
  const failures: string[] = []
  try {
    // 刻意串行：每次上传都会排一个索引任务，一口气丢几百个过去，
    // 向量服务商和 worker 池都得被压垮。这样跑完是慢一些，
    // 但它至少真的能跑完。
    for (const file of accepted) {
      try {
        await knowledgeApi.uploadDocument(typeId, file, documentName(file))
      } catch (err) {
        failures.push(`${documentName(file)}（${errorText(err, '上传失败')}）`)
      }
      batch.done += 1
    }
  } finally {
    uploading.value = false
    batch.total = 0
    batch.done = 0
  }

  reportBatch(accepted.length - failures.length, skipped, failures)
  await Promise.all([loadDocuments(typeId), loadTypes(), loadProjects()])
}

/** 先说清楚接下来会发生什么，毕竟索引这么多文件是要花钱的。 */
function confirmBatch(count: number, skipped: number): Promise<boolean> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: '确认上传',
      content:
        `将上传 ${count} 个文件` +
        (skipped ? `，跳过 ${skipped} 个不支持或过大的文件` : '') +
        '。每个文件都会切片并调用向量模型，会产生费用。',
      okText: '开始上传',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

function reportBatch(ok: number, skipped: number, failures: string[]) {
  const skipText = skipped ? `，跳过 ${skipped} 个` : ''
  if (!failures.length) {
    message.success(`已上传 ${ok} 个文件${skipText}，正在后台切片与向量化`)
    return
  }
  // 只列前几条，否则一个有问题的文件夹会刷出一大片没法读的内容。
  const shown = failures.slice(0, 3).join('；')
  const more = failures.length > 3 ? ` 等 ${failures.length} 个` : ''
  message.error(`${ok} 个已上传${skipText}，失败：${shown}${more}`, 8)
}

async function onReindex(id: number) {
  try {
    await knowledgeApi.reindexDocument(id)
    message.success('已重新排队索引')
    refreshDocuments()
  } catch (err) {
    message.error(errorText(err, '重新索引失败'))
  }
}

async function onDeleteDoc(id: number) {
  await knowledgeApi.deleteDocument(id)
  message.success('已删除')
  if (activeTypeId.value) {
    await Promise.all([loadDocuments(activeTypeId.value), loadTypes(), loadIndexState()])
  }
}

// ---- 索引 ------------------------------------------------------------------

async function onRebuild() {
  rebuilding.value = true
  try {
    const result = await knowledgeApi.rebuildIndex()
    if (result.started) {
      message.success(result.message)
    } else {
      message.info(result.message)
    }
    await Promise.all([loadIndexState(), activeTypeId.value ? loadDocuments(activeTypeId.value) : null])
  } catch (err) {
    message.error(errorText(err, '重建失败'))
  } finally {
    rebuilding.value = false
  }
}

// ---- 检索预览 --------------------------------------------------------------

const searchModal = reactive({
  open: false,
  loading: false,
  searched: false,
  query: '',
  hits: [] as SearchHit[],
})

function openSearch() {
  searchModal.open = true
  searchModal.searched = false
  searchModal.hits = []
}

async function runSearch() {
  const query = searchModal.query.trim()
  if (!query || !activeProjectId.value) return
  searchModal.loading = true
  try {
    // 限定在当前选中的项目范围内，这样预览结果和对话时看到的是一致的。
    const result = await knowledgeApi.search({
      query,
      top_k: 8,
      project_id: activeProjectId.value,
    })
    searchModal.hits = result.hits
    searchModal.searched = true
  } catch (err) {
    message.error(errorText(err, '检索失败'))
  } finally {
    searchModal.loading = false
  }
}

// ---- 辅助函数 --------------------------------------------------------------

function goToSettings() {
  router.push('/settings')
}

// 文件大小格式化统一用 utils/format 的 fmtSize（812 B / 4.2 K / 30.1 M）。

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([loadReadiness(), loadIndexState(), loadProjects()])
    // 加载类型必须先确定项目，所以这一步没法并进上面那批一起发。
    await loadTypes()
  } finally {
    loading.value = false
  }
  syncPolling()
})

// keep-alive：切走时停表；回来时本地状态可能是旧的，先补一轮再按结果挂表。
onDeactivated(stopPolling)
onActivated(() => {
  void pollOnce().finally(syncPolling)
})

onUnmounted(stopPolling)
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.toolbar {
  margin: 4px 0 16px;
  justify-content: space-between;
}
.kb-stats {
  font-size: 13px;
}
/* hover/active 底色用全局 .list-row；.muted/.hint 用全局共享类（style.css）。 */
.upload-hint {
  margin-bottom: 12px;
}
.batch-progress {
  color: var(--signal-text);
  margin-left: 4px;
}
/* 文档批量操作条：选中才出现，信号色浅底与全局选中态同族。 */
.doc-batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--signal-bg);
  font-size: 12px;
}
.project-form {
  margin-bottom: 12px;
}
.search-results {
  margin-top: 16px;
  max-height: 420px;
  overflow-y: auto;
}
.hit-content {
  white-space: pre-wrap;
  word-break: break-word;
  color: rgba(0, 0, 0, 0.65);
  font-size: 12px;
  line-height: 1.7;
}
</style>
