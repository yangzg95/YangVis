<template>
  <div class="page-card jobs-page">
    <div class="jobs-heading">
      <div>
        <div class="page-title">岗位</div>
        <div class="subtitle">按条件抓取并缓存 BOSS 直聘岗位，PC 与 App 共用同一份列表。</div>
      </div>
      <span class="source-badge">BOSS 直聘</span>
    </div>

    <div class="job-filters" aria-label="岗位筛选">
      <a-input
        v-model:value="filters.keyword"
        allow-clear
        placeholder="岗位或公司关键词"
        :maxlength="100"
        @press-enter="onSearch"
      >
        <template #prefix><SearchOutlined class="filter-icon" /></template>
      </a-input>
      <a-select
        v-model:value="filters.city"
        :options="cityOptions"
        aria-label="城市"
      />
      <a-select
        v-model:value="filters.postedDays"
        :options="postedOptions"
        allow-clear
        placeholder="发布时间"
        aria-label="发布时间"
      />
      <a-input-number
        v-model:value="filters.salaryMin"
        :min="0"
        :max="1000"
        :precision="1"
        placeholder="最低 K/月"
        aria-label="最低月薪 K"
      />
      <a-input-number
        v-model:value="filters.salaryMax"
        :min="0"
        :max="1000"
        :precision="1"
        placeholder="最高 K/月"
        aria-label="最高月薪 K"
      />
      <div class="filter-actions">
        <a-button :loading="loading" @click="onSearch">查询</a-button>
        <a-button type="primary" :loading="fetching" @click="onFetch">
          <SyncOutlined /> 抓取最新岗位
        </a-button>
      </div>
    </div>
    <div class="filter-note">薪资单位为 K/月；发布时间仅采用 BOSS 明确返回的岗位发布时间。</div>

    <a-alert
      v-if="fetchError"
      type="warning"
      show-icon
      class="job-alert"
      message="本次抓取未完成"
      :description="`${fetchError}。已有缓存不受影响。`"
    >
      <template #action><a-button size="small" @click="onFetch">重试</a-button></template>
    </a-alert>
    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      class="job-alert"
      message="岗位列表加载失败"
      :description="loadError"
    >
      <template #action><a-button size="small" danger @click="loadJobs">重试</a-button></template>
    </a-alert>

    <a-spin :spinning="fetching || (loading && jobs.length > 0)">
      <div v-if="loading && !jobs.length" class="job-list">
        <div v-for="i in 3" :key="i" class="job-card">
          <a-skeleton active :title="{ width: '35%' }" :paragraph="{ rows: 3 }" />
        </div>
      </div>
      <template v-else>
        <div v-if="jobs.length" class="job-list">
          <article v-for="job in jobs" :key="job.id" class="job-card">
            <div class="job-main">
              <div class="job-title-row">
                <h2>{{ job.title }}</h2>
                <strong>{{ job.salary_text }}</strong>
              </div>
              <div class="job-company">{{ job.company }}</div>
              <div class="job-meta">
                <span><EnvironmentOutlined /> {{ locationText(job) }}</span>
                <span v-if="job.experience">{{ job.experience }}</span>
                <span v-if="job.education">{{ job.education }}</span>
              </div>
              <div v-if="job.skills.length" class="job-tags">
                <a-tag v-for="skill in job.skills" :key="skill">{{ skill }}</a-tag>
              </div>
            </div>
            <div class="job-side">
              <div class="job-publish" :title="firstSeenText(job)">
                <ClockCircleOutlined /> {{ publishedText(job) }}
              </div>
              <div v-if="job.recruiter" class="job-recruiter">{{ job.recruiter }}</div>
              <a-button
                type="primary"
                size="small"
                :href="job.apply_url"
                target="_blank"
                rel="noopener noreferrer"
              >
                去投递 <ExportOutlined />
              </a-button>
            </div>
          </article>
        </div>
        <a-empty v-else class="job-empty" :description="emptyText">
          <a-button type="primary" :loading="fetching" @click="onFetch">抓取岗位</a-button>
        </a-empty>
      </template>
    </a-spin>

    <div v-if="total" class="job-pagination">
      <span class="muted">共 {{ total }} 条缓存岗位</span>
      <a-pagination
        :current="page"
        :page-size="pageSize"
        :total="total"
        :show-size-changer="true"
        :page-size-options="['10', '20', '50']"
        size="small"
        @change="onPageChange"
      />
    </div>
    <div class="job-disclaimer">
      岗位信息以 BOSS 直聘官方页面为准。“去投递”将打开新标签页，登录与投递在官方站点完成。
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ClockCircleOutlined,
  EnvironmentOutlined,
  ExportOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons-vue'
import { JOB_CITIES, jobsApi, type JobItem, type JobQuery } from '@/api'
import { errorText } from '@/utils/errors'

const jobs = ref<JobItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const fetching = ref(false)
const loadError = ref('')
const fetchError = ref('')
let requestSeq = 0

const filters = reactive({
  keyword: '',
  city: '全国',
  postedDays: undefined as 1 | 3 | 7 | 30 | undefined,
  salaryMin: null as number | null,
  salaryMax: null as number | null,
})

const cityOptions = JOB_CITIES.map((city) => ({ label: city, value: city }))
const postedOptions = [
  { label: '近 1 天', value: 1 },
  { label: '近 3 天', value: 3 },
  { label: '近 7 天', value: 7 },
  { label: '近 30 天', value: 30 },
]
const hasFilters = computed(
  () =>
    Boolean(filters.keyword.trim()) ||
    filters.city !== '全国' ||
    filters.postedDays !== undefined ||
    filters.salaryMin !== null ||
    filters.salaryMax !== null,
)
const emptyText = computed(() =>
  hasFilters.value ? '没有符合当前条件的缓存岗位' : '还没有岗位，先设置条件并抓取',
)

