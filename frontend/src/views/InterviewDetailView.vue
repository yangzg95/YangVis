<template>
  <div class="interview-page">
    <header class="interview-header">
      <div class="interview-title" :title="pageTitle">{{ pageTitle }}</div>
      <a-space :size="8">
        <a-button size="small" :disabled="!record" @click="openEdit">
          <EditOutlined /> 编辑
        </a-button>
        <a-popconfirm
          title="确认删除这场面试的记录？其中的问题与参考答案将一并删除。"
          ok-text="删除"
          cancel-text="取消"
          @confirm="onRemove"
        >
          <a-button size="small" danger :disabled="!record || deleted">
            <DeleteOutlined /> 删除
          </a-button>
        </a-popconfirm>
      </a-space>
    </header>

    <a-result v-if="loadError" status="warning" :title="loadError" />
    <a-result
      v-else-if="deleted"
      status="success"
      title="记录已删除"
      sub-title="这个标签页可以直接关闭了"
    />
    <div v-else-if="loading" class="page-loading">
      <a-spin />
    </div>

    <main v-else-if="record" class="interview-content">
      <!-- 窄屏单列：手机（<576px）上两列描述表每项只剩一百多像素，公司/岗位
           名称稍微长点就挤换行。 -->
      <a-descriptions :column="{ xs: 1, sm: 2 }" size="small" bordered>
        <a-descriptions-item label="公司">{{ record.company }}</a-descriptions-item>
        <a-descriptions-item label="岗位">{{ record.position }}</a-descriptions-item>
        <a-descriptions-item label="面试日期">{{ record.interview_date || '—' }}</a-descriptions-item>
        <a-descriptions-item label="轮次">{{ record.round || '—' }}</a-descriptions-item>
        <a-descriptions-item label="结果">
          <a-tag :color="resultMeta(record.result).color">{{ resultMeta(record.result).text }}</a-tag>
        </a-descriptions-item>
        <a-descriptions-item label="记录时间">{{ formatTime(record.created_at) }}</a-descriptions-item>
      </a-descriptions>

      <template v-if="record.notes">
        <div class="section-title">复盘备注</div>
        <div class="notes-text">{{ record.notes }}</div>
      </template>

      <div class="section-title questions-title">面试问题（{{ record.questions.length }}）</div>

      <!-- 添加问题 -->
      <div class="question-add">
        <a-textarea
          v-model:value="addForm.question"
          :rows="3"
          :maxlength="4000"
          placeholder="面试官问了什么？"
        />
        <a-textarea
          v-model:value="addForm.my_answer"
          :rows="4"
          :maxlength="20000"
          placeholder="你当时是怎么回答的？（可选，AI 生成参考答案时会结合点评）"
          class="question-add-answer"
        />
        <div class="question-add-actions">
          <a-button
            type="primary"
            :disabled="!addForm.question.trim()"
            :loading="addForm.loading"
            @click="submitAddQuestion"
          >
            添加问题
          </a-button>
        </div>
      </div>

      <a-empty
        v-if="!record.questions.length"
        :image="simpleEmpty"
        description="还没有记录问题，把面试官问的写下来吧"
      />

      <div v-for="(q, index) in record.questions" :key="q.qid" class="question-card">
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
              <a-textarea v-model:value="editing.question" :rows="3" :maxlength="4000" />
            </a-form-item>
            <a-form-item label="我的回答">
              <a-textarea v-model:value="editing.my_answer" :rows="4" :maxlength="20000" />
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
            v-if="q.ref_status === 'ready' && q.ref_answer && !isRefCollapsed(q.qid)"
            class="markdown ref-answer"
            v-html="renderMarkdown(q.ref_answer)"
          />
          <div class="ref-actions">
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
            <a-button
              v-if="q.ref_status === 'ready' && q.ref_answer"
              type="text"
              size="small"
              class="ref-toggle"
              @click="toggleRefCollapsed(q.qid)"
            >
              {{ isRefCollapsed(q.qid) ? '展开' : '收起' }}
              <DownOutlined v-if="isRefCollapsed(q.qid)" />
              <UpOutlined v-else />
            </a-button>
          </div>
        </div>
      </div>
    </main>

    <!-- ================= 编辑弹窗 ================= -->
    <a-modal
      v-model:open="editModal.open"
      title="编辑面试记录"
      :confirm-loading="editModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitEdit"
    >
      <a-form layout="vertical" class="edit-form">
        <a-form-item label="公司" required>
          <a-input v-model:value="editModal.company" :maxlength="128" />
        </a-form-item>
        <a-form-item label="岗位" required>
          <a-input v-model:value="editModal.position" :maxlength="128" />
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
          <a-textarea v-model:value="editModal.notes" :rows="4" :maxlength="20000" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Empty, message } from 'ant-design-vue'
import {
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  RobotOutlined,
  UpOutlined,
} from '@ant-design/icons-vue'
import {
  interviewApi,
  settingsApi,
  type InterviewDetail,
  type InterviewQuestion,
  type InterviewResult,
} from '@/api'
import { createMarkdown } from '@/utils/markdown'

/** 面试记录详情的整页展示：/office/interview/<id>，由列表页新开标签页打开。 */

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const md = createMarkdown()
const route = useRoute()
const recordId = Number(route.params.id)

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

