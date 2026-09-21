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

    <a-tabs v-model:activeKey="activeTab" class="resume-tabs">
      <!-- ================= 简历列表 ================= -->
      <a-tab-pane key="resumes" tab="简历列表">
        <div class="toolbar">
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
              <a-tooltip :title="record.status === 'error' ? record.error_msg : ''">
                <a-tag :color="statusColor(record.status)">
                  <LoadingOutlined v-if="record.status === 'analyzing'" />
                  {{ statusText(record.status) }}
                </a-tag>
              </a-tooltip>
              <a-tooltip v-if="record.has_netdisk" title="原件已同步到百度网盘">
                <CloudOutlined class="netdisk-icon" />
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ formatTime(record.created_at) }}</span>
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
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="cell-title">{{ record.title || `对比 #${record.id}` }}</div>
              <div class="cell-sub">{{ record.resume_titles.join(' vs ') }}</div>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tooltip :title="record.status === 'error' ? record.error_msg : ''">
                <a-tag :color="statusColor(record.status)">
                  <LoadingOutlined v-if="record.status === 'analyzing'" />
                  {{ statusText(record.status) }}
                </a-tag>
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ formatTime(record.created_at) }}</span>
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

      <!-- ================= 求职助手 ================= -->
      <a-tab-pane key="toolkit" tab="求职助手">
        <div class="tool-grid">
          <div
            v-for="tool in TOOL_DEFS"
            :key="tool.kind"
            class="tool-card"
            :class="{ 'tool-card-disabled': !chatReady }"
            @click="openTool(tool)"
          >
            <component :is="tool.icon" class="tool-icon" />
            <div class="tool-name">{{ tool.name }}</div>
            <div class="tool-desc">{{ tool.desc }}</div>
          </div>
        </div>

        <div class="drawer-section-title toolkit-history-title">生成记录</div>
        <a-table
          :columns="toolkitColumns"
          :data-source="toolkitTasks"
          :loading="loadingToolkit"
          row-key="id"
          size="middle"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'kind'">
              <a-tag color="cyan">{{ record.kind_label }}</a-tag>
            </template>
            <template v-else-if="column.key === 'title'">
              <span class="cell-title">{{ record.title }}</span>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tooltip :title="record.status === 'error' ? record.error_msg : ''">
                <a-tag :color="statusColor(record.status)">
                  <LoadingOutlined v-if="record.status === 'analyzing'" />
                  {{ statusText(record.status) }}
                </a-tag>
              </a-tooltip>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ formatTime(record.created_at) }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space :size="4">
                <a-button
                  size="small"
                  type="link"
                  :disabled="record.status !== 'ready'"
                  @click="openToolkitReport(record.id)"
                >
                  查看报告
                </a-button>
                <a-popconfirm
                  title="确认删除这条生成记录？"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="onRemoveToolkit(record)"
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
            <a-empty :image="simpleEmpty" description="还没有生成记录，点上方卡片发起一次" />
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

    <!-- ================= 求职助手工具弹窗 ================= -->
    <a-modal
      v-model:open="toolModal.open"
      :title="toolModal.def ? `求职助手 · ${toolModal.def.name}` : ''"
      :confirm-loading="toolModal.loading"
      :ok-button-props="{ disabled: !toolFormValid }"
      ok-text="开始生成"
      cancel-text="取消"
      @ok="submitTool"
    >
      <template v-if="toolModal.def">
        <p class="tool-modal-desc">{{ toolModal.def.desc }}</p>
        <a-form layout="vertical">
          <a-form-item
            v-if="toolModal.def.resumeSource"
            :label="
              toolModal.def.resumeSource === 'optional'
                ? '简历（可选，附上后生成内容会贴合你的真实经历）'
                : '简历'
            "
            :required="toolModal.def.resumeSource !== 'optional'"
          >
            <a-radio-group v-model:value="toolModal.resumeSource">
              <a-radio v-if="toolModal.def.resumeSource === 'optional'" value="none">
                不附带简历
              </a-radio>
              <a-radio value="id">选择已上传简历</a-radio>
              <a-radio value="text">粘贴简历文本</a-radio>
              <a-radio v-if="toolModal.def.resumeSource === 'background-or'" value="background">
                直接描述我的背景
              </a-radio>
            </a-radio-group>
            <a-select
              v-if="toolModal.resumeSource === 'id'"
              v-model:value="toolModal.resumeId"
              :options="resumeOptions"
              placeholder="选择一份简历"
              show-search
              option-filter-prop="label"
              class="resume-source-control"
            />
            <a-textarea
              v-else-if="toolModal.resumeSource === 'text'"
              v-model:value="toolModal.resumeText"
              :rows="6"
              :maxlength="20000"
              placeholder="粘贴你的简历全文"
              class="resume-source-control"
            />
            <a-textarea
              v-else-if="toolModal.resumeSource === 'background'"
              v-model:value="toolModal.fields.background"
              :rows="6"
              :maxlength="20000"
              placeholder="描述你的教育背景、工作经历、技能与项目经验"
              class="resume-source-control"
            />
          </a-form-item>
          <a-form-item
            v-for="field in toolModal.def.fields"
            :key="field.key"
            :label="field.label"
            :required="field.required"
          >
            <a-textarea
              v-if="field.textarea"
              v-model:value="toolModal.fields[field.key]"
              :rows="field.rows || 4"
              :maxlength="field.maxlength || 2000"
              :placeholder="field.placeholder"
            />
            <a-input
              v-else
              v-model:value="toolModal.fields[field.key]"
              :maxlength="field.maxlength || 128"
              :placeholder="field.placeholder"
            />
          </a-form-item>
        </a-form>
      </template>
    </a-modal>

    <!-- ================= 预览抽屉 ================= -->
    <a-drawer
      v-model:open="previewDrawer.open"
      :width="720"
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
  </div>
