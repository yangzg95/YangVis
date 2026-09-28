<template>
  <div class="page-card">
    <div class="page-title">幻灯片</div>

    <a-alert
      v-if="!chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="幻灯片的整份生成依赖一个用途为「对话」的模型。请先添加、通过连通性测试并设为默认。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <div class="toolbar">
      <span class="muted">
        共 {{ decks.length }} 份幻灯片；材料可以是文档、粘贴的文案，配图在建立后逐张上传
      </span>
      <a-button type="primary" :disabled="!chatReady" @click="openCreate">
        <PlusOutlined /> 新建幻灯片
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="decks"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'">
          <div class="cell-title">{{ record.title }}</div>
          <div class="cell-sub">
            {{ record.page_count }} 页 · {{ record.asset_count }} 张配图
            <template v-if="record.description"> · {{ record.description }}</template>
          </div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tooltip :title="record.status === 'error' ? record.error_msg : ''">
            <a-tag :color="statusColor(record.status)">
              <LoadingOutlined v-if="record.status === 'analyzing'" />
              {{ statusText(record.status) }}
            </a-tag>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'updated_at'">
          <span class="muted">{{ formatTime(record.updated_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4" wrap>
            <a-button
              size="small"
              type="link"
              :disabled="record.page_count === 0"
              @click="openShow(record.id)"
            >
              放映
            </a-button>
            <a-button size="small" type="link" @click="openWorkbench(record.id)">工作台</a-button>
            <a-popconfirm
              title="重新生成会覆盖当前编辑过的内容，确认继续？"
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
              title="确认删除这份幻灯片？配图文件会一并从服务器上删除。"
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
        <a-empty :image="simpleEmpty" description="还没有幻灯片，用一份材料生成第一版" />
      </template>
    </a-table>

    <!-- 新建：材料一次性给全，生成结束后再进工作台改。 -->
    <a-modal
      v-model:open="createModal.open"
      title="新建幻灯片"
      :confirm-loading="createModal.submitting"
      ok-text="生成"
      cancel-text="取消"
      width="640px"
      @ok="onCreate"
    >
      <a-form layout="vertical" class="create-form">
        <a-form-item label="主题" required>
          <a-input
            v-model:value="createModal.title"
            :maxlength="128"
            placeholder="例如：2026 年上半年业务复盘"
          />
        </a-form-item>
        <a-form-item label="内容说明">
          <a-textarea
            v-model:value="createModal.description"
            :rows="2"
            :maxlength="512"
            placeholder="这份幻灯片要讲什么、给谁看"
          />
        </a-form-item>
        <a-form-item label="材料（文档 / 图片）">
          <a-upload-dragger
            v-model:file-list="createModal.fileList"
            :before-upload="beforeUpload"
            :multiple="true"
            accept=".pdf,.docx,.txt,.md,.markdown"
          >
            <p class="ant-upload-drag-icon"><InboxOutlined /></p>
            <p class="ant-upload-text">拖入或选择文档</p>
            <p class="upload-hint">支持 PDF / Word / txt / md，单文件 10 MB 以内</p>
          </a-upload-dragger>
        </a-form-item>
        <a-form-item label="或直接粘贴文案">
          <a-textarea
            v-model:value="createModal.text"
            :rows="5"
            :maxlength="200000"
            placeholder="把要点、讲稿、数据结论直接贴进来，会和文档一起作为材料"
          />
        </a-form-item>
        <a-form-item label="生成要求（可选）">
          <a-input
            v-model:value="createModal.requirement"
            :maxlength="512"
            placeholder="例如：12 页以内，面向非技术管理层，多用数据"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { h, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { InboxOutlined, LoadingOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { Button, Empty, message, notification, type UploadFile } from 'ant-design-vue'
import {
  slidesApi,
  settingsApi,
  type SlideDeckItem,
  type SlideStatus,
} from '@/api'

// 智能应用 · 幻灯片列表：材料进来 → 后台生成 → 进工作台改 → 新开窗口放映。
// 生成本身不在本页等（一次要读完全部材料再产出整份结构，远超 axios 超时），
// 落一行 analyzing 记录后靠轮询跟随状态，与简历分析同一形态。

const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const decks = ref<SlideDeckItem[]>([])
const loading = ref(false)
const chatReady = ref(true)

const columns = [
  { key: 'title', title: '主题', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 180 },
  { key: 'actions', title: '操作', width: 260 },
]

const createModal = reactive<{
  open: boolean
  submitting: boolean
  title: string
  description: string
  requirement: string
  text: string
  fileList: UploadFile[]
}>({
  open: false,
  submitting: false,
  title: '',
  description: '',
  requirement: '',
  text: '',
  fileList: [],
})

function openCreate() {
  createModal.open = true
  createModal.submitting = false
  createModal.title = ''
  createModal.description = ''
  createModal.requirement = ''
  createModal.text = ''
  createModal.fileList = []
}

function beforeUpload(file: File) {
  // 返回 false 拦住 antd 的自动上传：文件先攒着，点「生成」时和表单一起提交。
  createModal.fileList = [...createModal.fileList, file as unknown as UploadFile]
  if (!createModal.title) {
    createModal.title = file.name.replace(/\.[^.]+$/, '')
  }
  return false
}

// ---- 加载与轮询 --------------------------------------------------------------

// 上一次看到的Status，用来识别「刚生成完」这个跃迁：轮询回调不是用户手势，
// window.open 会被浏览器拦，所以完成提示做成带按钮的通知，由用户点。
const lastStatus = new Map<number, SlideStatus>()

async function load() {
  loading.value = true
  try {
    const result = await slidesApi.list()
    decks.value = result.items
    for (const item of result.items) {
      const previous = lastStatus.get(item.id)
      if (previous === 'analyzing' && item.status !== 'analyzing') {
        notifyFinished(item)
      }
      lastStatus.set(item.id, item.status)
    }
  } finally {
    loading.value = false
  }
  syncPolling()
}

function notifyFinished(item: SlideDeckItem) {
  if (item.status === 'ready') {
    notification.success({
      key: `slide-done-${item.id}`,
      message: `《${item.title}》已生成`,
      description: `${item.page_count} 页，可在工作台里调整内容`,
      btn: () =>
        h(
          Button,
          { size: 'small', type: 'primary', onClick: () => openWorkbench(item.id) },
          () => '打开工作台',
        ),
    })
  } else {
    notification.error({
      key: `slide-done-${item.id}`,
      message: `《${item.title}》生成失败`,
      description: item.error_msg || '模型没有返回可用结构，可重新生成',
    })
  }
}

let pollTimer: number | undefined

function syncPolling() {
  const running = decks.value.some((item) => item.status === 'analyzing')
  if (running && pollTimer === undefined) {
    pollTimer = window.setInterval(async () => {
      try {
        await load()
      } catch {
        // 一轮失败不致命（拦截器已弹提示）：一次网络抖动不该让进度永远卡住。
        return
      }
    }, 2500)
  } else if (!running && pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态接口失败时按可用处理，真正的报错会在生成时冒出来。
    chatReady.value = true
  }
}

// ---- 操作 --------------------------------------------------------------------

function openWorkbench(id: number) {
  const href = router.resolve({ name: 'SlideWorkbench', params: { id } }).href
  window.open(href, '_blank', 'noopener')
}

function openShow(id: number) {
  const href = router.resolve({ name: 'SlideShow', params: { id } }).href
  window.open(href, '_blank', 'noopener')
}

function goToSettings() {
  router.push('/settings')
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

async function onCreate() {
  if (!createModal.title.trim()) {
    message.warning('请填写主题')
    return
  }
  const files = createModal.fileList
    .map((item) => item.originFileObj as unknown as File)
    .filter((file): file is File => Boolean(file))
  if (!files.length && !createModal.text.trim() && !createModal.description.trim()) {
    message.warning('请上传文档、粘贴文案，或至少写一句内容说明')
    return
  }
  createModal.submitting = true
  try {
    await slidesApi.create({
      title: createModal.title.trim(),
      description: createModal.description.trim() || undefined,
      requirement: createModal.requirement.trim() || undefined,
      text: createModal.text.trim() || undefined,
      files,
    })
    createModal.open = false
    message.success('已开始生成，稍后在工作台里调整')
    await load()
  } catch (err) {
    message.error(errorText(err, '创建失败'))
  } finally {
    createModal.submitting = false
  }
}

async function onRegenerate(record: SlideDeckItem) {
  try {
    await slidesApi.regenerate(record.id)
    message.success('已开始重新生成')
    await load()
  } catch (err) {
    message.error(errorText(err, '重新生成失败'))
  }
}

async function onRemove(record: SlideDeckItem) {
  try {
    await slidesApi.remove(record.id)
    message.success('已删除')
    await load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

// ---- 展示辅助 ----------------------------------------------------------------

function statusText(status: SlideStatus | string): string {
  return { analyzing: '生成中', ready: '已生成', error: '失败' }[status] || status
}

function statusColor(status: SlideStatus | string): string {
  return { analyzing: 'processing', ready: 'success', error: 'error' }[status] || 'default'
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

onMounted(() => {
  void Promise.all([loadReadiness(), load()])
})

// keep-alive：切去别的标签页时停表，回来时按当前状态恢复。
onDeactivated(() => {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
})

onActivated(() => {
  syncPolling()
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 4px 0 16px;
  flex-wrap: wrap;
}

.cell-title {
  font-weight: 600;
}

.cell-sub {
  color: var(--text-3);
  font-size: 12px;
  margin-top: 2px;
}

.upload-hint {
  color: var(--text-3);
  font-size: 12px;
}

.create-form :deep(.ant-form-item) {
  margin-bottom: 14px;
}
</style>
