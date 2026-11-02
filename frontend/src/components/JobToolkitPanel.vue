<template>
  <div>
    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="求职助手的六类生成都依赖对话模型。请先添加一个用途为「对话」的模型、通过连通性测试并设为默认。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

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

    <div class="history-title">生成记录</div>
    <a-table
      :columns="toolkitColumns"
      :data-source="toolkitTasks"
      :loading="loadingToolkit"
      row-key="id"
      size="middle"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'kind'">
          <a-tag color="cyan">{{ record.kind_label }}</a-tag>
        </template>
        <template v-else-if="column.key === 'title'">
          <span class="cell-title">{{ record.title }}</span>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tooltip :title="statusHint(record.status, record.error_msg)">
            <a-tag :color="statusColor(record.status)" class="run-tag" @click="openRuns(record)">
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
              <a-button size="small" type="link" danger :disabled="record.status === 'analyzing'">
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

    <!-- ================= 工具弹窗 ================= -->
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

    <!-- 状态标签点开的运行现场：这张列表的状态都是由那次运行翻译出来的。 -->
    <WorkflowRunsDrawer
      v-model:open="runsOpen"
      :workflow="null"
      :subject="runsSubject"
      @changed="onRunsChanged"
    />
  </div>
</template>

<script setup lang="ts">
// 求职助手：从「简历」页迁到「面试」页的独立面板。六类一次性生成与分析/对比共用同一条
// 执行链路，但入口在面试域——简历优化、JD 审计、面试准备本来就是求职过程里的事。
// 简历来源下拉要读已上传简历列表，所以面板自己拉一次 resumeApi.list()。
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, message } from 'ant-design-vue'
import {
  AuditOutlined,
  CompassOutlined,
  HighlightOutlined,
  LoadingOutlined,
  PayCircleOutlined,
  QuestionCircleOutlined,
  RocketOutlined,
} from '@ant-design/icons-vue'
import {
  resumeApi,
  settingsApi,
  type ResumeItem,
  type ResumeStatus,
  type ResumeToolkitCreate,
  type ResumeToolkitItem,
  type ToolkitKind,
  type WorkflowRunSubject,
} from '@/api'
import { fmtDateTime } from '@/utils/format'
import { errorText } from '@/utils/errors'
import { createPollLoop } from '@/utils/polling'
import WorkflowRunsDrawer from '@/components/WorkflowRunsDrawer.vue'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const router = useRouter()

// ---- 工具定义 -----------------------------------------------------------------
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

// ---- 状态 ---------------------------------------------------------------------

const chatReady = ref(true)
const resumes = ref<ResumeItem[]>([])
const toolkitTasks = ref<ResumeToolkitItem[]>([])
const loadingToolkit = ref(false)

const toolkitColumns = [
  { key: 'kind', title: '类型', dataIndex: 'kind_label', width: 130 },
  { key: 'title', title: '标题', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '发起时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 150 },
]

const resumeOptions = computed(() =>
  resumes.value.map((item) => ({ value: item.id, label: item.title })),
)

const toolModal = reactive({
  open: false,
  loading: false,
  def: null as ToolDef | null,
  resumeSource: 'id' as 'none' | 'id' | 'text' | 'background',
  resumeId: undefined as number | undefined,
  resumeText: '',
  fields: {} as Record<string, string>,
})

// ---- 加载与轮询 ---------------------------------------------------------------

// 生成是后台任务，这里用轮询跟随状态变化；没有进行中的任务就停表（退避与页面隐藏
// 暂停由 utils/polling.ts 统一处理）。
const pollLoop = createPollLoop(() => loadToolkit(), { shouldContinue: needsPolling })

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
  return toolkitTasks.value.some((item) => item.status === 'analyzing')
}

function syncPolling() {
  if (needsPolling()) pollLoop.start()
  else pollLoop.stop()
}

async function loadResumes() {
  try {
    const result = await resumeApi.list()
    resumes.value = result.items
  } catch {
    // 拦截器已弹提示；下拉留空时用户仍可选「粘贴简历文本」。
  }
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态接口失败时按可用处理，真正的报错会在触发生成时冒出来。
    chatReady.value = true
  }
}

// ---- 发起生成 -----------------------------------------------------------------

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

// ---- 报告 / 运行记录 -----------------------------------------------------------

/** 「查看报告」新开一个浏览器标签页，整页展示（ReportView）。这里只负责拼地址。 */
function openToolkitReport(id: number) {
  const href = router.resolve({ name: 'ReportView', params: { kind: 'toolkit', id } }).href
  window.open(href, '_blank', 'noopener')
}

const runsOpen = ref(false)
const runsSubject = ref<WorkflowRunSubject | null>(null)

function openRuns(record: ResumeToolkitItem) {
  runsSubject.value = { kind: 'resume-toolkit', ref: record.id, label: record.title }
  runsOpen.value = true
}

/** 抽屉里跑完一次（或重跑完一步）：列表状态就是那次运行翻译出来的，跟着换。 */
async function onRunsChanged() {
  await loadToolkit()
  syncPolling()
}

/** 状态标签的悬浮说明：失败时先给原因，再告诉这点会开什么。 */
function statusHint(status: string, errorMsg: string | null): string {
  return status === 'error' && errorMsg ? `${errorMsg} · 点击查看运行记录` : '点击查看运行记录'
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

function goToSettings() {
  router.push('/system/settings')
}

// ---- 生命周期 -------------------------------------------------------------------

onMounted(async () => {
  await Promise.all([loadReadiness(), loadResumes(), loadToolkit()])
  syncPolling()
})

// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
onDeactivated(() => pollLoop.stop())
onActivated(() => syncPolling())
onUnmounted(() => pollLoop.stop())
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
/* 工具卡片：静态卡片用发丝线，hover 才给信号色。 */
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
  font-size: var(--fs-3xl);
  color: var(--signal-text);
}
.tool-name {
  font-weight: 600;
  margin-top: 8px;
}
.tool-desc {
  color: var(--text-3);
  font-size: var(--fs-xs);
  margin-top: 4px;
  line-height: 1.5;
}
.history-title {
  font-weight: 600;
  margin-bottom: 12px;
}
.tool-modal-desc {
  color: var(--text-3);
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
.resume-source-control {
  width: 100%;
  margin-top: 8px;
}
.cell-title {
  font-weight: 500;
}
</style>