</template>

<script setup lang="ts">
import { computed, h, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Empty, message, notification } from 'ant-design-vue'
import {
  AuditOutlined,
  CloudOutlined,
  CompassOutlined,
  HighlightOutlined,
  InboxOutlined,
  LoadingOutlined,
  PayCircleOutlined,
  QuestionCircleOutlined,
  RocketOutlined,
  SwapOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import type { UploadFile } from 'ant-design-vue'
import {
  resumeApi,
  settingsApi,
  type ResumeComparisonItem,
  type ResumeItem,
  type ResumeStatus,
  type ResumeToolkitCreate,
  type ResumeToolkitItem,
  type ToolkitKind,
} from '@/api'
import { saveBlobResponse } from '@/utils/download'
import { fmtSize } from '@/utils/format'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const router = useRouter()

// ---- 求职助手工具定义 ----------------------------------------------------------
// 声明式配置：新增工具只动这张表与后端 TOOLKIT_KINDS，弹窗表单按字段自动渲染。

interface ToolFieldDef {
  key: 'position' | 'job_description' | 'background' | 'offer_amount' | 'notes'
  label: string
  placeholder?: string
  textarea?: boolean
  rows?: number
  required?: boolean
  maxlength?: number
}

interface ToolDef {
  kind: ToolkitKind
  name: string
  desc: string
  icon: typeof HighlightOutlined
  /**
   * 简历来源控件：null=不出现；required=简历必选；background-or=简历或背景描述
   * 三选一；optional=可附带也可不附带。
   */
  resumeSource: 'required' | 'background-or' | 'optional' | null
  fields: ToolFieldDef[]
}

const TOOL_DEFS: ToolDef[] = [
  {
    kind: 'optimize',
    name: '简历优化',
    desc: '把简历从「描述职责」重写为「展示成果」：量化数字、行动动词、ATS 友好格式一步到位',
    icon: HighlightOutlined,
    resumeSource: 'required',
    fields: [
      {
        key: 'position',
        label: '目标岗位（可选）',
        placeholder: '如：高级前端工程师，填写后优化会向该岗位靠拢',
      },
    ],
  },
  {
    kind: 'career-match',
    name: '职业匹配分析',
    desc: '找出你「配得上但没想到」的 10 个高薪职位方向，按薪资潜力与市场需求排名',
    icon: CompassOutlined,
    resumeSource: 'background-or',
    fields: [],
  },
  {
    kind: 'jd-match',
    name: '简历匹配审计',
    desc: '对照职位描述找出缺失关键词，重写简历让 ATS 匹配度达到 90% 以上',
    icon: AuditOutlined,
    resumeSource: 'required',
    fields: [
      {
        key: 'job_description',
        label: '职位描述（JD）',
        textarea: true,
        rows: 6,
        required: true,
        maxlength: 20000,
        placeholder: '粘贴招聘信息全文',
      },
    ],
  },
  {
    kind: 'interview-prep',
    name: '面试准备策略',
    desc: '按岗位生成 15 个真实面试题与自信清晰的模范答案，提前练好再上考场',
    icon: QuestionCircleOutlined,
    resumeSource: 'optional',
    fields: [
      { key: 'position', label: '岗位名称', required: true, placeholder: '如：高级前端工程师' },
      {
        key: 'job_description',
        label: '职位描述（JD，可选）',
        textarea: true,
        rows: 5,
        maxlength: 20000,
        placeholder: '粘贴招聘信息全文，面试题会紧扣 JD 里的具体要求',
      },
    ],
  },
  {
    kind: 'portfolio-plan',
    name: '证明构建计划',
    desc: '3 个本周就能完成的作品集项目创意，让简历立刻有东西可写',
    icon: RocketOutlined,
    resumeSource: 'optional',
    fields: [
      { key: 'position', label: '目标职位', required: true, placeholder: '如：数据分析师' },
      {
        key: 'background',
        label: '个人背景补充（可选）',
        textarea: true,
        rows: 3,
        placeholder: '想重点展示的技能或方向',
      },
    ],
  },
  {
    kind: 'salary-negotiation',
    name: '薪资最大化框架',
    desc: '礼貌但有说服力的谈薪话术，温柔而坚定地多拿 15-20%',
    icon: PayCircleOutlined,
    resumeSource: null,
    fields: [
      { key: 'offer_amount', label: 'offer 年薪', required: true, placeholder: '如：30万' },
      { key: 'position', label: '岗位（可选）', placeholder: '如：高级前端工程师' },
      {
        key: 'notes',
        label: '补充说明（可选）',
        textarea: true,
        rows: 3,
        placeholder: '如：手里还有其他 offer、目前的薪资水平',
      },
    ],
  },
]

// ---- 状态 -------------------------------------------------------------------

const chatReady = ref(true)
const activeTab = ref('resumes')

const resumes = ref<ResumeItem[]>([])
const loadingResumes = ref(false)
const selectedIds = ref<number[]>([])

const comparisons = ref<ResumeComparisonItem[]>([])
const loadingComparisons = ref(false)

const toolkitTasks = ref<ResumeToolkitItem[]>([])
const loadingToolkit = ref(false)

const toolModal = reactive({
  open: false,
  loading: false,
  def: null as ToolDef | null,
  resumeSource: 'id' as 'none' | 'id' | 'text' | 'background',
  resumeId: undefined as number | undefined,
  resumeText: '',
  fields: {} as Record<string, string>,
})

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

const toolkitColumns = [
  { key: 'kind', title: '类型', dataIndex: 'kind_label', width: 130 },
  { key: 'title', title: '标题', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '发起时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 150 },
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
let pollTimer: number | undefined

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

async function loadToolkit() {
  loadingToolkit.value = true
  try {
    const result = await resumeApi.listToolkitTasks()
    toolkitTasks.value = result.items
  } finally {
    loadingToolkit.value = false
  }
}

function needsPolling(): boolean {
  return (
    resumes.value.some((item) => item.status === 'analyzing') ||
    comparisons.value.some((item) => item.status === 'analyzing') ||
    toolkitTasks.value.some((item) => item.status === 'analyzing')
  )
}

function syncPolling() {
  if (needsPolling() && pollTimer === undefined) {
    pollTimer = window.setInterval(async () => {
      await Promise.all([loadResumes(), loadComparisons(), loadToolkit()])
      if (!needsPolling()) {
        window.clearInterval(pollTimer)
        pollTimer = undefined
      }
    }, 2500)
  }
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
// 绑定/解绑统一在「通用设置 → 百度网盘」里做；这里只消费行上的 has_netdisk 标记。

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

// ---- 求职助手 -----------------------------------------------------------------

const resumeOptions = computed(() =>
  resumes.value.map((item) => ({ value: item.id, label: item.title })),
)

function openTool(def: ToolDef) {
  if (!chatReady.value) return
  toolModal.def = def
  toolModal.resumeSource =
    def.resumeSource === 'optional' ? 'none' : resumes.value.length ? 'id' : 'text'
  toolModal.resumeId = undefined
  toolModal.resumeText = ''
  toolModal.fields = {}
  toolModal.open = true
}

// 与后端 TOOLKIT_KINDS 的必填规则保持一致，先在前端拦一道。
const toolFormValid = computed(() => {
  const def = toolModal.def
  if (!def) return false
  for (const field of def.fields) {
    if (field.required && !(toolModal.fields[field.key] || '').trim()) return false
  }
  if (def.resumeSource === 'required') {
    if (toolModal.resumeSource === 'id') return toolModal.resumeId !== undefined
    return !!toolModal.resumeText.trim()
  }
  if (def.resumeSource === 'background-or') {
    if (toolModal.resumeSource === 'id') return toolModal.resumeId !== undefined
    if (toolModal.resumeSource === 'text') return !!toolModal.resumeText.trim()
    return !!(toolModal.fields.background || '').trim()
  }
  return true
})

async function submitTool() {
  const def = toolModal.def
  if (!def || !toolFormValid.value) return

  const payload: ResumeToolkitCreate = { kind: def.kind }
  if (def.resumeSource && toolModal.resumeSource === 'id' && toolModal.resumeId !== undefined) {
    payload.resume_id = toolModal.resumeId
  } else if (def.resumeSource && toolModal.resumeSource === 'text') {
    payload.resume_text = toolModal.resumeText.trim()
  } else if (def.resumeSource === 'background-or' && toolModal.resumeSource === 'background') {
    payload.background = (toolModal.fields.background || '').trim()
  }
  for (const field of def.fields) {
    const value = (toolModal.fields[field.key] || '').trim()
    if (value) payload[field.key] = value
  }

  toolModal.loading = true
  try {
    await resumeApi.createToolkitTask(payload)
    message.success('已开始生成，完成后可在下方「生成记录」中查看')
    toolModal.open = false
    await loadToolkit()
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '生成发起失败'))
  } finally {
    toolModal.loading = false
  }
}

async function onRemoveToolkit(record: ResumeToolkitItem) {
  try {
    await resumeApi.removeToolkitTask(record.id)
    message.success('已删除')
    await loadToolkit()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 报告 --------------------------------------------------------------------

/** 「查看报告」统一新开一个浏览器标签页，整页展示（ReportView）。
 *  这里只负责拼地址；加载、下载、存网盘都在那个页面里。 */
function openReportTab(kind: 'resume' | 'comparison' | 'toolkit', id: number) {
  const href = router.resolve({ name: 'ReportView', params: { kind, id } }).href
  window.open(href, '_blank', 'noopener')
}

const openReport = (id: number) => openReportTab('resume', id)
const openComparisonReport = (id: number) => openReportTab('comparison', id)
const openToolkitReport = (id: number) => openReportTab('toolkit', id)

// ---- 辅助 ---------------------------------------------------------------------

function goToSettings() {
  router.push('/settings')
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

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

onMounted(async () => {
  await Promise.all([loadReadiness(), loadResumes(), loadComparisons(), loadToolkit()])
  syncPolling()
})

// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
// watchedComparisonId 刻意留着：回来时那份对比还没跑完就接着盯。
onDeactivated(() => {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
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
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
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
.toolbar {
  margin: 4px 0 16px;
  justify-content: space-between;
}
/* .muted 用全局共享类（style.css）。 */
.cell-title {
  font-weight: 500;
}
.cell-sub {
  color: var(--text-3);
  font-size: 12px;
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
  color: #0a7e86;
}
.compare-form {
  margin-top: 4px;
}
.compare-form :deep(.ant-form-item) {
  margin-bottom: 0;
}
.drawer-section-title {
  font-weight: 600;
  margin-bottom: 12px;
}
/* 求职助手工具卡片：静态卡片用发丝线，hover 才给信号色。 */
.tool-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
  margin: 4px 0 20px;
}
.tool-card {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 16px;
  cursor: pointer;
  transition:
    border-color 0.2s,
    background-color 0.2s;
}
.tool-card:hover {
  border-color: var(--signal-border);
  background: var(--signal-bg);
}
.tool-card-disabled,
.tool-card-disabled:hover {
  opacity: 0.5;
  cursor: not-allowed;
  border-color: var(--hairline);
  background: transparent;
}
.tool-icon {
  font-size: 22px;
  color: var(--signal-text);
}
.tool-name {
  font-weight: 600;
  margin-top: 8px;
}
.tool-desc {
  color: var(--text-3);
  font-size: 12px;
  margin-top: 4px;
  line-height: 1.5;
}
.toolkit-history-title {
  margin-bottom: 12px;
}
.tool-modal-desc {
  color: var(--text-3);
  font-size: 13px;
  margin-bottom: 16px;
}
.resume-source-control {
  width: 100%;
  margin-top: 8px;
}
.preview-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}
.preview-meta {
  margin-bottom: 12px;
  font-size: 12px;
}
.preview-content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.7;
}
</style>
