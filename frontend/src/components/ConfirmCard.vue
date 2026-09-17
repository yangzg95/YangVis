<template>
  <div :class="['confirm-card', { dark }]">
    <div class="confirm-title">
      <WarningOutlined />
      AI 请求执行一条写操作命令
      <span v-if="action.target_name" class="confirm-target">{{ action.target_name }}</span>
    </div>
    <pre class="confirm-command">{{ action.command }}</pre>
    <div v-if="action.reason" class="confirm-reason">理由：{{ action.reason }}</div>

    <!-- 待确认：两个按钮 + 本地倒计时。倒计时只是展示层置灰，真正的过期
         判定在服务端 confirm 的 CAS 里——本地到点了用户还点得动，点到过期
         的 action 会收到 error 事件兜底。 -->
    <div v-if="displayStatus === 'pending'" class="confirm-actions">
      <span class="confirm-countdown">{{ countdownText }}</span>
      <a-button size="small" danger :disabled="acting" @click="emit('reject', action)">
        拒绝
      </a-button>
      <!-- 无 ops_write 的用户能看卡片、能拒绝，但同意按钮置灰；真正
           的拦截在服务端 confirm 的 403，这里只是提前讲清原因。 -->
      <a-tooltip :title="canApprove ? '' : '没有运维写权限，请联系管理员'">
        <a-button
          size="small"
          type="primary"
          :disabled="acting || !canApprove"
          @click="emit('approve', action)"
        >
          同意执行
        </a-button>
      </a-tooltip>
    </div>

    <template v-else>
      <div :class="['confirm-state', `is-${displayStatus}`]">{{ stateText }}</div>
      <!-- 执行完/失败的输出块：续答模型看的是同一份，用户也能直接核对。 -->
      <pre v-if="showResult" class="confirm-result">{{ action.result }}</pre>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { WarningOutlined } from '@ant-design/icons-vue'
import type { OpsAction } from '@/api'

const props = withDefaults(
  defineProps<{
    action: OpsAction
    /** 点了按钮之后父组件把卡片置灰，等服务端结果回来再切状态。 */
    acting?: boolean
    /** 深色变体：嵌在深色页里（全屏终端页）时使用，默认浅色。 */
    dark?: boolean
    /** 调用方有没有运维写权限；false 时同意按钮置灰并说明原因。 */
    canApprove?: boolean
  }>(),
  { acting: false, dark: false, canApprove: true },
)

const emit = defineEmits<{
  (e: 'approve', action: OpsAction): void
  (e: 'reject', action: OpsAction): void
}>()

// 本地倒计时：created_at + timeout_seconds 算截止时间。后端写的是本地
// 时区的 DATETIME，new Date() 对不带时区的 ISO 串按本地解析，正好对上。
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined

const deadline = computed(() => {
  const created = new Date(props.action.created_at).getTime()
  return created + props.action.timeout_seconds * 1000
})

const remainingSeconds = computed(() =>
  Math.max(0, Math.ceil((deadline.value - now.value) / 1000)),
)

// 倒计时归零不直接改 action.status（那是服务端的裁决权），只在展示层
// 当作 expired 渲染。
const displayStatus = computed(() =>
  props.action.status === 'pending' && remainingSeconds.value <= 0
    ? 'expired'
    : props.action.status,
)

const countdownText = computed(() => {
  const m = Math.floor(remainingSeconds.value / 60)
  const s = remainingSeconds.value % 60
  return `剩余 ${m}:${String(s).padStart(2, '0')}`
})

const STATE_TEXT: Record<OpsAction['status'], string> = {
  pending: '',
  approved: '已批准，正在执行…',
  rejected: '已拒绝',
  expired: '等待超时，请让 AI 重新提议',
  executed: '',
  failed: '',
}

const stateText = computed(() => {
  const status = displayStatus.value
  if (status === 'executed') {
    return `已执行完成（退出码 ${props.action.exit_status ?? 0}）`
  }
  if (status === 'failed') {
    return props.action.exit_status != null
      ? `执行失败（退出码 ${props.action.exit_status}）`
      : '执行失败'
  }
  return STATE_TEXT[status]
})

const showResult = computed(
  () =>
    (displayStatus.value === 'executed' || displayStatus.value === 'failed') &&
    !!props.action.result,
)

onMounted(() => {
  if (props.action.status === 'pending') {
    timer = setInterval(() => {
      now.value = Date.now()
      // 归零后停表：displayStatus 已经切到 expired，不用再 tick。
      if (remainingSeconds.value <= 0 && timer) {
        clearInterval(timer)
        timer = undefined
      }
    }, 1000)
  }
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.confirm-card {
  margin-bottom: 10px;
  padding: 10px;
  border: 1px solid #ffd591;
  border-radius: 8px;
  background: #fffbe6;
}

.confirm-title {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #d46b08;
  font-size: 13px;
  font-weight: 600;
}

.confirm-target {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: #8c6b1f;
}

.confirm-command {
  margin: 8px 0 4px;
  padding: 6px 8px;
  border-radius: 4px;
  background: var(--ink);
  color: #d4d4d4;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.confirm-reason {
  color: #8c6b1f;
  font-size: 12px;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.confirm-countdown {
  margin-right: auto;
  color: #8c6b1f;
  font-size: 12px;
}

.confirm-state {
  margin-top: 6px;
  color: var(--text-3);
  font-size: 12px;
  text-align: right;
}

.confirm-state.is-executed {
  color: #389e0d;
}

.confirm-state.is-failed {
  color: #cf1322;
}

.confirm-result {
  margin: 6px 0 0;
  padding: 6px 8px;
  max-height: 200px;
  overflow-y: auto;
  border-radius: 4px;
  background: var(--ink);
  color: #d4d4d4;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ---- 深色变体（全屏终端页右侧的 AI 面板，与终端 #11181f 同一族） ------ */

.confirm-card.dark {
  border-color: rgba(250, 173, 20, 0.4);
  background: rgba(250, 173, 20, 0.08);
}

.confirm-card.dark .confirm-title {
  color: #faad14;
}

.confirm-card.dark .confirm-target,
.confirm-card.dark .confirm-countdown {
  color: #cfa14a;
}

.confirm-card.dark .confirm-command {
  background: #0d141b;
  color: #d8dee4;
}

.confirm-card.dark .confirm-reason {
  color: #cfa14a;
}

.confirm-card.dark .confirm-state {
  color: #8b98a5;
}

.confirm-card.dark .confirm-state.is-executed {
  color: #52c41a;
}

.confirm-card.dark .confirm-state.is-failed {
  color: #ff7875;
}

.confirm-card.dark .confirm-result {
  background: #0d141b;
  color: #d8dee4;
}
</style>
