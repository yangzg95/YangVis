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
      :scroll="{ x: 'max-content' }"
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
            <a-button size="small" type="link" @click="openDetailTab(record)">详情</a-button>
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
            :rows="4"
            :maxlength="20000"
            placeholder="整场面试的感受、发挥得失、后续要做的事"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 面试记录列表：简历页第 4 个 tab。问题明细与 AI 参考答案都在整页详情
// （/office/interview/<id>，views/InterviewDetailView.vue）里处理，这里只管场次元数据。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, message } from 'ant-design-vue'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { interviewApi, type InterviewItem, type InterviewResult } from '@/api'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const router = useRouter()

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

// ---- 详情：新开标签页进整页（同报告页的模式） -------------------------------------

function openDetailTab(record: InterviewItem) {
  const href = router.resolve({ name: 'InterviewDetail', params: { id: record.id } }).href
  window.open(href, '_blank', 'noopener')
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
  try {
    await interviewApi.remove(record.id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadRecords()
}

// ---- 展示辅助 -----------------------------------------------------------------

function resultMeta(result: InterviewResult): { text: string; color: string } {
  return RESULT_META[result] ?? { text: result, color: 'default' }
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

// ---- 生命周期 -----------------------------------------------------------------

onMounted(loadRecords)
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
</style>
