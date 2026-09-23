<template>
  <div>
    <div class="toolbar interview-toolbar">
      <span class="muted">
        共 {{ records.length }} 场面试<template v-if="keyword.trim()">，筛选出 {{ filteredRecords.length }} 场</template>
      </span>
      <a-space>
        <a-input
          v-model:value="keyword"
          allow-clear
          class="keyword-input"
          placeholder="按公司 / 岗位筛选"
        >
          <template #prefix><SearchOutlined /></template>
        </a-input>
        <a-button type="primary" @click="openCreate">
          <PlusOutlined /> 记录面试
        </a-button>
      </a-space>
    </div>

    <a-table
      :columns="columns"
      :data-source="filteredRecords"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'company'">
          <div class="cell-title">{{ record.company }}</div>
          <div class="cell-sub">{{ record.position }}</div>
        </template>
        <template v-else-if="column.key === 'interview_date'">
          <span class="muted">{{ record.interview_date || '—' }}</span>
        </template>
        <template v-else-if="column.key === 'round'">
          <span class="muted">{{ record.round || '—' }}</span>
        </template>
        <template v-else-if="column.key === 'result'">
          <a-tag :color="resultMeta(record.result).color">{{ resultMeta(record.result).text }}</a-tag>
        </template>
        <template v-else-if="column.key === 'question_count'">
          <span class="muted">{{ record.question_count }} 题</span>
        </template>
        <template v-else-if="column.key === 'created_at'">
          <span class="muted">{{ formatTime(record.created_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4">
            <a-button size="small" type="link" @click="openDetail(record)">详情</a-button>
            <a-button size="small" type="link" @click="openEdit(record)">编辑</a-button>
            <a-popconfirm
              title="确认删除这场面试的记录？其中的问题与参考答案将一并删除。"
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
        <a-empty :image="simpleEmpty" description="还没有面试记录，点右上角「记录面试」开始复盘" />
      </template>
    </a-table>

    <!-- ================= 新增 / 编辑弹窗 ================= -->
    <a-modal
      v-model:open="editModal.open"
      :title="editModal.id ? '编辑面试记录' : '记录一场面试'"
      :confirm-loading="editModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitEdit"
    >
      <a-form layout="vertical" class="edit-form">
        <a-form-item label="公司" required>
          <a-input v-model:value="editModal.company" :maxlength="128" placeholder="如：字节跳动" />
        </a-form-item>
        <a-form-item label="岗位" required>
          <a-input v-model:value="editModal.position" :maxlength="128" placeholder="如：高级后端工程师" />
        </a-form-item>
        <a-form-item label="面试日期">
          <a-date-picker
            v-model:value="editModal.interview_date"
            value-format="YYYY-MM-DD"
            class="full-width"
            placeholder="选择日期"
          />
        </a-form-item>
        <a-form-item label="轮次">
          <a-auto-complete
            v-model:value="editModal.round"
            :options="ROUND_OPTIONS"
            :maxlength="32"
            placeholder="如：一面，可自由填写"
            allow-clear
          />
        </a-form-item>
        <a-form-item label="结果">
          <a-select v-model:value="editModal.result" :options="RESULT_OPTIONS" />
        </a-form-item>
        <a-form-item label="复盘备注">
          <a-textarea
            v-model:value="editModal.notes"
            :rows="3"
            :maxlength="20000"
            placeholder="整场面试的感受、发挥得失、后续要做的事"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 详情抽屉 ================= -->
    <a-drawer v-model:open="drawer.open" :width="720" :title="drawerTitle">
      <div v-if="drawer.loading" class="drawer-loading">
        <a-spin />
      </div>
      <template v-else-if="drawer.record">
        <a-descriptions :column="2" size="small" bordered>
          <a-descriptions-item label="公司">{{ drawer.record.company }}</a-descriptions-item>
          <a-descriptions-item label="岗位">{{ drawer.record.position }}</a-descriptions-item>
          <a-descriptions-item label="面试日期">{{ drawer.record.interview_date || '—' }}</a-descriptions-item>
          <a-descriptions-item label="轮次">{{ drawer.record.round || '—' }}</a-descriptions-item>
          <a-descriptions-item label="结果">
            <a-tag :color="resultMeta(drawer.record.result).color">
              {{ resultMeta(drawer.record.result).text }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="记录时间">{{ formatTime(drawer.record.created_at) }}</a-descriptions-item>
        </a-descriptions>

        <template v-if="drawer.record.notes">
          <div class="drawer-section-title notes-title">复盘备注</div>
          <div class="notes-text">{{ drawer.record.notes }}</div>
        </template>

        <div class="drawer-section-title questions-title">
          面试问题（{{ drawer.record.questions.length }}）
        </div>

        <!-- 添加问题 -->
        <div class="question-add">
          <a-textarea
            v-model:value="addForm.question"
            :rows="2"
            :maxlength="4000"
            placeholder="面试官问了什么？"
          />
          <a-textarea
            v-model:value="addForm.my_answer"
            :rows="2"
            :maxlength="20000"
            placeholder="你当时是怎么回答的？（可选，AI 生成参考答案时会结合点评）"
            class="question-add-answer"
          />
          <div class="question-add-actions">
            <a-button
              type="primary"
              size="small"
              :disabled="!addForm.question.trim()"
              :loading="addForm.loading"
              @click="submitAddQuestion"
            >
              添加问题
            </a-button>
          </div>
        </div>

        <a-empty
          v-if="!drawer.record.questions.length"
          :image="simpleEmpty"
          description="还没有记录问题，把面试官问的写下来吧"
        />

        <div v-for="(q, index) in drawer.record.questions" :key="q.qid" class="question-card">
          <!-- 查看态 -->
          <template v-if="editing.qid !== q.qid">
            <div class="question-head">
              <span class="question-text">Q{{ index + 1 }} · {{ q.question }}</span>
              <a-space :size="0" class="question-head-actions">
                <a-button size="small" type="text" @click="startEditQuestion(q)">
                  <EditOutlined />
                </a-button>
                <a-popconfirm
                  title="删除这条问题？其参考答案将一并删除。"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="onRemoveQuestion(q)"
                >
                  <a-button size="small" type="text" danger :disabled="q.ref_status === 'analyzing'">
                    <DeleteOutlined />
                  </a-button>
                </a-popconfirm>
              </a-space>
            </div>
            <div class="question-field">
              <span class="field-label">我的回答</span>
              <span class="field-text" :class="{ muted: !q.my_answer }">{{ q.my_answer || '未填写' }}</span>
            </div>
            <div class="question-field">
              <span class="field-label">备注</span>
              <span class="field-text" :class="{ muted: !q.note }">{{ q.note || '—' }}</span>
            </div>
          </template>

          <!-- 编辑态 -->
          <div v-else class="question-edit">
            <a-form layout="vertical">
              <a-form-item label="问题" required>
                <a-textarea v-model:value="editing.question" :rows="2" :maxlength="4000" />
              </a-form-item>
              <a-form-item label="我的回答">
                <a-textarea v-model:value="editing.my_answer" :rows="3" :maxlength="20000" />
              </a-form-item>
              <a-form-item label="备注">
                <a-textarea v-model:value="editing.note" :rows="2" :maxlength="20000" />
              </a-form-item>
            </a-form>
            <a-space class="question-edit-actions">
              <a-button size="small" @click="editing.qid = ''">取消</a-button>
              <a-button
                type="primary"
                size="small"
                :disabled="!editing.question.trim()"
                :loading="editing.loading"
                @click="submitEditQuestion"
              >
                保存
              </a-button>
            </a-space>
          </div>

          <!-- AI 参考答案 -->
          <div class="ref-section">
            <a-alert
              v-if="q.ref_status === 'error'"
              type="error"
              show-icon
              class="ref-error"
              :message="q.ref_error || '生成失败，可重试'"
            />
            <div
              v-if="q.ref_status === 'ready' && q.ref_answer"
              class="markdown ref-answer"
              v-html="renderMarkdown(q.ref_answer)"
            />
            <a-button
              size="small"
              :loading="q.ref_status === 'analyzing'"
              :disabled="!chatReady"
              @click="onGenerate(q)"
            >
              <RobotOutlined />
              {{
                q.ref_status === 'analyzing'
                  ? '正在生成…'
                  : q.ref_status === 'ready'
                    ? '重新生成参考答案'
                    : '生成参考答案'
              }}
            </a-button>
          </div>
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { Empty, message } from 'ant-design-vue'
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import {
  interviewApi,
  type InterviewDetail,
  type InterviewItem,
  type InterviewQuestion,
  type InterviewResult,
} from '@/api'
import { createMarkdown } from '@/utils/markdown'

// AI 生成依赖对话模型：与简历页同一道门槛，由父页面传入（模板里直接用 chatReady）。
defineProps<{ chatReady: boolean }>()

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const md = createMarkdown()

const ROUND_OPTIONS = ['一面', '二面', '三面', 'HR面', '电话面', '笔试', '终面'].map((value) => ({
  value,
}))

const RESULT_META: Record<InterviewResult, { text: string; color: string }> = {
  pending: { text: '待定', color: 'gold' },
  passed: { text: '通过', color: 'green' },
  failed: { text: '未通过', color: 'red' },
  offer: { text: '已 offer', color: 'geekblue' },
}
const RESULT_OPTIONS = (Object.keys(RESULT_META) as InterviewResult[]).map((value) => ({
  value,
  label: RESULT_META[value].text,
}))

const columns = [
  { key: 'company', title: '公司 / 岗位', dataIndex: 'company', ellipsis: true },
  { key: 'interview_date', title: '面试日期', dataIndex: 'interview_date', width: 110 },
  { key: 'round', title: '轮次', dataIndex: 'round', width: 90 },
  { key: 'result', title: '结果', dataIndex: 'result', width: 100 },
  { key: 'question_count', title: '问题', dataIndex: 'question_count', width: 80 },
  { key: 'created_at', title: '记录时间', dataIndex: 'created_at', width: 170 },
  { key: 'actions', title: '操作', width: 200 },
]

// ---- 列表 -------------------------------------------------------------------

const records = ref<InterviewItem[]>([])
const loading = ref(false)
const keyword = ref('')

const filteredRecords = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return records.value
  return records.value.filter(
    (item) =>
      item.company.toLowerCase().includes(kw) || item.position.toLowerCase().includes(kw),
  )
})

async function loadRecords() {
  loading.value = true
  try {
    const result = await interviewApi.list()
    records.value = result.items
  } finally {
    loading.value = false
  }
}

// ---- 新增 / 编辑弹窗 -----------------------------------------------------------

const editModal = reactive({
  open: false,
  loading: false,
  id: 0,
  company: '',
  position: '',
  interview_date: undefined as string | undefined,
  round: '',
  result: 'pending' as InterviewResult,
  notes: '',
})

function openCreate() {
  Object.assign(editModal, {
    open: true,
    id: 0,
    company: '',
    position: '',
    interview_date: undefined,
    round: '',
    result: 'pending',
    notes: '',
  })
}

function openEdit(record: InterviewItem) {
  Object.assign(editModal, {
    open: true,
    id: record.id,
    company: record.company,
    position: record.position,
    interview_date: record.interview_date ?? undefined,
    round: record.round ?? '',
    result: record.result,
    notes: record.notes ?? '',
  })
}

async function submitEdit() {
  if (!editModal.company.trim() || !editModal.position.trim()) {
    message.warning('公司与岗位必填')
    return
  }
  editModal.loading = true
  try {
    const payload = {
      company: editModal.company.trim(),
      position: editModal.position.trim(),
      // 空串置 undefined 让后端按「未填写」处理；编辑时空日期/null 才会真的清空。
      interview_date: editModal.interview_date || undefined,
      round: editModal.round.trim() || undefined,
      result: editModal.result,
      notes: editModal.notes.trim() || undefined,
    }
    if (editModal.id) {
      await interviewApi.update(editModal.id, payload)
      message.success('已保存')
    } else {
      await interviewApi.create(payload)
      message.success('已记录')
    }
    editModal.open = false
    await loadRecords()
  } finally {
    editModal.loading = false
  }
}

async function onRemove(record: InterviewItem) {
  await interviewApi.remove(record.id)
  message.success('已删除')
  if (drawer.record?.id === record.id) {
    drawer.open = false
    drawer.record = null
  }
  await loadRecords()
}

// ---- 详情抽屉 -----------------------------------------------------------------

const drawer = reactive({
  open: false,
  loading: false,
  record: null as InterviewDetail | null,
})

const drawerTitle = computed(() =>
  drawer.record ? `${drawer.record.company} · ${drawer.record.position}` : '面试详情',
)

async function openDetail(record: InterviewItem) {
  drawer.open = true
  drawer.loading = true
  try {
    drawer.record = await interviewApi.detail(record.id)
  } finally {
    drawer.loading = false
  }
}

// ---- 题目：添加 / 内联编辑 / 删除 -----------------------------------------------

const addForm = reactive({ question: '', my_answer: '', loading: false })

async function submitAddQuestion() {
  if (!drawer.record) return
  addForm.loading = true
  try {
    drawer.record = await interviewApi.addQuestion(drawer.record.id, {
      question: addForm.question.trim(),
      my_answer: addForm.my_answer.trim() || undefined,
    })
    addForm.question = ''
    addForm.my_answer = ''
  } finally {
    addForm.loading = false
  }
}

const editing = reactive({
  qid: '',
  question: '',
  my_answer: '',
  note: '',
  loading: false,
})

function startEditQuestion(q: InterviewQuestion) {
  Object.assign(editing, {
    qid: q.qid,
    question: q.question,
    my_answer: q.my_answer ?? '',
    note: q.note ?? '',
    loading: false,
  })
}

async function submitEditQuestion() {
  if (!drawer.record) return
  editing.loading = true
  try {
    drawer.record = await interviewApi.updateQuestion(drawer.record.id, editing.qid, {
      question: editing.question.trim(),
      my_answer: editing.my_answer.trim() || undefined,
      note: editing.note.trim() || undefined,
    })
    editing.qid = ''
  } finally {
    editing.loading = false
  }
}

async function onRemoveQuestion(q: InterviewQuestion) {
  if (!drawer.record) return
  drawer.record = await interviewApi.removeQuestion(drawer.record.id, q.qid)
  message.success('已删除')
}

// ---- AI 参考答案与轮询 -----------------------------------------------------------

// 生成是后台任务：触发后接口立刻返回 analyzing 状态，这里用轮询跟随进度；
// 没有生成中的题目就停表。
let pollTimer: number | undefined

async function onGenerate(q: InterviewQuestion) {
  if (!drawer.record) return
  drawer.record = await interviewApi.generateAnswer(drawer.record.id, q.qid)
  syncPolling()
}

function hasAnalyzing(): boolean {
  return Boolean(
    drawer.open && drawer.record?.questions.some((q) => q.ref_status === 'analyzing'),
  )
}

async function refreshDetail() {
  if (!drawer.record) return
  // 用户正在内联编辑某题时跳过这次刷新：整换会把没保存的草稿冲掉，
  // 定时器还在走，保存完下一轮自然补上。
  if (editing.qid) return
  drawer.record = await interviewApi.detail(drawer.record.id)
  syncPolling()
}

function syncPolling() {
  if (hasAnalyzing() && pollTimer === undefined) {
    pollTimer = window.setInterval(refreshDetail, 2500)
  } else if (!hasAnalyzing() && pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

// ---- 展示辅助 -----------------------------------------------------------------

function resultMeta(result: InterviewResult): { text: string; color: string } {
  return RESULT_META[result] ?? { text: result, color: 'default' }
}

function renderMarkdown(text: string): string {
  return md.render(text)
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

// ---- 生命周期 -----------------------------------------------------------------

onMounted(loadRecords)

// 这个组件活在 keep-alive 的简历页里：切去别的标签页时停表，回来时再按
// 当前状态恢复（抽屉可能还开着、题目可能还在生成）。
onDeactivated(stopPolling)
onActivated(syncPolling)
onUnmounted(stopPolling)
</script>

<style scoped>
/* flex 基底用全局 .toolbar（style.css），这里只留本组件差异。 */
.interview-toolbar {
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
  font-size: 12px;
}
.full-width {
  width: 100%;
}
.edit-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}
.drawer-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}
.drawer-section-title {
  font-weight: 600;
  margin-bottom: 12px;
}
.notes-title {
  margin-top: 16px;
}
.notes-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-3);
  font-size: 13px;
  line-height: 1.7;
}
.questions-title {
  margin-top: 20px;
}
.question-add {
  border: 1px dashed var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 16px;
}
.question-add-answer {
  margin-top: 8px;
}
.question-add-actions {
  margin-top: 8px;
  text-align: right;
}
.question-card {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 12px;
}
.question-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.question-text {
  font-weight: 500;
  line-height: 1.6;
}
.question-head-actions {
  flex-shrink: 0;
}
.question-field {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.7;
}
.field-label {
  flex-shrink: 0;
  color: var(--text-3);
}
.field-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.question-edit-actions {
  display: flex;
  justify-content: flex-end;
}
.ref-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--hairline);
}
.ref-error {
  margin-bottom: 8px;
}
.ref-answer {
  margin-bottom: 12px;
  font-size: 13px;
}
</style>