function queryPayload(): JobQuery {
  return {
    keyword: filters.keyword.trim() || undefined,
    city: filters.city,
    posted_days: filters.postedDays,
    salary_min: filters.salaryMin ?? undefined,
    salary_max: filters.salaryMax ?? undefined,
  }
}

function validSalaryRange(): boolean {
  if (
    filters.salaryMin !== null &&
    filters.salaryMax !== null &&
    filters.salaryMin > filters.salaryMax
  ) {
    message.warning('最低薪资不能高于最高薪资')
    return false
  }
  return true
}

async function loadJobs() {
  const seq = ++requestSeq
  loading.value = true
  loadError.value = ''
  try {
    const data = await jobsApi.list(queryPayload(), page.value, pageSize.value)
    if (seq !== requestSeq) return
    jobs.value = data.items
    total.value = data.total
  } catch (err) {
    if (seq === requestSeq) loadError.value = errorText(err, '请检查网络后重试')
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function onSearch() {
  if (!validSalaryRange()) return
  page.value = 1
  void loadJobs()
}

async function onFetch() {
  if (fetching.value || !validSalaryRange()) return
  fetching.value = true
  fetchError.value = ''
  try {
    const result = await jobsApi.fetch(queryPayload())
    message.success(
      `抓取完成：获取 ${result.fetched} 条，符合 ${result.matched} 条，新增 ${result.created} 条，更新 ${result.updated} 条`,
    )
    page.value = 1
    await loadJobs()
  } catch (err) {
    fetchError.value = errorText(err, '请稍后重试')
  } finally {
    fetching.value = false
  }
}

function onPageChange(nextPage: number, nextPageSize: number) {
  page.value = nextPageSize === pageSize.value ? nextPage : 1
  pageSize.value = nextPageSize
  void loadJobs()
}

function locationText(job: JobItem): string {
  return [job.city, job.district].filter(Boolean).join(' · ')
}

function publishedText(job: JobItem): string {
  if (!job.published_at) return '发布时间未知'
  return `发布于 ${new Date(job.published_at).toLocaleDateString('zh-CN')}`
}

function firstSeenText(job: JobItem): string {
  return `本系统首次抓取：${new Date(job.first_seen_at).toLocaleString('zh-CN')}`
}

onMounted(loadJobs)
</script>

<style scoped>
.jobs-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.source-badge {
  flex: none;
  padding: 5px 10px;
  border: 1px solid var(--signal-border);
  border-radius: 6px;
  color: var(--signal-text);
  background: var(--signal-bg);
  font-size: 12px;
  font-weight: 600;
}

.job-filters {
  display: grid;
  grid-template-columns: minmax(180px, 1.4fr) 116px 132px 124px 124px auto;
  gap: 10px;
  margin-top: 22px;
}

.job-filters :deep(.ant-input-number) {
  width: 100%;
}

.filter-icon {
  color: var(--text-3);
}

.filter-actions {
  display: flex;
  gap: 8px;
  white-space: nowrap;
}

.filter-note {
  margin: 8px 0 18px;
  color: var(--text-3);
  font-size: 12px;
}

.job-alert {
  margin-bottom: 14px;
}

.job-list {
  display: grid;
  gap: 12px;
}

.job-card {
  display: flex;
  gap: 24px;
  padding: 18px 20px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--surface);
  transition: border-color 0.16s ease;
}

.job-card:hover {
  border-color: var(--signal-border);
}

.job-main {
  min-width: 0;
  flex: 1;
}

.job-title-row {
  display: flex;
  align-items: baseline;
  gap: 16px;
}

.job-title-row h2 {
  min-width: 0;
  margin: 0;
  color: var(--text-1);
  font-size: 17px;
  font-weight: 600;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.job-title-row strong {
  flex: none;
  color: var(--signal-text);
  font-size: 17px;
  font-weight: 600;
}

.job-company {
  margin-top: 5px;
  color: var(--text-2);
  font-size: 14px;
}

.job-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin-top: 12px;
  color: var(--text-3);
  font-size: 13px;
}

.job-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}

.job-tags :deep(.ant-tag) {
  margin-inline-end: 0;
}

.job-side {
  display: flex;
  flex: none;
  min-width: 150px;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  color: var(--text-3);
  font-size: 12px;
}

.job-recruiter {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-side :deep(.ant-btn) {
  margin-top: auto;
}

.job-empty {
  padding: 48px 0;
}

.job-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 18px;
}

.job-disclaimer {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--hairline);
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

@media (max-width: 1200px) {
  .job-filters {
    grid-template-columns: minmax(180px, 1fr) 116px 132px;
  }

  .filter-actions {
    grid-column: span 2;
  }
}

@media (max-width: 720px) {
  .job-filters {
    grid-template-columns: 1fr 1fr;
  }

  .job-filters > :first-child,
  .filter-actions {
    grid-column: 1 / -1;
  }

  .filter-actions :deep(.ant-btn) {
    flex: 1;
  }

  .job-card {
    flex-direction: column;
    gap: 16px;
    padding: 16px;
  }

  .job-side {
    min-width: 0;
    flex-direction: row;
    align-items: center;
    flex-wrap: wrap;
  }

  .job-side :deep(.ant-btn) {
    margin-top: 0;
    margin-left: auto;
  }

  .job-pagination {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
