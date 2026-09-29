<template>
  <div class="home-page">
    <section class="home-hero">
      <div class="hero-text">
        <div class="hero-title">{{ greeting }}，{{ auth.displayName }}</div>
        <div class="hero-sub muted">{{ todayText }}</div>
      </div>
      <div class="hero-actions">
        <a-button type="primary" @click="go('/customer-service')">
          <MessageOutlined /> 开始对话
        </a-button>
        <a-button @click="go('/knowledge')">
          <UploadOutlined /> 上传资料
        </a-button>
      </div>
    </section>

    <!-- 对话模型是首页多数入口的共同前提，先把缺的讲清楚，省得点进去再撞各页的 gate。 -->
    <a-alert
      v-if="chatGate"
      type="warning"
      show-icon
      class="home-gate"
      message="未配置可用的对话模型"
      description="对话、简历分析、幻灯片生成这类功能都要一个已通过连通性测试的对话模型。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="go('/settings')">去配置</a-button>
      </template>
    </a-alert>

    <div class="home-columns">
      <section class="home-card">
        <div class="card-title">功能模块</div>
        <div class="module-grid">
          <button
            v-for="mod in modules"
            :key="mod.path"
            type="button"
            class="module-card"
            @click="go(mod.path)"
          >
            <span class="module-head">
              <span class="module-icon"><component :is="mod.icon" /></span>
              <span class="module-name">{{ mod.name }}</span>
            </span>
            <span class="module-desc muted">{{ mod.desc }}</span>
            <span class="module-count">
              <a-skeleton v-if="mod.value === null" active :title="false" :paragraph="{ rows: 1, width: '72px' }" />
              <template v-else>
                {{ mod.value.toLocaleString() }}<em>{{ mod.unit }}</em>
              </template>
            </span>
            <span v-if="mod.extra" class="module-extra muted">{{ mod.extra }}</span>
          </button>
        </div>
      </section>

      <section class="home-card">
        <div class="card-title">
          最近对话
          <button v-if="recent.length" type="button" class="card-more" @click="go('/customer-service')">
            查看全部
          </button>
        </div>
        <a-skeleton v-if="!convsLoaded" active :paragraph="{ rows: 5 }" :title="false" />
        <a-empty
          v-else-if="!recent.length"
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          description="还没有会话"
        />
        <div v-else class="recent-list">
          <button
            v-for="conv in recent"
            :key="conv.id"
            type="button"
            class="recent-item"
            @click="openConversation(conv.id)"
          >
            <span class="recent-title">{{ conv.title }}</span>
            <span class="recent-meta muted">
              {{ conv.message_count }} 条 · {{ relativeTime(conv.updated_at) }}
            </span>
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Empty } from 'ant-design-vue'
import {
  ApiOutlined,
  BookOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FundProjectionScreenOutlined,
  MessageOutlined,
  ProfileOutlined,
  RobotOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import {
  chatApi,
  dashboardApi,
  settingsApi,
  type Conversation,
  type DashboardSummary,
} from '@/api'
import { useAuthStore } from '@/stores/auth'

/** 首页上的一张模块卡片：数量口径来自 /dashboard/summary，按各页自己的展示范围取。 */
interface HomeModule {
  path: string
  name: string
  desc: string
  icon: unknown
  unit: string
  extra?: string
  /** null 表示概览还没回来，卡片先亮骨架屏。 */
  value: number | null
}

const router = useRouter()
const auth = useAuthStore()

const summary = ref<DashboardSummary | null>(null)
const conversations = ref<Conversation[]>([])
const convsLoaded = ref(false)
const chatReady = ref(true)

const RECENT_LIMIT = 6

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return '夜深了'
  if (hour < 12) return '早上好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
})

const todayText = computed(() =>
  new Date().toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  }),
)

