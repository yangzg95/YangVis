<template>
  <div class="page-card">
    <div class="page-title">简历</div>

    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="AI 分析与简历对比都依赖对话模型。请先添加一个用途为「对话」的模型、通过连通性测试并设为默认。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-tabs v-model:activeKey="activeTab" class="resume-tabs" @change="onTabChange">
      <!-- ================= 在线简历 ================= -->
      <a-tab-pane key="docs" tab="在线简历">
        <div class="toolbar toolbar--between toolbar--snug">
          <span class="muted">共 {{ docList.length }} 份在线简历，模块可自由排序、一键导出 PDF</span>
          <a-button type="primary" :loading="creatingDoc" @click="createDoc">
            <PlusOutlined /> 新建简历
          </a-button>
        </div>

        <a-table
          :columns="docColumns"
          :data-source="docList"
          :loading="loadingDocs"
          row-key="id"
          size="middle"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="cell-title">{{ record.title }}</div>
              <div class="cell-sub">在线编辑 · 最后保存 {{ fmtDateTime(record.updated_at) }}</div>
            </template>
            <template v-else-if="column.key === 'updated_at'">
              <span class="muted">{{ fmtDateTime(record.updated_at) }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space :size="4" wrap>
                <a-button size="small" type="primary" @click="openDoc(record)">编辑</a-button>
                <a-button
                  size="small"
                  type="link"
                  :disabled="!chatReady"
                  @click="openDocChat(record)"
                >
                  AI 对话
                </a-button>
                <a-button size="small" type="link" @click="openRenameDoc(record)">重命名</a-button>
                <a-popconfirm
                  title="确认删除这份在线简历？删除后不可恢复。"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="removeDoc(record)"
                >
                  <a-button size="small" type="link" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
          <template #emptyText>
            <a-empty :image="simpleEmpty" description="还没有在线简历，新建一份开始编辑" />
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ================= 简历列表 ================= -->
      <a-tab-pane key="resumes" tab="简历列表">
        <div class="toolbar toolbar--between toolbar--snug">
          <span class="muted">
            共 {{ resumes.length }} 份简历<template v-if="selectedIds.length">，已选 {{ selectedIds.length }} 份</template>
          </span>
          <a-space>
            <a-tooltip :title="selectedIds.length < 2 ? '勾选至少 2 份简历后可对比' : ''">
              <a-button :disabled="selectedIds.length < 2" @click="openCompare">
                <SwapOutlined /> 对比所选
              </a-button>
            </a-tooltip>
            <a-button type="primary" @click="openUpload">
              <UploadOutlined /> 上传简历
            </a-button>
          </a-space>
        </div>

        <a-table
          :columns="resumeColumns"
          :data-source="resumes"
          :loading="loadingResumes"
          :row-selection="rowSelection"
          row-key="id"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="cell-title">{{ record.title }}</div>
              <div class="cell-sub">{{ record.filename }}</div>
            </template>
            <template v-else-if="column.key === 'size'">
              <span class="muted">{{ fmtSize(record.size) }}</span>
            </template>
            <template v-else-if="column.key === 'description'">
              <span class="muted">{{ record.description || '—' }}</span>
            </template>
            <template v-else-if="column.key === 'status'">
              <!-- 状态是从认领这份简历的运行翻译出来的，点标签直接看那次运行的现场。 -->
              <a-tooltip :title="statusHint(record.status, record.error_msg)">
                <a-tag
                  :color="statusColor(record.status)"
                  class="run-tag"
                  @click="openResumeRuns(record)"
                >
                  <LoadingOutlined v-if="record.status === 'analyzing'" />
                  {{ statusText(record.status) }}
                </a-tag>
              </a-tooltip>
              <a-tooltip v-if="record.has_netdisk" title="原件已同步到百度网盘">
                <CloudOutlined class="netdisk-icon" />
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ fmtDateTime(record.created_at) }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space :size="4" wrap>
                <a-button size="small" type="link" @click="openPreview(record)">预览</a-button>
                <a-button
                  v-if="record.has_report"
                  size="small"
                  type="link"
                  @click="openReport(record.id)"
                >
                  查看报告
                </a-button>
                <a-button
                  size="small"
                  type="link"
                  :disabled="!chatReady || record.status === 'analyzing'"
                  @click="onAnalyze(record)"
                >
                  {{ record.has_report ? '重新分析' : '生成分析' }}
                </a-button>
                <a-button size="small" type="link" @click="openEdit(record)">编辑</a-button>
                <a-button
                  v-if="record.has_netdisk"
                  size="small"
                  type="link"
                  :loading="downloadingId === record.id"
                  @click="onDownload(record)"
                >
                  下载原件
                </a-button>
                <a-popconfirm
                  :title="
                    record.has_netdisk
                      ? '确认删除这份简历？分析报告与网盘里的原件将一并删除。'
                      : '确认删除这份简历？其分析报告将一并删除。'
                  "
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="onRemove(record)"
                >
                  <a-button
                    size="small"
                    type="link"
                    danger
                    :disabled="record.status === 'analyzing'"
                  >
                    删除
                  </a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
          <template #emptyText>
            <a-empty :image="simpleEmpty" description="还没有简历，先上传一份吧" />
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ================= 对比记录 ================= -->
      <a-tab-pane key="comparisons" tab="对比记录">
        <a-table
          :columns="comparisonColumns"
          :data-source="comparisons"
          :loading="loadingComparisons"
          row-key="id"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="cell-title">{{ record.title || `对比 #${record.id}` }}</div>
              <div class="cell-sub">{{ record.resume_titles.join(' vs ') }}</div>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tooltip :title="statusHint(record.status, record.error_msg)">
                <a-tag
                  :color="statusColor(record.status)"
                  class="run-tag"
                  @click="openComparisonRuns(record)"
                >
                  <LoadingOutlined v-if="record.status === 'analyzing'" />
                  {{ statusText(record.status) }}
                </a-tag>
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ fmtDateTime(record.created_at) }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space :size="4">
                <a-button
                  size="small"
                  type="link"
                  :disabled="record.status !== 'ready'"
                  @click="openComparisonReport(record.id)"
                >
                  查看报告
                </a-button>
                <a-popconfirm
                  title="确认删除这条对比记录？"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="onRemoveComparison(record)"
                >
                  <a-button
                    size="small"
                    type="link"
                    danger
                    :disabled="record.status === 'analyzing'"
                  >
                    删除
                  </a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
          <template #emptyText>
            <a-empty :image="simpleEmpty" description="暂无对比记录。在简历列表勾选多份简历即可发起对比" />
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>

    <!-- ================= 上传弹窗 ================= -->
    <a-modal
      v-model:open="uploadModal.open"
      title="上传简历"
      :confirm-loading="uploadModal.loading"
      ok-text="上传"
      cancel-text="取消"
      @ok="submitUpload"
    >
      <a-upload-dragger
        :file-list="uploadModal.fileList"
        :before-upload="beforeUpload"
        :max-count="1"
        accept=".pdf,.docx,.txt,.md,.markdown"
        class="upload-dragger"
        @remove="uploadModal.fileList = []"
      >
        <p class="ant-upload-drag-icon"><InboxOutlined /></p>
        <p class="ant-upload-text">点击或拖拽简历文件到此处</p>
        <p class="ant-upload-hint">支持 PDF / Word（docx）/ 纯文本，单文件不超过 10 MB</p>
      </a-upload-dragger>
      <a-form layout="vertical" class="upload-form">
        <a-form-item label="标题（留空则取文件名）">
          <a-input v-model:value="uploadModal.title" :maxlength="128" placeholder="如：张三-前端工程师-3年" />
        </a-form-item>
        <a-form-item label="描述（可选，AI 分析时会作为参考背景）">
          <a-textarea
            v-model:value="uploadModal.description"
            :rows="3"
            :maxlength="512"
            placeholder="如：应聘高级前端岗位，重点关注工程化经验"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 编辑弹窗 ================= -->
    <a-modal
      v-model:open="editModal.open"
      title="编辑简历信息"
      :confirm-loading="editModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitEdit"
    >
      <a-form layout="vertical">
        <a-form-item label="标题" required>
          <a-input v-model:value="editModal.title" :maxlength="128" />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="editModal.description" :rows="3" :maxlength="512" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 在线简历重命名 ================= -->
    <a-modal
      v-model:open="docRename.open"
      title="重命名简历"
      :confirm-loading="docRename.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitRenameDoc"
    >
      <a-input
        v-model:value="docRename.title"
        :maxlength="128"
        placeholder="简历标题"
        @pressEnter="submitRenameDoc"
      />
    </a-modal>

    <!-- ================= 对比确认弹窗 ================= -->
    <a-modal
      v-model:open="compareModal.open"
      title="对比所选简历"
      :confirm-loading="compareModal.loading"
      ok-text="开始对比"
      cancel-text="取消"
      @ok="submitCompare"
    >
      <p class="compare-tip">将对以下 {{ compareModal.resumes.length }} 份简历做一次横向对比分析：</p>
      <a-list size="small" bordered :data-source="compareModal.resumes" class="compare-list">
        <template #renderItem="{ item }">
          <a-list-item>{{ item.title }}<span class="muted">（{{ item.filename }}）</span></a-list-item>
        </template>
      </a-list>
      <a-form layout="vertical" class="compare-form">
        <a-form-item label="对比标题（可选）">
          <a-input v-model:value="compareModal.title" :maxlength="128" placeholder="如：前端候选人初筛" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 预览抽屉 ================= -->
    <!-- 手机上抽屉占满屏：antd-vue 4 的 Drawer 没有 max-width 上限，固定 720
         会超出视口。 -->
    <a-drawer
      v-model:open="previewDrawer.open"
      :width="isMobile ? '100%' : 720"
      :title="previewDrawer.title || '简历预览'"
    >
      <div v-if="previewDrawer.loading" class="preview-loading">
        <a-spin />
      </div>
      <template v-else>
        <div class="preview-meta muted">
          {{ previewDrawer.filename }}<template v-if="previewDrawer.size">
            · {{ fmtSize(previewDrawer.size) }}
          </template>
        </div>
        <pre class="preview-content">{{ previewDrawer.content }}</pre>
      </template>
    </a-drawer>

    <!-- 报告不再用抽屉看：「查看报告」统一新开标签页进 /office/report/...（见 ReportView）。 -->

    <!-- 状态标签点开的运行现场：列表上那个「分析中/失败」到底是哪次运行、跑了什么。 -->
    <WorkflowRunsDrawer
      v-model:open="runsOpen"
      :workflow="null"
      :subject="runsSubject"
      @changed="onRunsChanged"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, h, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Empty, message, notification } from 'ant-design-vue'