// ---- 详情加载 -----------------------------------------------------------------

const record = ref<InterviewDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const deleted = ref(false)
const chatReady = ref(true)

const pageTitle = computed(() =>
  record.value ? `${record.value.company} · ${record.value.position}` : '面试详情',
)

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

async function loadDetail() {
  try {
    record.value = await interviewApi.detail(recordId)
    document.title = `${pageTitle.value} · 杨维斯`
  } catch (err) {
    loadError.value = errorText(err, '面试记录加载失败')
  } finally {
    loading.value = false
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

// ---- 元数据编辑 / 删除 -----------------------------------------------------------

const editModal = reactive({
  open: false,
  loading: false,
  company: '',
  position: '',
  interview_date: undefined as string | undefined,
  round: '',
  result: 'pending' as InterviewResult,
  notes: '',
})

function openEdit() {
  if (!record.value) return
  Object.assign(editModal, {
    open: true,
    company: record.value.company,
    position: record.value.position,
    interview_date: record.value.interview_date ?? undefined,
    round: record.value.round ?? '',
    result: record.value.result,
    notes: record.value.notes ?? '',
  })
}

async function submitEdit() {
  if (!editModal.company.trim() || !editModal.position.trim()) {
    message.warning('公司与岗位必填')
    return
  }
  editModal.loading = true
  try {
    record.value = await interviewApi.update(recordId, {
      company: editModal.company.trim(),
      position: editModal.position.trim(),
      interview_date: editModal.interview_date || undefined,
      round: editModal.round.trim() || undefined,
      result: editModal.result,
      notes: editModal.notes.trim() || undefined,
    })
    message.success('已保存')
    editModal.open = false
  } finally {
    editModal.loading = false
  }
}

async function onRemove() {
  await interviewApi.remove(recordId)
  deleted.value = true
}

// ---- 题目：添加 / 内联编辑 / 删除 -----------------------------------------------

const addForm = reactive({ question: '', my_answer: '', loading: false })

async function submitAddQuestion() {
  addForm.loading = true
  try {
    record.value = await interviewApi.addQuestion(recordId, {
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
  editing.loading = true
  try {
    record.value = await interviewApi.updateQuestion(recordId, editing.qid, {
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
  record.value = await interviewApi.removeQuestion(recordId, q.qid)
  message.success('已删除')
}

// ---- AI 参考答案与轮询 -----------------------------------------------------------

// 生成是后台任务：触发后接口立刻返回 analyzing 状态，这里用轮询跟随进度；
// 没有生成中的题目就停表。
let pollTimer: number | undefined

async function onGenerate(q: InterviewQuestion) {
  record.value = await interviewApi.generateAnswer(recordId, q.qid)
  syncPolling()
}

function hasAnalyzing(): boolean {
  return Boolean(record.value?.questions.some((q) => q.ref_status === 'analyzing'))
}

async function refreshDetail() {
  // 用户正在内联编辑某题时跳过这次刷新：整换会把没保存的草稿冲掉，
  // 定时器还在走，保存完下一轮自然补上。
  if (editing.qid) return
  record.value = await interviewApi.detail(recordId)
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

// 参考答案收起状态：按 qid 单独记账——生成中的轮询会整换 record，
// 状态挂在题目对象上的话每轮刷新都会被冲掉。
const refCollapsed = reactive<Record<string, boolean>>({})

function isRefCollapsed(qid: string): boolean {
  return Boolean(refCollapsed[qid])
}

function toggleRefCollapsed(qid: string) {
  refCollapsed[qid] = !refCollapsed[qid]
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

onMounted(async () => {
  await Promise.all([loadDetail(), loadReadiness()])
  syncPolling()
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<style scoped>
/* 整页外壳：与 ReportView 同一模式——新标签页打开，没有主布局的侧边栏。 */
.interview-page {
  max-width: 960px;
  margin: 0 auto;
  padding: 16px 24px 64px;
}
.interview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.interview-title {
  font-size: 18px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.page-loading {
  display: flex;
  justify-content: center;
  padding: 96px 0;
}
.section-title {
  font-weight: 600;
  margin-bottom: 12px;
}
.questions-title {
  margin-top: 20px;
}
.notes-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-3);
  font-size: 13px;
  line-height: 1.7;
  margin-bottom: 4px;
}
.section-title:not(.questions-title) {
  margin-top: 16px;
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
.ref-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 收起/展开钮贴右，弱化处理——它是浏览辅助，不是主操作。 */
.ref-toggle {
  margin-left: auto;
  color: var(--text-3);
}
.full-width {
  width: 100%;
}
.edit-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}

/* 长文输入框：默认可上下拖拽加高（AntD 没有关掉原生 resize，这里写明是
   怕被以后的全局样式误伤），最小高度也放宽，写长回答不用先拖。 */
.question-add :deep(textarea),
.question-edit :deep(textarea),
.edit-form :deep(textarea) {
  resize: vertical;
  min-height: 72px;
}

/* 手机端有限适配：收紧页边距和标题，内容本身（卡片、表单）本来就是单列
   流式布局，不用动。 */
@media (max-width: 768px) {
  .interview-page {
    padding: 12px 12px 48px;
  }
  .interview-title {
    font-size: 16px;
  }
  .question-card {
    padding: 10px 12px;
  }
}
</style>