const modules = computed<HomeModule[]>(() => {
  const s = summary.value
  const list: HomeModule[] = [
    {
      path: '/customer-service',
      name: '对话',
      desc: '与智能体问答，可挂知识库与运维工具',
      icon: MessageOutlined,
      unit: '个会话',
      value: s?.conversation_count ?? null,
    },
    {
      path: '/knowledge',
      name: '知识库',
      desc: '项目、知识类型与向量索引',
      icon: BookOutlined,
      unit: '份文档',
      value: s?.knowledge_document_count ?? null,
    },
    {
      path: '/office/resume',
      name: '简历',
      desc: '分析、对比与求职助手',
      icon: ProfileOutlined,
      unit: '份简历',
      value: s?.resume_count ?? null,
      extra: s ? `面试记录 ${s.interview_count}` : undefined,
    },
    {
      path: '/office/slides',
      name: '幻灯片',
      desc: '由材料生成，可放映与导出',
      icon: FundProjectionScreenOutlined,
      unit: '份幻灯片',
      value: s?.slide_count ?? null,
    },
    {
      path: '/ops/server',
      name: '服务器',
      desc: 'SSH 台账与终端运维',
      icon: CloudServerOutlined,
      unit: '台服务器',
      value: s?.server_count ?? null,
    },
    {
      path: '/ops/database',
      name: '数据库',
      desc: 'MySQL 与 Redis 连接',
      icon: DatabaseOutlined,
      unit: '个连接',
      value: s?.database_count ?? null,
    },
    {
      path: '/agents',
      name: '智能体',
      desc: '人设、提示词与工具开关',
      icon: RobotOutlined,
      unit: '个智能体',
      value: s?.agent_count ?? null,
    },
  ]
  // 网关是平台级资产，后端只对管理员出这三项；卡片也以管理员身份为条件。
  if (auth.isAdmin && s) {
    list.push({
      path: '/system/ai-gateway',
      name: 'AI 网关',
      desc: '上游通道、模型路由与调用日志',
      icon: ApiOutlined,
      unit: '次调用（今日）',
      value: s.ai_call_count_today ?? 0,
      extra: `${(s.ai_token_count_today ?? 0).toLocaleString()} tokens · ${s.ai_channel_count ?? 0} 个通道`,
    })
  }
  return list
})

const recent = computed(() => conversations.value.slice(0, RECENT_LIMIT))

const chatGate = computed(() => !chatReady.value)

/** 相对时间：列表里「刚刚 / 3 小时前」比完整时间戳更好扫，超过一周回落到日期。 */
function relativeTime(value: string): string {
  const diff = Date.now() - new Date(value).getTime()
  const minute = 60 * 1000
  if (diff < minute) return '刚刚'
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`
  if (diff < 7 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))} 天前`
  return new Date(value).toLocaleDateString('zh-CN')
}

async function loadSummary() {
  try {
    summary.value = await dashboardApi.summary()
  } catch {
    // 拦截器已弹提示；卡片留在骨架态好过闪成 0。
  }
}

async function loadConversations() {
  try {
    const data = await chatApi.listConversations()
    conversations.value = data.items
  } catch {
    // 同上：拿不到就不装作「还没有会话」。
  } finally {
    convsLoaded.value = true
  }
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
  } catch {
    // 状态检查失败不该在家门口亮一条红字吓唬人：对话页自己有重试入口。
    chatReady.value = true
  }
}

function go(path: string) {
  router.push(path)
}

/** 带着会话 id 进对话页，由那边打开指定会话（见 CustomerService 的深链处理）。 */
function openConversation(id: number) {
  router.push({ path: '/customer-service', query: { conv: String(id) } })
}

async function loadAll() {
  await Promise.all([loadSummary(), loadConversations(), loadReadiness()])
}

onMounted(loadAll)

// 首页挂在 keep-alive 里：从别的页面切回来时数量该是新的，不然「会话 1」能停在原地一整天。
let firstActivation = true
onActivated(() => {
  if (firstActivation) {
    firstActivation = false
    return
  }
  void loadAll()
})
</script>

<style scoped>
.home-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.home-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  padding: 20px 24px;
}
.hero-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-1);
}
.hero-sub {
  margin-top: 4px;
  font-size: 13px;
}
.hero-actions {
  display: flex;
  gap: 8px;
}
.home-gate {
  margin: 0;
}

.home-columns {
  display: grid;
  /* 模块卡片是主角，最近对话只占一列；窄窗（含手机）自动堆叠。 */
  grid-template-columns: minmax(0, 2fr) minmax(240px, 1fr);
  gap: 16px;
  align-items: start;
}
@media (max-width: 900px) {
  .home-columns {
    grid-template-columns: 1fr;
  }
}

.home-card {
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  padding: 20px 24px;
}
.card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 14px;
}
.card-more {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-size: 12px;
  font-weight: 400;
  color: var(--signal-text);
  cursor: pointer;
}

.module-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(220px, 100%), 1fr));
  gap: 12px;
}
.module-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: left;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s;
}
.module-card:hover {
  border-color: var(--signal-border);
}
.module-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.module-icon {
  display: inline-flex;
  align-items: center;
  color: var(--signal-text);
  font-size: 16px;
}
.module-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}
.module-desc {
  font-size: 12px;
  line-height: 1.5;
  min-height: 36px;
}
.module-count {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.2;
}
.module-count em {
  font-style: normal;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-3);
  margin-left: 6px;
}
.module-extra {
  font-size: 12px;
}

.recent-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.recent-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  border-radius: var(--radius-md);
  padding: 8px 10px;
  cursor: pointer;
  font: inherit;
  transition: background-color 0.15s;
}
.recent-item:hover {
  background: var(--grid-hover);
}
.recent-title {
  /* 长标题省略而不是换行撑高一格。 */
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--text-1);
}
.recent-meta {
  font-size: 12px;
}
</style>