import {
  CloudOutlined,
  InboxOutlined,
  LoadingOutlined,
  PlusOutlined,
  SwapOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import type { UploadFile } from 'ant-design-vue'
import {
  resumeApi,
  resumeBuilderApi,
  settingsApi,
  type ResumeComparisonItem,
  type ResumeDocItem,
  type ResumeItem,
  type ResumeStatus,
  type WorkflowRunSubject,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { fmtDateTime, fmtSize } from '@/utils/format'
import { errorText } from '@/utils/errors'
import { createPollLoop } from '@/utils/polling'
import { useIsMobile } from '@/utils/useIsMobile'
import WorkflowRunsDrawer from '@/components/WorkflowRunsDrawer.vue'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const router = useRouter()
const isMobile = useIsMobile()

// ---- 状态 -------------------------------------------------------------------

const chatReady = ref(true)
const activeTab = ref('docs')

// ---- 在线简历（自主编辑工作台）--------------------------------------------------

const docList = ref<ResumeDocItem[]>([])
const loadingDocs = ref(false)
const creatingDoc = ref(false)
const docsLoaded = ref(false)
const docRename = reactive({ open: false, loading: false, id: 0, title: '' })

const docColumns = [
  { key: 'title', title: '简历', dataIndex: 'title', ellipsis: true },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 220 },
  { key: 'actions', title: '操作', width: 300 },
]

async function loadDocs(force = false) {
  if (loadingDocs.value || (docsLoaded.value && !force)) return
  loadingDocs.value = true
  try {
    const result = await resumeBuilderApi.list()
    docList.value = result.items
    docsLoaded.value = true
  } catch {
    // 拦截器已弹提示。
  } finally {
    loadingDocs.value = false
  }
}

async function createDoc() {
  creatingDoc.value = true
  try {
    const detail = await resumeBuilderApi.create('未命名简历')
    await loadDocs(true)
    openDoc(detail)
  } catch (err) {
    message.error(errorText(err, '创建失败'))
  } finally {
    creatingDoc.value = false
  }
}

function openDoc(record: ResumeDocItem) {
  // 工作台 bare 整页，新开标签页避开 keep-alive 多实例（同幻灯片工作台）。
  const href = router.resolve({ name: 'ResumeBuilder', params: { id: record.id } }).href
  window.open(href, '_blank', 'noopener')
}

function openDocChat(record: ResumeDocItem) {
  if (!chatReady.value) return
  // 对话页读 query 里的引用自行拉取简历全文；同样新开标签页（整页路由 +
  // keep-alive 多实例考量，与编辑工作台一致）。
  const href = router.resolve({ name: 'Chat', query: { 'resume-doc': record.id } }).href
  window.open(href, '_blank', 'noopener')
}

function openRenameDoc(record: ResumeDocItem) {
  docRename.id = record.id
  docRename.title = record.title
  docRename.open = true
}

async function submitRenameDoc() {
  if (!docRename.title.trim()) {
    message.warning('请输入标题')
    return
  }
  docRename.loading = true
  try {
    await resumeBuilderApi.update(docRename.id, { title: docRename.title.trim() })
    message.success('已保存')
    docRename.open = false
    await loadDocs(true)
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    docRename.loading = false
  }
}

async function removeDoc(record: ResumeDocItem) {
  try {
    await resumeBuilderApi.remove(record.id)
    message.success('已删除')
    await loadDocs(true)
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

function onTabChange(key: string) {
  if (key === 'docs') void loadDocs()
}

const resumes = ref<ResumeItem[]>([])
const loadingResumes = ref(false)
const selectedIds = ref<number[]>([])

const comparisons = ref<ResumeComparisonItem[]>([])
const loadingComparisons = ref(false)

const uploadModal = reactive({
  open: false,
  loading: false,
  fileList: [] as UploadFile[],
  title: '',
  description: '',
})

const editModal = reactive({
  open: false,
  loading: false,
  id: 0,
  title: '',
  description: '',
})

const compareModal = reactive({
  open: false,
  loading: false,
  resumes: [] as ResumeItem[],
  title: '',
})

const downloadingId = ref(0)

const previewDrawer = reactive({
  open: false,
  loading: false,
  title: '',
  filename: '',
  size: 0,
  content: '',
})

const resumeColumns = [
  { key: 'title', title: '简历', dataIndex: 'title', ellipsis: true },
  { key: 'size', title: '大小', dataIndex: 'size', width: 90 },
  { key: 'description', title: '描述', dataIndex: 'description', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '上传时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 300 },
]

const comparisonColumns = [
  { key: 'title', title: '对比', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '发起时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 180 },
]

const rowSelection = computed(() => ({
  selectedRowKeys: selectedIds.value,
  onChange: (keys: (string | number)[]) => {
    selectedIds.value = keys.map(Number)
  },
  getCheckboxProps: (record: ResumeItem) => ({
    disabled: record.status === 'analyzing',
  }),
}))

// ---- 加载与轮询 --------------------------------------------------------------

// 分析是后台任务，这里用轮询跟随状态变化；没有进行中的任务就停表。
// 失败自动退避、页面隐藏暂停（详见 utils/polling.ts），不再固定 2.5s 硬打。
const pollLoop = createPollLoop(() => Promise.all([loadResumes(), loadComparisons()]), {
  shouldContinue: needsPolling,
})

async function loadResumes() {
  loadingResumes.value = true
  try {
    const result = await resumeApi.list()
    resumes.value = result.items
    // 删掉后还留在勾选框里的 id 要同步清掉。
    const alive = new Set(result.items.map((item) => item.id))
    selectedIds.value = selectedIds.value.filter((id) => alive.has(id))
  } finally {
    loadingResumes.value = false
  }
}

async function loadComparisons() {
  loadingComparisons.value = true
  try {
    const result = await resumeApi.listComparisons()
    comparisons.value = result.items
  } finally {
    loadingComparisons.value = false
  }
}

function needsPolling(): boolean {
  return (
    resumes.value.some((item) => item.status === 'analyzing') ||
    comparisons.value.some((item) => item.status === 'analyzing')
  )
}

function syncPolling() {
  if (needsPolling()) pollLoop.start()
  else pollLoop.stop()
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态接口失败时按可用处理，真正的报错会在触发分析时冒出来。
    chatReady.value = true
  }
}

// ---- 简历操作 ---------------------------------------------------------------

function openUpload() {
  uploadModal.fileList = []
  uploadModal.title = ''
  uploadModal.description = ''
  uploadModal.open = true
}

function beforeUpload(file: File) {
  // 返回 false 拦住 antd 的自动上传，文件先攒着，点「上传」时和表单一起提交。
  uploadModal.fileList = [file as unknown as UploadFile]
  if (!uploadModal.title) {
    uploadModal.title = file.name.replace(/\.[^.]+$/, '')
  }
  return false
}

async function submitUpload() {
  const file = uploadModal.fileList[0]
  if (!file) {
    message.warning('请先选择简历文件')
    return
  }
  uploadModal.loading = true
  try {
    await resumeApi.upload(
      file as unknown as File,
      uploadModal.title.trim(),
      uploadModal.description.trim(),
    )
    message.success('上传成功')
    uploadModal.open = false
    await loadResumes()
  } catch (err) {
    message.error(errorText(err, '上传失败'))
  } finally {
    uploadModal.loading = false
  }
}

function openEdit(record: ResumeItem) {
  editModal.id = record.id
  editModal.title = record.title
  editModal.description = record.description || ''
  editModal.open = true
}

async function submitEdit() {
  if (!editModal.title.trim()) {
    message.warning('标题不能为空')
    return
  }
  editModal.loading = true
  try {
    await resumeApi.update(editModal.id, {
      title: editModal.title.trim(),
      description: editModal.description.trim(),
    })
    message.success('已保存')
    editModal.open = false
    await loadResumes()
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    editModal.loading = false
  }
}

async function onRemove(record: ResumeItem) {
  try {
    await resumeApi.remove(record.id)
    message.success('已删除')
    await loadResumes()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 预览 --------------------------------------------------------------------
// 看的是上传时抽取好的纯文本（PDF/DOCX/TXT 统一了），不依赖网盘里有没有原件。

async function openPreview(record: ResumeItem) {
  previewDrawer.open = true
  previewDrawer.loading = true
  previewDrawer.title = record.title
  previewDrawer.filename = record.filename
  previewDrawer.size = record.size
  previewDrawer.content = ''
  try {
    const detail = await resumeApi.preview(record.id)
    previewDrawer.content = detail.content
  } catch (err) {
    previewDrawer.open = false
    message.error(errorText(err, '加载预览失败'))
  } finally {
    previewDrawer.loading = false
  }
}

async function onAnalyze(record: ResumeItem) {
  try {
    await resumeApi.analyze(record.id)
    message.success(`已开始分析「${record.title}」`)
    await loadResumes()
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '分析发起失败'))
  }
}

// ---- 网盘原件下载 -------------------------------------------------------------
// 绑定/解绑统一在「模型与集成 → 百度网盘」里做；这里只消费行上的 has_netdisk 标记。

async function onDownload(record: ResumeItem) {
  downloadingId.value = record.id
  try {
    const res = await resumeApi.download(record.id)
    await saveBlobResponse(res, record.filename)
  } catch (err) {
    message.error(errorText(err, '下载原件失败'))
  } finally {
    downloadingId.value = 0
  }
}

// ---- 对比 --------------------------------------------------------------------

function openCompare() {
  const selected = resumes.value.filter((item) => selectedIds.value.includes(item.id))
  if (selected.length < 2) return
  compareModal.resumes = selected
  compareModal.title = ''
  compareModal.open = true
}

async function submitCompare() {
  compareModal.loading = true
  try {
    const comparison = await resumeApi.compare(
      compareModal.resumes.map((item) => item.id),
      compareModal.title.trim(),
    )
    message.success('已开始对比分析，完成后可在「对比记录」中查看')
    compareModal.open = false
    selectedIds.value = []
    await loadComparisons()
    syncPolling()
    // 等这份对比跑完，弹出带「查看报告」按钮的通知，用户不用干等着盯状态。
    watchComparison(comparison.id)
  } catch (err) {
    message.error(errorText(err, '对比发起失败'))
  } finally {
    compareModal.loading = false
  }
}

/** 轮询单条对比直到终态，完成时弹出带「查看报告」按钮的通知。
 *  定时器和盯着的 id 都登记在模块变量里：之前 timer 是局部变量，组件卸载后
 *  没人能清掉它，离开页面也会一直请求下去（泄漏）。 */
let watchTimer: number | undefined
let watchedComparisonId: number | null = null

function watchComparison(id: number) {
  stopWatchingComparison()
  watchedComparisonId = id
  watchTimer = window.setInterval(checkWatchedComparison, 2500)
}

async function checkWatchedComparison() {
  const id = watchedComparisonId
  if (id === null) return
  try {
    const detail = await resumeApi.comparisonDetail(id)
    if (detail.status === 'analyzing') return
    stopWatchingComparison()
    if (detail.status === 'ready') {
      // 报告在新标签页里看，而轮询回调不是用户手势，window.open 会被
      // 浏览器拦。所以这里弹一条带按钮的通知，点开那一下就是手势。
      notification.success({
        key: `comparison-done-${detail.id}`,
        message: '对比分析完成',
        description: detail.title || `对比 #${detail.id}`,
        btn: () =>
          h(
            Button,
            {
              type: 'primary',
              size: 'small',
              onClick: () => {
                // antd-vue 的 destroy 类型不收 key 参数（实际实现也不支持按 key 关），
                // 点进报告这个时机把通知全清掉也合理。
                notification.destroy()
                openReportTab('comparison', detail.id)
              },
            },
            () => '查看报告',
          ),
      })
    } else {
      message.error(detail.error_msg || '对比分析失败')
    }
  } catch {
    stopWatchingComparison()
  }
}

function stopWatchingComparison() {
  if (watchTimer !== undefined) window.clearInterval(watchTimer)
  watchTimer = undefined
  watchedComparisonId = null
}

async function onRemoveComparison(record: ResumeComparisonItem) {
  try {
    await resumeApi.removeComparison(record.id)
    message.success('已删除')
    await loadComparisons()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 报告 --------------------------------------------------------------------

/** 「查看报告」统一新开一个浏览器标签页，整页展示（ReportView）。
 *  这里只负责拼地址；加载、下载、存网盘都在那个页面里。 */
function openReportTab(kind: 'resume' | 'comparison', id: number) {
  const href = router.resolve({ name: 'ReportView', params: { kind, id } }).href
  window.open(href, '_blank', 'noopener')
}

const openReport = (id: number) => openReportTab('resume', id)
const openComparisonReport = (id: number) => openReportTab('comparison', id)

// ---- 辅助 ---------------------------------------------------------------------

function goToSettings() {
  router.push('/system/settings')
}

function statusText(status: ResumeStatus | string): string {
  return (
    {
      uploaded: '未分析',
      analyzing: '分析中',
      ready: '已完成',
      error: '失败',
    }[status] || status
  )
}

function statusColor(status: ResumeStatus | string): string {
  return (
    {
      uploaded: 'default',
      analyzing: 'processing',
      ready: 'success',
      error: 'error',
    }[status] || 'default'
  )
}

// ---- 运行记录入口 -------------------------------------------------------------
// 列表上的状态标签就是那次运行翻译出来的，点它看当时的现场：模型收到了什么
// prompt、哪一步挂了、挂在哪。发起入口不在这儿——那是功能自己在发起时做的事。

const runsOpen = ref(false)
const runsSubject = ref<WorkflowRunSubject | null>(null)

function openRuns(subject: WorkflowRunSubject) {
  runsSubject.value = subject
  runsOpen.value = true
}

/** 抽屉里跑完一次（或重跑完一步）：这两张列表的状态都是由那次运行翻译出来的，跟着换。 */
async function onRunsChanged() {
  await Promise.all([loadResumes(), loadComparisons()])
  syncPolling()
}

/** 状态标签的悬浮说明：失败时先给原因，再告诉这点会开什么。 */
function statusHint(status: string, errorMsg: string | null): string {
  return status === 'error' && errorMsg ? `${errorMsg} · 点击查看运行记录` : '点击查看运行记录'
}

function openResumeRuns(record: ResumeItem) {
  openRuns({ kind: 'resume', ref: record.id, label: record.title })
}

function openComparisonRuns(record: ResumeComparisonItem) {
  openRuns({
    kind: 'resume-comparison',
    ref: record.id,
    label: record.title || `对比 #${record.id}`,
  })
}

onMounted(async () => {
  await Promise.all([loadReadiness(), loadDocs(), loadResumes(), loadComparisons()])
  syncPolling()
})

// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
// watchedComparisonId 刻意留着：回来时那份对比还没跑完就接着盯。
onDeactivated(() => {
  pollLoop.stop()
  if (watchTimer !== undefined) {
    window.clearInterval(watchTimer)
    watchTimer = undefined
  }
})

onActivated(() => {
  syncPolling()
  if (watchedComparisonId !== null && watchTimer === undefined) {
    watchTimer = window.setInterval(checkWatchedComparison, 2500)
    void checkWatchedComparison()
  }
})

onUnmounted(() => {
  pollLoop.stop()
  stopWatchingComparison()
})
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
.resume-tabs {
  margin-top: 4px;
}
/* .muted 用全局共享类（style.css）。 */
.cell-title {
  font-weight: 500;
}
.cell-sub {
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.upload-dragger {
  margin-bottom: 16px;
}
.upload-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}
.compare-tip {
  margin-bottom: 8px;
}
.compare-list {
  max-height: 200px;
  overflow-y: auto;
  margin-bottom: 12px;
}
.netdisk-icon {
  margin-left: 6px;
  color: var(--signal-text);
}
.compare-form {
  margin-top: 4px;
}
.compare-form :deep(.ant-form-item) {
  margin-bottom: 0;
}
.preview-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}
.preview-meta {
  margin-bottom: 12px;
  font-size: var(--fs-xs);
}
.preview-content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-sm);
  line-height: 1.7;
}
</style>
