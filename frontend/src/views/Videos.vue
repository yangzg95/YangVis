<template>
  <div class="page-card">
    <div class="page-title">短视频</div>

    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="创作卡与分镜由一条内置流程产出，它用的是用途为「对话」的模型。请先添加、通过连通性测试并设为默认。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="!imageReady"
      type="info"
      show-icon
      class="gate-alert"
      message="还没配出图的模型"
      description="脚本可以先写，但首帧渲染要用用途为「图片」的模型：到设置里添加、测通并设为默认后才能出片。"
    >
      <template #action>
        <a-button size="small" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <div class="toolbar toolbar--between toolbar--snug">
      <a-space :size="12" wrap>
        <span class="muted">
          {{ filteredProjects.length === projects.length
            ? `共 ${projects.length} 条片子`
            : `筛出 ${filteredProjects.length} / ${projects.length} 条片子` }}
        </span>
        <a-select v-model:value="lifecycleFilter" size="small" class="lifecycle-filter">
          <a-select-option value="">全部制作进度</a-select-option>
          <a-select-opt-group label="脚本">
            <a-select-option value="script:pending">待生成</a-select-option>
            <a-select-option value="script:active">生成中</a-select-option>
            <a-select-option value="script:done">已生成</a-select-option>
            <a-select-option value="script:attention">脚本失败</a-select-option>
          </a-select-opt-group>
          <a-select-opt-group label="首帧">
            <a-select-option value="frames:pending">待渲染</a-select-option>
            <a-select-option value="frames:active">渲染中</a-select-option>
            <a-select-option value="frames:partial">部分完成</a-select-option>
            <a-select-option value="frames:done">已完成</a-select-option>
            <a-select-option value="frames:attention">需处理</a-select-option>
          </a-select-opt-group>
          <a-select-opt-group label="片段">
            <a-select-option value="clips:pending">待渲染</a-select-option>
            <a-select-option value="clips:active">渲染中</a-select-option>
            <a-select-option value="clips:partial">部分完成</a-select-option>
            <a-select-option value="clips:done">已完成</a-select-option>
            <a-select-option value="clips:attention">需处理</a-select-option>
          </a-select-opt-group>
          <a-select-opt-group label="成片">
            <a-select-option value="publish:blocked">待片段</a-select-option>
            <a-select-option value="publish:pending">待合成</a-select-option>
            <a-select-option value="publish:active">合成中</a-select-option>
            <a-select-option value="publish:done">已合成</a-select-option>
            <a-select-option value="publish:attention">合成失败</a-select-option>
          </a-select-opt-group>
        </a-select>
      </a-space>
      <a-button type="primary" :disabled="!chatReady" @click="openCreate">
        <PlusOutlined /> 新建短视频
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="filteredProjects"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'cover'">
          <img v-if="record.cover_url" class="cover-thumb" :src="record.cover_url" alt="" />
          <span v-else class="cover-thumb cover-empty">9:16</span>
        </template>
        <template v-else-if="column.key === 'title'">
          <div class="cell-title">{{ record.title }}</div>
          <div class="cell-sub">
            {{ platformText(record.platform) }} · {{ record.shot_count }} 镜 ·
            {{ record.duration_s }} 秒
          </div>
        </template>
        <template v-else-if="column.key === 'script'">
          <!-- 脚本状态来自认领这条片子的运行，点它直接看那次运行的现场。 -->
          <a-tooltip :title="statusHint(record.status, record.error_msg)">
            <a-tag
              :color="lifecycleFor(record, 'script').color"
              class="run-tag"
              @click="openRuns(record)"
            >
              <LoadingOutlined v-if="record.status === 'analyzing'" />
              {{ lifecycleFor(record, 'script').text }}
            </a-tag>
          </a-tooltip>
        </template>
        <template v-else-if="['frames', 'clips', 'publish'].includes(column.key)">
          <a-tooltip :title="lifecycleFor(record, column.key).hint">
            <div class="lifecycle-cell">
              <a-tag :color="lifecycleFor(record, column.key).color">
                <LoadingOutlined v-if="lifecycleFor(record, column.key).state === 'active'" />
                {{ lifecycleFor(record, column.key).text }}
              </a-tag>
              <div v-if="lifecycleFor(record, column.key).detail" class="stage-detail">
                {{ lifecycleFor(record, column.key).detail }}
                <span v-if="lifecycleFor(record, column.key).attention" class="stage-attention">
                  · {{ lifecycleFor(record, column.key).attention }} 需处理
                </span>
              </div>
            </div>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'updated_at'">
          <span class="muted">{{ fmtDateTime(record.updated_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4" wrap>
            <a-button size="small" type="link" @click="openWorkbench(record.id)">工作台</a-button>
            <a-popconfirm
              title="重新生成会整包替换创作卡、口播文案与全部分镜（含已保存内容），已渲出的首帧与片段将不再挂在新镜头上，确认继续？"
              ok-text="重新生成"
              cancel-text="取消"
              @confirm="onRegenerate(record)"
            >
              <a-button
                size="small"
                type="link"
                :disabled="!chatReady || record.status === 'analyzing'"
              >
                重新生成
              </a-button>
            </a-popconfirm>
            <a-popconfirm
              title="确认删除这条短视频？首帧与封面文件会一并从服务器上删除。"
              ok-text="删除"
              cancel-text="取消"
              @confirm="onRemove(record)"
            >
              <a-button size="small" type="link" danger :disabled="hasActiveVideoWork(record)">
                删除
              </a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
      <template #emptyText>
        <a-empty
          :image="simpleEmpty"
          :description="lifecycleFilter
            ? '没有符合当前制作进度的短视频'
            : '还没有短视频，写一句想法生成第一版分镜'"
        />
      </template>
    </a-table>

    <!-- 新建：想法一次性给全，脚本跑完再进工作台逐镜调。 -->
    <a-modal
      v-model:open="createModal.open"
      title="新建短视频"
      :confirm-loading="createModal.submitting"
      ok-text="生成脚本"
      cancel-text="取消"
      width="640px"
      @ok="onCreate"
    >
      <a-form layout="vertical" class="create-form">
        <a-form-item label="想法" required>
          <a-textarea
            v-model:value="createModal.idea"
            :rows="3"
            :maxlength="4000"
            placeholder="一句话说清这条片子要讲什么、给谁看。例如：教普通打工人三步做出老板看得懂的季度复盘"
          />
        </a-form-item>
        <a-form-item label="标题（可选）">
          <a-input
            v-model:value="createModal.title"
            :maxlength="128"
            placeholder="留空则由生成结果里的标题补上"
          />
        </a-form-item>
        <a-form-item label="发布平台">
          <a-radio-group v-model:value="createModal.platform" button-style="solid" size="small">
            <a-radio-button value="douyin">抖音</a-radio-button>
            <a-radio-button value="kuaishou">快手</a-radio-button>
            <a-radio-button value="other">通用</a-radio-button>
          </a-radio-group>
          <div class="hint">平台只影响策划师对节奏与时长的偏好，不写死任何平台规则。</div>
        </a-form-item>
        <a-form-item label="成片目标时长（可选）">
          <a-input-number
            v-model:value="createModal.durationS"
            :min="5"
            :max="60"
            :step="5"
            placeholder="留空＝由策划师按选题定"
            style="width: 100%"
          />
          <div class="hint">单位秒，5-60。定下来后分镜会按它分配每一镜的时长。</div>
        </a-form-item>
        <a-form-item label="补充要求（可选）">
          <a-input
            v-model:value="createModal.requirement"
            :maxlength="512"
            placeholder="例如：前 3 秒给结论，别出现公司名，结尾带一句行动号召"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 进度与失败原因都长在这次项目的运行记录上，这里就是它的入口。 -->
    <WorkflowRunsDrawer
      v-model:open="runsOpen"
      :workflow="null"
      :subject="runsSubject"
      @changed="load"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, h, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { LoadingOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { Button, Empty, message, notification } from 'ant-design-vue'
import {
  settingsApi,
  videosApi,
  type VideoPlatform,
  type VideoProjectItem,
  type VideoProjectStatus,
  type WorkflowRunSubject,
} from '@/api'
import WorkflowRunsDrawer from '@/components/WorkflowRunsDrawer.vue'
import { errorText } from '@/utils/errors'
import { fmtDateTime } from '@/utils/format'
import { createPollLoop } from '@/utils/polling'
import {
  hasActiveVideoWork,
  matchesVideoLifecycle,
  videoLifecycleStage,
  type VideoLifecycleStage,
} from '@/utils/videoLifecycle'

// 智能应用 · 短视频列表：脚本、首帧、片段与成片各有事实源，本页只做摘要。
// 脚本沿用工作流状态；首帧与片段汇总当前镜头的 media_job；成片取最近 publish_job。
// 任何阶段还在活动都继续轮询，但筛选只改变展示，不影响完整列表的进度跟随。

const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const projects = ref<VideoProjectItem[]>([])
const loading = ref(false)
const chatReady = ref(true)
const imageReady = ref(true)
const lifecycleFilter = ref('')
const filteredProjects = computed(() =>
  projects.value.filter((item) => matchesVideoLifecycle(item, lifecycleFilter.value)),
)

const columns = [
  { key: 'cover', title: '封面', width: 72 },
  { key: 'title', title: '片子', dataIndex: 'title', ellipsis: true, width: 260 },
  { key: 'script', title: '脚本', dataIndex: 'status', width: 110 },
  { key: 'frames', title: '首帧', width: 140 },
  { key: 'clips', title: '片段', width: 140 },
  { key: 'publish', title: '成片', width: 120 },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 180 },
  { key: 'actions', title: '操作', width: 230 },
]

const runsOpen = ref(false)
const runsSubject = ref<WorkflowRunSubject | null>(null)

function openRuns(record: VideoProjectItem) {
  runsSubject.value = { kind: 'video-project', ref: record.id, label: record.title }
  runsOpen.value = true
}

function lifecycleFor(record: VideoProjectItem, key: string) {
  return videoLifecycleStage(record, key as VideoLifecycleStage)
}

/** 状态标签的悬浮说明：失败时先给原因，再告诉这点会开什么。 */
function statusHint(status: VideoProjectStatus | string, errorMsg: string | null): string {
  return status === 'error' && errorMsg ? `${errorMsg} · 点击查看运行记录` : '点击查看运行记录'
}

const createModal = reactive<{
  open: boolean
  submitting: boolean
  idea: string
  title: string
  platform: VideoPlatform
  durationS: number | null
  requirement: string
}>({
  open: false,
  submitting: false,
  idea: '',
  title: '',
  platform: 'douyin',
  durationS: null,
  requirement: '',
})

function openCreate() {
  createModal.open = true
  createModal.submitting = false
  createModal.idea = ''
  createModal.title = ''
  createModal.platform = 'douyin'
  createModal.durationS = null
  createModal.requirement = ''
}

// ---- 加载与轮询 --------------------------------------------------------------

// 上一次看到的状态，用来识别「刚生成完」这个跃迁：完成提示做成带按钮的通知，
// 由用户点进工作台——轮询回调里直接导航会把人正在看的列表跳走。
const lastStatus = new Map<number, VideoProjectStatus | string>()

async function load() {
  loading.value = true
  try {
    const result = await videosApi.list()
    projects.value = result.items
    for (const item of result.items) {
      const previous = lastStatus.get(item.id)
      if (previous === 'analyzing' && item.status !== 'analyzing') notifyFinished(item)
      lastStatus.set(item.id, item.status)
    }
  } finally {
    loading.value = false
  }
  syncPolling()
}

function notifyFinished(item: VideoProjectItem) {
  if (item.status === 'ready') {
    notification.success({
      key: `video-script-${item.id}`,
      message: `《${item.title}》分镜已生成`,
      description: `${item.shot_count} 个镜头，可在工作台里逐镜调整并渲首帧`,
      btn: () =>
        h(
          Button,
          { size: 'small', type: 'primary', onClick: () => openWorkbench(item.id) },
          () => '打开工作台',
        ),
    })
  } else {
    notification.error({
      key: `video-script-${item.id}`,
      message: `《${item.title}》脚本生成失败`,
      description: item.error_msg || '模型没有返回可用结构，可重新生成',
    })
  }
}

// 四个阶段都可能后台推进；失败退避、页面隐藏暂停见 utils/polling.ts。
const pollLoop = createPollLoop(load, {
  shouldContinue: () => projects.value.some(hasActiveVideoWork),
})

function syncPolling() {
  if (projects.value.some(hasActiveVideoWork)) pollLoop.start()
  else pollLoop.stop()
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
    imageReady.value = status.image.ready
  } catch {
    // 状态接口失败时按可用处理，真正的报错会在提交时冒出来。
    chatReady.value = true
    imageReady.value = true
  }
}

// ---- 操作 --------------------------------------------------------------------

function openWorkbench(id: number) {
  router.push({ name: 'VideoWorkbench', params: { id } })
}

function goToSettings() {
  router.push('/system/settings')
}

async function onCreate() {
  if (!createModal.idea.trim()) {
    message.warning('请先写一句想法')
    return
  }
  createModal.submitting = true
  try {
    await videosApi.create({
      idea: createModal.idea.trim(),
      title: createModal.title.trim() || undefined,
      platform: createModal.platform,
      duration_s: createModal.durationS ?? undefined,
      requirement: createModal.requirement.trim() || undefined,
    })
    createModal.open = false
    message.success('已开始生成脚本，稍后在工作台里逐镜调整')
    await load()
  } catch (err) {
    message.error(errorText(err, '创建失败'))
  } finally {
    createModal.submitting = false
  }
}

async function onRegenerate(record: VideoProjectItem) {
  try {
    await videosApi.regenerate(record.id, {})
    message.success('已开始重新生成')
    await load()
  } catch (err) {
    message.error(errorText(err, '重新生成失败'))
  }
}

async function onRemove(record: VideoProjectItem) {
  try {
    await videosApi.remove(record.id)
    message.success('已删除')
    await load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 展示辅助 ----------------------------------------------------------------

function platformText(platform: VideoPlatform | string): string {
  return { douyin: '抖音', kuaishou: '快手', other: '通用' }[platform] || platform
}

onMounted(() => {
  void Promise.all([loadReadiness(), load()])
})

// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
onDeactivated(() => pollLoop.stop())

onActivated(() => {
  syncPolling()
  // 从工作台返回时立即带出最新渲染进度；首次 mounted 正在加载时不重复请求。
  if (!loading.value) void load()
  void loadReadiness()
})

onUnmounted(() => pollLoop.stop())
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}

.cover-thumb {
  display: block;
  width: 40px;
  height: 71px;
  object-fit: cover;
  border-radius: 6px;
  background: var(--surface-raised, rgba(0, 0, 0, 0.03));
}

.cover-empty {
  color: var(--text-3);
  font-size: var(--fs-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--hairline);
}

.cell-title {
  font-weight: 600;
}

.cell-sub {
  color: var(--text-3);
  font-size: var(--fs-xs);
  margin-top: 2px;
}

.lifecycle-filter {
  width: 190px;
  text-align: left;
}

.lifecycle-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.lifecycle-cell :deep(.ant-tag) {
  margin-inline-end: 0;
}

.stage-detail {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.stage-attention {
  color: var(--danger, #cf1322);
}

.create-form :deep(.ant-form-item) {
  margin-bottom: 14px;
}
</style>
