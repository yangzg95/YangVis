<template>
  <div class="page-card">
    <div class="page-title">AI 网关</div>

    <a-result
      v-if="forbidden"
      status="403"
      title="需要管理员权限"
      sub-title="AI 网关是平台级共享资产，只有管理员可以配置通道、签发密钥与查看调用记录。"
    />

    <a-tabs v-else v-model:activeKey="tab" @change="onTabChange">
      <!-- ---- 概览 ---- -->
      <a-tab-pane key="overview" tab="概览">
        <div class="access-card">
          <div class="access-row">
            <span class="access-label">接入地址（base_url）</span>
            <code class="mono access-value">{{ baseUrl }}</code>
            <a-button size="small" @click="copy(baseUrl)">复制</a-button>
          </div>
          <div class="access-row">
            <span class="access-label">调用示例</span>
            <pre class="mono access-snippet">{{ curlSample }}</pre>
          </div>
          <div class="hint">
            对外走 OpenAI 兼容协议：把 base_url 填成上面的地址、api_key 填成「统一密钥」页签里签发的钥匙即可，
            请求里的 model 用「模型路由」页签配置的对外模型名。
            开发环境下这个地址由 Vite 代理转发到后端 18099，生产环境前后端同源、直接就是它。
            <template v-if="overview && !overview.payload_logging">
              当前已关闭正文留存（AI_GATEWAY_LOG_PAYLOAD=false），调用日志只记指标不记内容。
            </template>
          </div>
        </div>

        <div class="stat-row">
          <div class="stat-card">
            <div class="stat-value">{{ stats?.totals.calls ?? 0 }}</div>
            <div class="stat-label">调用次数</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ successRate }}</div>
            <div class="stat-label">成功率（失败 {{ stats?.totals.failed ?? 0 }}）</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ (stats?.totals.total_tokens ?? 0).toLocaleString() }}</div>
            <div class="stat-label">Token 合计</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ stats?.totals.avg_latency_ms ?? 0 }} ms</div>
            <div class="stat-label">平均耗时（最长 {{ stats?.totals.max_latency_ms ?? 0 }} ms）</div>
          </div>
        </div>

        <div class="toolbar">
          <a-radio-group v-model:value="statsDays" button-style="solid" size="small" @change="loadStats">
            <a-radio-button :value="1">今天</a-radio-button>
            <a-radio-button :value="7">近 7 天</a-radio-button>
            <a-radio-button :value="30">近 30 天</a-radio-button>
          </a-radio-group>
          <a-button size="small" :loading="statsLoading" @click="loadStats">刷新</a-button>
        </div>

        <div v-if="dailyTrend.length" class="trend">
          <div v-for="point in dailyTrend" :key="point.name" class="trend-col">
            <div class="trend-bars">
              <a-tooltip :title="`${point.name}：${point.calls} 次，失败 ${point.failed}，${point.total_tokens} tokens`">
                <div class="trend-bar" :style="{ height: `${barHeight(point.calls)}%` }" />
              </a-tooltip>
            </div>
            <div class="trend-label">{{ shortDate(point.name) }}</div>
          </div>
        </div>
        <a-empty v-else :image="Empty.PRESENTED_IMAGE_SIMPLE" description="这段时间还没有调用" />

        <div class="breakdown">
          <div v-for="group in breakdowns" :key="group.title" class="breakdown-block">
            <div class="breakdown-title">{{ group.title }}</div>
            <a-table
              :data-source="group.rows"
              :columns="breakdownColumns"
              row-key="name"
              size="small"
              :pagination="false"
              :scroll="{ x: 'max-content', y: 220 }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'failed'">
                  <span :class="{ 'text-danger': record.failed > 0 }">{{ record.failed }}</span>
                </template>
                <template v-else-if="column.key === 'total_tokens'">
                  {{ record.total_tokens.toLocaleString() }}
                </template>
              </template>
            </a-table>
          </div>
        </div>
      </a-tab-pane>

      <!-- ---- 上游通道 ---- -->
      <a-tab-pane key="channels" tab="上游通道">
        <div class="toolbar">
          <span class="hint">
            一个通道 = 一个 OpenAI 兼容的上游厂商（base_url + api_key）。同一份对外模型可以挂多个通道做故障转移。
          </span>
          <a-button type="primary" class="toolbar-action" @click="openChannel()">
            <PlusOutlined /> 新增通道
          </a-button>
        </div>

        <a-table
          :data-source="channels"
          :columns="channelColumns"
          row-key="id"
          :loading="channelLoading"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'api_key'">
              <span class="mono">{{ record.api_key || '—' }}</span>
              <div v-if="record.api_key_error" class="text-danger hint">{{ record.api_key_error }}</div>
            </template>
            <template v-else-if="column.key === 'models'">
              <a-tag v-for="name in record.models || []" :key="name">{{ name }}</a-tag>
              <span v-if="!record.models?.length" class="muted">未填写</span>
            </template>
            <template v-else-if="column.key === 'enabled'">
              <a-switch
                :checked="record.enabled"
                size="small"
                @change="(value: boolean) => toggleChannel(record, value)"
              />
            </template>
            <template v-else-if="column.key === 'last_test'">
              <a-tag v-if="record.last_tested_at" :color="record.last_test_ok ? 'success' : 'error'">
                {{ record.last_test_ok ? '可用' : '失败' }}
              </a-tag>
              <span v-else class="muted">未测试</span>
              <div v-if="record.last_test_error" class="hint text-danger">{{ record.last_test_error }}</div>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" :loading="testingId === record.id" @click="testChannel(record)">
                  测试
                </a-button>
                <a-button size="small" @click="openChannel(record)">编辑</a-button>
                <a-popconfirm
                  title="删除通道会同时删掉指向它的模型路由，确认？"
                  @confirm="removeChannel(record)"
                >
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ---- 模型路由 ---- -->
      <a-tab-pane key="routes" tab="模型路由">
        <div class="toolbar">
          <a-input
            v-model:value="routeFilter"
            allow-clear
            class="filter-input"
            placeholder="按对外模型名筛选"
            @press-enter="loadRoutes"
          />
          <a-button size="small" @click="loadRoutes">查询</a-button>
          <span class="hint">同名多通道时按优先级升序依次尝试，上游拒绝或不可达自动转移到下一条。</span>
          <a-button type="primary" class="toolbar-action" @click="openRoute()">
            <PlusOutlined /> 新增路由
          </a-button>
        </div>

        <a-table
          :data-source="routes"
          :columns="routeColumns"
          row-key="id"
          :loading="routeLoading"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'model_name'">
              <code class="mono">{{ record.model_name }}</code>
            </template>
            <template v-else-if="column.key === 'channel_name'">
              <span>{{ record.channel_name || '—' }}</span>
              <a-tag v-if="!record.channel_name" color="error">通道已删除</a-tag>
            </template>
            <template v-else-if="column.key === 'upstream_model'">
              <code class="mono">{{ record.upstream_model || record.model_name }}</code>
            </template>
            <template v-else-if="column.key === 'enabled'">
              <a-switch
                :checked="record.enabled"
                size="small"
                @change="(value: boolean) => toggleRoute(record, value)"
              />
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" @click="openRoute(record)">编辑</a-button>
                <a-popconfirm title="确认删除这条路由？" @confirm="removeRoute(record)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ---- 统一密钥 ---- -->
      <a-tab-pane key="keys" tab="统一密钥">
        <div class="toolbar">
          <span class="hint">
            密钥明文只在创建时出现一次，数据库里只存摘要，之后无法找回；忘了就新建一把、把旧的停用。
          </span>
          <a-button type="primary" class="toolbar-action" @click="openKey()">
            <PlusOutlined /> 新建密钥
          </a-button>
        </div>

        <a-table
          :data-source="keys"
          :columns="keyColumns"
          row-key="id"
          :loading="keyLoading"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'key_prefix'">
              <code class="mono">{{ record.key_prefix }}</code>
            </template>
            <template v-else-if="column.key === 'enabled'">
              <a-tag :color="record.enabled ? 'success' : 'error'">
                {{ record.enabled ? '启用' : '停用' }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'last_used_at'">
              <span class="muted">{{ formatTime(record.last_used_at) }}</span>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" @click="openKey(record)">编辑</a-button>
                <a-popconfirm
                  :title="record.enabled ? '停用后这把密钥立刻无法调用，确认？' : '确认重新启用？'"
                  @confirm="toggleKey(record)"
                >
                  <a-button size="small">{{ record.enabled ? '停用' : '启用' }}</a-button>
                </a-popconfirm>
                <a-popconfirm title="删除后使用该密钥的调用会立刻失败，确认？" @confirm="removeKey(record)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ---- 调用日志 ---- -->
      <a-tab-pane key="logs" tab="调用日志">
        <div class="toolbar">
          <a-range-picker
            v-model:value="logRange"
            show-time
            value-format="YYYY-MM-DDTHH:mm:ss"
            size="small"
            :allow-clear="true"
          />
          <a-select
            v-model:value="logFilter.key_id"
            allow-clear
            size="small"
            class="filter-select"
            placeholder="密钥"
            :options="keyOptions"
          />
          <a-select
            v-model:value="logFilter.channel_id"
            allow-clear
            size="small"
            class="filter-select"
            placeholder="通道"
            :options="channelOptions"
          />
          <a-input
            v-model:value="logFilter.keyword"
            allow-clear
            size="small"
            class="filter-input"
            placeholder="模型 / 错误 / request id"
            @press-enter="searchLogs"
          />
          <a-select
            v-model:value="logFilter.success"
            allow-clear
            size="small"
            class="filter-select-sm"
            placeholder="结果"
            :options="[
              { value: true, label: '成功' },
              { value: false, label: '失败' },
            ]"
          />
          <a-button size="small" type="primary" @click="searchLogs">查询</a-button>
          <a-button size="small" @click="resetLogs">重置</a-button>
          <a-popconfirm
            :title="`清理 ${purgeDays} 天之前的调用日志？此操作不可撤销`"
            @confirm="purgeLogs"
          >
            <a-button size="small" danger class="toolbar-action">清理历史</a-button>
          </a-popconfirm>
        </div>

        <a-table
          :data-source="logs"
          :columns="logColumns"
          row-key="id"
          size="small"
          :loading="logLoading"
          :pagination="logPagination"
          :scroll="{ x: 'max-content' }"
          :custom-row="(record: AiCallLogItem) => ({ onClick: () => openLog(record) })"
          @change="onLogTableChange"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'success'">
              <a-tag :color="record.success ? 'success' : 'error'">
                {{ record.success ? record.status_code : `失败 ${record.status_code}` }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'model'">
              <code class="mono">{{ record.model || '—' }}</code>
              <a-tag v-if="record.stream" color="blue">流式</a-tag>
            </template>
            <template v-else-if="column.key === 'tokens'">
              {{ record.total_tokens.toLocaleString() }}
              <span class="muted">（{{ record.prompt_tokens }}+{{ record.completion_tokens }}）</span>
            </template>
            <template v-else-if="column.key === 'latency'">
              {{ record.latency_ms }} ms
              <span v-if="record.first_token_ms !== null" class="muted">
                / 首字 {{ record.first_token_ms }} ms
              </span>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span class="muted">{{ formatTime(record.created_at) }}</span>
            </template>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>

    <!-- 通道表单 -->
    <a-modal
      v-model:open="channelModal.open"
      :title="channelModal.id ? '编辑通道' : '新增通道'"
      :confirm-loading="channelModal.loading"
      ok-text="保存"
      cancel-text="取消"
      width="560px"
      @ok="submitChannel"
    >
      <a-form layout="vertical">
        <a-form-item label="通道名称" required>
          <a-input v-model:value="channelModal.form.name" placeholder="例如：火山方舟 / DeepSeek" />
        </a-form-item>
        <a-form-item label="base_url" required>
          <a-input v-model:value="channelModal.form.base_url" placeholder="https://api.deepseek.com/v1" />
          <div class="hint">OpenAI 兼容根地址，通常以 /v1 结尾，网关会在后面拼 /chat/completions。</div>
        </a-form-item>
        <a-form-item label="api_key">
          <a-input-password
            v-model:value="channelModal.form.api_key"
            :placeholder="channelModal.id ? '留空表示不修改' : '上游厂商的密钥，加密落库'"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="支持的模型名">
          <a-select
            v-model:value="channelModal.form.models"
            mode="tags"
            placeholder="回车添加，例如 deepseek-chat"
            :token-separators="[',']"
          />
          <div class="hint">仅用于配置路由时的候选提示，不参与转发判定。</div>
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="channelModal.form.enabled" />
        </a-form-item>
        <a-form-item label="备注">
          <a-input v-model:value="channelModal.form.remark" placeholder="选填" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 路由表单 -->
    <a-modal
      v-model:open="routeModal.open"
      :title="routeModal.id ? '编辑路由' : '新增路由'"
      :confirm-loading="routeModal.loading"
      ok-text="保存"
      cancel-text="取消"
      width="520px"
      @ok="submitRoute"
    >
      <a-form layout="vertical">
        <a-form-item label="对外模型名" required>
          <a-auto-complete
            v-model:value="routeModal.form.model_name"
            :options="knownModelOptions"
            placeholder="调用方在请求里写的 model，例如 gpt-4o"
          />
        </a-form-item>
        <a-form-item label="上游通道" required>
          <a-select
            v-model:value="routeModal.form.channel_id"
            :options="channelOptions"
            placeholder="选择通道"
            @change="onRouteChannelChange"
          />
        </a-form-item>
        <a-form-item label="上游模型名">
          <a-auto-complete
            v-model:value="routeModal.form.upstream_model"
            :options="upstreamModelOptions"
            placeholder="留空表示与对外模型名一致"
          />
        </a-form-item>
        <a-form-item label="优先级">
          <a-input-number v-model:value="routeModal.form.priority" :min="0" :max="9999" />
          <div class="hint">数字越小越先尝试；同名多通道时构成故障转移顺序。</div>
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="routeModal.form.enabled" />
        </a-form-item>
        <a-form-item label="备注">
          <a-input v-model:value="routeModal.form.remark" placeholder="选填" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 密钥表单 -->
    <a-modal
      v-model:open="keyModal.open"
      :title="keyModal.id ? '编辑密钥' : '新建密钥'"
      :confirm-loading="keyModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitKey"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="keyModal.form.name" placeholder="例如：客服系统 / 数据组" />
        </a-form-item>
        <a-form-item label="备注">
          <a-input v-model:value="keyModal.form.remark" placeholder="选填，写清这把钥匙给了谁" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 新密钥明文（只出现一次） -->
    <a-modal v-model:open="secretModal.open" title="密钥已创建" :footer="null" width="620px">
      <a-alert
        type="warning"
        show-icon
        message="明文只显示这一次"
        description="数据库里只保存摘要，关掉这个窗口之后无法再取回，请立刻复制并妥善保管。"
      />
      <div class="secret-row">
        <code class="mono secret-value">{{ secretModal.value }}</code>
        <a-button type="primary" @click="copy(secretModal.value)">复制</a-button>
      </div>
    </a-modal>

    <!-- 日志详情 -->
    <a-drawer v-model:open="logDrawer.open" title="调用详情" width="720" placement="right">
      <a-spin :spinning="logDrawer.loading">
        <template v-if="logDrawer.detail">
          <a-descriptions bordered size="small" :column="2">
            <a-descriptions-item label="request id" :span="2">
              <code class="mono">{{ logDrawer.detail.request_id }}</code>
            </a-descriptions-item>
            <a-descriptions-item label="端点">{{ logDrawer.detail.endpoint }}</a-descriptions-item>
            <a-descriptions-item label="结果">
              <a-tag :color="logDrawer.detail.success ? 'success' : 'error'">
                {{ logDrawer.detail.status_code }}
              </a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="对外模型">
              <code class="mono">{{ logDrawer.detail.model || '—' }}</code>
            </a-descriptions-item>
            <a-descriptions-item label="上游模型">
              <code class="mono">{{ logDrawer.detail.upstream_model || '—' }}</code>
            </a-descriptions-item>
            <a-descriptions-item label="通道">{{ logDrawer.detail.channel_name || '—' }}</a-descriptions-item>
            <a-descriptions-item label="密钥">{{ logDrawer.detail.key_name || '—' }}</a-descriptions-item>
            <a-descriptions-item label="Token">
              {{ logDrawer.detail.prompt_tokens }} 入 / {{ logDrawer.detail.completion_tokens }} 出
              （合计 {{ logDrawer.detail.total_tokens }}）
            </a-descriptions-item>
            <a-descriptions-item label="耗时">
              {{ logDrawer.detail.latency_ms }} ms
              <template v-if="logDrawer.detail.first_token_ms !== null">
                （首字 {{ logDrawer.detail.first_token_ms }} ms）
              </template>
            </a-descriptions-item>
            <a-descriptions-item label="来源 IP">{{ logDrawer.detail.client_ip || '—' }}</a-descriptions-item>
            <a-descriptions-item label="时间">{{ formatTime(logDrawer.detail.created_at) }}</a-descriptions-item>
            <a-descriptions-item v-if="logDrawer.detail.error" label="错误" :span="2">
              <span class="text-danger">{{ logDrawer.detail.error }}</span>
            </a-descriptions-item>
          </a-descriptions>

          <template v-if="logDrawer.detail.attempts?.length">
            <div class="drawer-section">故障转移轨迹</div>
            <a-table
              :data-source="logDrawer.detail.attempts"
              :columns="attemptColumns"
              row-key="channel_id"
              size="small"
              :pagination="false"
              :scroll="{ x: 'max-content' }"
            />
          </template>

          <div class="drawer-section">请求正文</div>
          <pre class="mono payload">{{ pretty(logDrawer.detail.request_body) }}</pre>
          <div class="drawer-section">响应正文</div>
          <pre class="mono payload">{{ pretty(logDrawer.detail.response_body) }}</pre>
        </template>
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Empty, message } from 'ant-design-vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import {
  aiGatewayApi,
  type AiApiKeyItem,
  type AiCallLogItem,
  type AiChannelItem,
  type AiGatewayOverview,
  type AiModelRouteItem,
  type AiStats,
  type AiStatsBucket,
} from '@/api'
import { copyText } from '@/utils/clipboard'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const tab = ref('overview')
const forbidden = ref(false)

const overview = ref<AiGatewayOverview | null>(null)
const channels = ref<AiChannelItem[]>([])
const routes = ref<AiModelRouteItem[]>([])
const keys = ref<AiApiKeyItem[]>([])

const baseUrl = `${window.location.origin}/v1`
const curlSample = `curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer <你的网关密钥>" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "<对外模型名>", "messages": [{"role": "user", "content": "你好"}]}'`

async function copy(text: string) {
  message.success((await copyText(text)) ? '已复制' : '复制失败，请手动选择')
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

// ---- 概览 -------------------------------------------------------------------

const stats = ref<AiStats | null>(null)
const statsDays = ref(7)
const statsLoading = ref(false)

const successRate = computed(() => {
  const totals = stats.value?.totals
  if (!totals?.calls) return '—'
  return `${((totals.success / totals.calls) * 100).toFixed(1)}%`
})

const dailyTrend = computed<AiStatsBucket[]>(() => [...(stats.value?.daily ?? [])].reverse())

const maxDailyCalls = computed(() =>
  Math.max(1, ...dailyTrend.value.map((point) => point.calls)),
)

function barHeight(calls: number): number {
  // 留 8% 的底线，否则 1 次调用的柱子矮到看不见。
  return Math.max(8, Math.round((calls / maxDailyCalls.value) * 100))
}

function shortDate(value: string): string {
  const parts = value.split('-')
  return parts.length === 3 ? `${parts[1]}-${parts[2]}` : value
}

const breakdownColumns = [
  { title: '名称', dataIndex: 'name', ellipsis: true },
  { title: '调用', dataIndex: 'calls', width: 70 },
  { title: '失败', key: 'failed', width: 70 },
  { title: 'Token', key: 'total_tokens', width: 100 },
  { title: '均耗时', dataIndex: 'avg_latency_ms', width: 90 },
]

const breakdowns = computed(() => [
  { title: '按通道', rows: stats.value?.by_channel ?? [] },
  { title: '按模型', rows: stats.value?.by_model ?? [] },
  { title: '按密钥', rows: stats.value?.by_key ?? [] },
])

async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await aiGatewayApi.stats(statsDays.value)
  } finally {
    statsLoading.value = false
  }
}

async function loadOverview() {
  overview.value = await aiGatewayApi.overview()
}

// ---- 通道 -------------------------------------------------------------------

const channelLoading = ref(false)
const testingId = ref<number | null>(null)

const channelColumns = [
  { title: '名称', dataIndex: 'name', width: 160 },
  { title: 'base_url', dataIndex: 'base_url', ellipsis: true },
  { title: 'api_key', key: 'api_key', width: 140 },
  { title: '模型', key: 'models', width: 260 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '连通性', key: 'last_test', width: 200 },
  { title: '操作', key: 'action', width: 200 },
]

const channelOptions = computed(() =>
  channels.value.map((channel) => ({ value: channel.id, label: channel.name })),
)

async function loadChannels() {
  channelLoading.value = true
  try {
    const result = await aiGatewayApi.channels()
    channels.value = result.items
  } finally {
    channelLoading.value = false
  }
}

const emptyChannelForm = () => ({
  name: '',
  base_url: '',
  api_key: '',
  models: [] as string[],
  enabled: true,
  remark: '',
})

const channelModal = reactive({
  open: false,
  loading: false,
  id: 0,
  maskedKey: '',
  form: emptyChannelForm(),
})

function openChannel(channel?: AiChannelItem) {
  channelModal.id = channel?.id ?? 0
  channelModal.maskedKey = channel?.api_key ?? ''
  channelModal.form = channel
    ? {
        name: channel.name,
        base_url: channel.base_url,
        // 掩码原样提交回去，后端会识别成「不修改」。
        api_key: channel.api_key,
        models: [...(channel.models ?? [])],
        enabled: channel.enabled,
        remark: channel.remark ?? '',
      }
    : emptyChannelForm()
  channelModal.open = true
}

async function submitChannel() {
  const form = channelModal.form
  if (!form.name.trim() || !form.base_url.trim()) {
    message.warning('请填写通道名称与 base_url')
    return
  }
  channelModal.loading = true
  try {
    const payload = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      api_key: form.api_key || null,
      models: form.models,
      enabled: form.enabled,
      remark: form.remark || null,
    }
    if (channelModal.id) {
      await aiGatewayApi.updateChannel(channelModal.id, payload)
      message.success('已保存')
    } else {
      await aiGatewayApi.createChannel(payload)
      message.success('通道已创建')
    }
    channelModal.open = false
    await Promise.all([loadChannels(), loadOverview()])
  } finally {
    channelModal.loading = false
  }
}

async function toggleChannel(channel: AiChannelItem, enabled: boolean) {
  await aiGatewayApi.updateChannel(channel.id, { enabled })
  message.success(enabled ? '已启用' : '已停用')
  await loadChannels()
}

async function testChannel(channel: AiChannelItem) {
  testingId.value = channel.id
  try {
    const result = await aiGatewayApi.testChannel(channel.id)
    if (result.success) message.success(result.message)
    else message.error(result.message)
  } catch (error) {
    message.error((error as { message?: string })?.message || '测试失败')
  } finally {
    testingId.value = null
    await loadChannels()
  }
}

async function removeChannel(channel: AiChannelItem) {
  await aiGatewayApi.removeChannel(channel.id)
  message.success('已删除')
  await Promise.all([loadChannels(), loadRoutes(), loadOverview()])
}

// ---- 路由 -------------------------------------------------------------------

const routeLoading = ref(false)
const routeFilter = ref('')

const routeColumns = [
  { title: '对外模型名', key: 'model_name', width: 220 },
  { title: '通道', key: 'channel_name', width: 160 },
  { title: '上游模型名', key: 'upstream_model', width: 220 },
  { title: '优先级', dataIndex: 'priority', width: 90 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
  { title: '操作', key: 'action', width: 150 },
]

// 已配过的对外模型名，新建路由时给候选，减少打字打错。
const knownModelOptions = computed(() =>
  [...new Set(routes.value.map((route) => route.model_name))].map((name) => ({ value: name })),
)

async function loadRoutes() {
  routeLoading.value = true
  try {
    const result = await aiGatewayApi.routes(routeFilter.value.trim() || undefined)
    routes.value = result.items
  } finally {
    routeLoading.value = false
  }
}

const emptyRouteForm = () => ({
  model_name: '',
  channel_id: undefined as number | undefined,
  upstream_model: '',
  priority: 100,
  enabled: true,
  remark: '',
})

const routeModal = reactive({ open: false, loading: false, id: 0, form: emptyRouteForm() })

const upstreamModelOptions = computed(() => {
  const channel = channels.value.find((item) => item.id === routeModal.form.channel_id)
  return (channel?.models ?? []).map((name) => ({ value: name }))
})

function openRoute(route?: AiModelRouteItem) {
  routeModal.id = route?.id ?? 0
  routeModal.form = route
    ? {
        model_name: route.model_name,
        channel_id: route.channel_id,
        upstream_model: route.upstream_model ?? '',
        priority: route.priority,
        enabled: route.enabled,
        remark: route.remark ?? '',
      }
    : emptyRouteForm()
  routeModal.open = true
}

// 选完通道后，如果上游模型名还空着，就顺手填成通道声明的第一个模型。
function onRouteChannelChange() {
  if (routeModal.form.upstream_model) return
  routeModal.form.upstream_model = upstreamModelOptions.value[0]?.value ?? ''
}

async function submitRoute() {
  const form = routeModal.form
  if (!form.model_name.trim()) {
    message.warning('请填写对外模型名')
    return
  }
  if (!form.channel_id) {
    message.warning('请选择上游通道')
    return
  }
  routeModal.loading = true
  try {
    const payload = {
      model_name: form.model_name.trim(),
      channel_id: form.channel_id,
      upstream_model: form.upstream_model.trim() || null,
      priority: form.priority,
      enabled: form.enabled,
      remark: form.remark || null,
    }
    if (routeModal.id) {
      await aiGatewayApi.updateRoute(routeModal.id, payload)
      message.success('已保存')
    } else {
      await aiGatewayApi.createRoute(payload)
      message.success('路由已创建')
    }
    routeModal.open = false
    await Promise.all([loadRoutes(), loadOverview()])
  } finally {
    routeModal.loading = false
  }
}

async function toggleRoute(route: AiModelRouteItem, enabled: boolean) {
  await aiGatewayApi.updateRoute(route.id, { enabled })
  await loadRoutes()
}

async function removeRoute(route: AiModelRouteItem) {
  await aiGatewayApi.removeRoute(route.id)
  message.success('已删除')
  await Promise.all([loadRoutes(), loadOverview()])
}

// ---- 密钥 -------------------------------------------------------------------

const keyLoading = ref(false)

const keyColumns = [
  { title: '名称', dataIndex: 'name', width: 180 },
  { title: '密钥', key: 'key_prefix', width: 200 },
  { title: '状态', key: 'enabled', width: 90 },
  { title: '调用次数', dataIndex: 'call_count', width: 100 },
  { title: '最近调用', key: 'last_used_at', width: 180 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
  { title: '操作', key: 'action', width: 200 },
]

const keyOptions = computed(() =>
  keys.value.map((row) => ({ value: row.id, label: row.name })),
)

async function loadKeys() {
  keyLoading.value = true
  try {
    const result = await aiGatewayApi.keys()
    keys.value = result.items
  } finally {
    keyLoading.value = false
  }
}

const keyModal = reactive({
  open: false,
  loading: false,
  id: 0,
  form: { name: '', remark: '' },
})

const secretModal = reactive({ open: false, value: '' })

function openKey(row?: AiApiKeyItem) {
  keyModal.id = row?.id ?? 0
  keyModal.form = row ? { name: row.name, remark: row.remark ?? '' } : { name: '', remark: '' }
  keyModal.open = true
}

async function submitKey() {
  const name = keyModal.form.name.trim()
  if (!name) {
    message.warning('请填写密钥名称')
    return
  }
  keyModal.loading = true
  try {
    if (keyModal.id) {
      await aiGatewayApi.updateKey(keyModal.id, {
        name,
        remark: keyModal.form.remark || null,
      })
      message.success('已保存')
    } else {
      const created = await aiGatewayApi.createKey({
        name,
        remark: keyModal.form.remark || null,
      })
      secretModal.value = created.api_key
      secretModal.open = true
    }
    keyModal.open = false
    await Promise.all([loadKeys(), loadOverview()])
  } finally {
    keyModal.loading = false
  }
}

async function toggleKey(row: AiApiKeyItem) {
  await aiGatewayApi.updateKey(row.id, { enabled: !row.enabled })
  message.success(row.enabled ? '已停用' : '已启用')
  await loadKeys()
}

async function removeKey(row: AiApiKeyItem) {
  await aiGatewayApi.removeKey(row.id)
  message.success('已删除')
  await Promise.all([loadKeys(), loadOverview()])
}

// ---- 调用日志 ---------------------------------------------------------------

const logs = ref<AiCallLogItem[]>([])
const logLoading = ref(false)
const logRange = ref<[string, string] | null>(null)
const purgeDays = ref(30)
const logFilter = reactive<{
  key_id?: number
  channel_id?: number
  keyword?: string
  success?: boolean
}>({})
const logPage = reactive({ page: 1, pageSize: 20, total: 0 })

const logPagination = computed(() => ({
  current: logPage.page,
  pageSize: logPage.pageSize,
  total: logPage.total,
  showSizeChanger: true,
  showTotal: (total: number) => `共 ${total} 条`,
}))

const logColumns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '密钥', dataIndex: 'key_name', width: 130, ellipsis: true },
  { title: '模型', key: 'model', width: 200 },
  { title: '通道', dataIndex: 'channel_name', width: 140, ellipsis: true },
  { title: '结果', key: 'success', width: 110 },
  { title: 'Token', key: 'tokens', width: 160 },
  { title: '耗时', key: 'latency', width: 170 },
  { title: '来源', dataIndex: 'client_ip', width: 130 },
]

const attemptColumns = [
  { title: '通道', dataIndex: 'channel_name' },
  { title: '状态码', dataIndex: 'status_code', width: 90 },
  { title: '耗时(ms)', dataIndex: 'latency_ms', width: 100 },
  { title: '错误', dataIndex: 'error', ellipsis: true },
]

async function loadLogs() {
  logLoading.value = true
  try {
    const result = await aiGatewayApi.logs({
      page: logPage.page,
      page_size: logPage.pageSize,
      key_id: logFilter.key_id,
      channel_id: logFilter.channel_id,
      keyword: logFilter.keyword?.trim() || undefined,
      success: logFilter.success,
      start: logRange.value?.[0],
      end: logRange.value?.[1],
    })
    logs.value = result.items
    logPage.total = result.total
  } finally {
    logLoading.value = false
  }
}

function searchLogs() {
  logPage.page = 1
  return loadLogs()
}

function resetLogs() {
  logRange.value = null
  logFilter.key_id = undefined
  logFilter.channel_id = undefined
  logFilter.keyword = ''
  logFilter.success = undefined
  return searchLogs()
}

function onLogTableChange(pagination: { current?: number; pageSize?: number }) {
  logPage.page = pagination.current ?? 1
  logPage.pageSize = pagination.pageSize ?? 20
  loadLogs()
}

const logDrawer = reactive({
  open: false,
  loading: false,
  detail: null as null | (AiCallLogItem & {
    attempts: { channel_id: number; channel_name: string; status_code: number; error: string | null; latency_ms: number }[] | null
    request_body: string | null
    response_body: string | null
  }),
})

async function openLog(row: AiCallLogItem) {
  logDrawer.open = true
  logDrawer.loading = true
  logDrawer.detail = null
  try {
    logDrawer.detail = await aiGatewayApi.logDetail(row.id)
  } finally {
    logDrawer.loading = false
  }
}

/** 正文入库时是紧凑 JSON，展开成缩进版才好读；不是 JSON 就原样返回。 */
function pretty(value: string | null): string {
  if (!value) return '（未留存）'
  try {
    return JSON.stringify(JSON.parse(value), null, 2)
  } catch {
    return value
  }
}

async function purgeLogs() {
  const result = await aiGatewayApi.purgeLogs(purgeDays.value)
  message.success(`已清理 ${result.deleted} 条`)
  await Promise.all([loadLogs(), loadOverview()])
}

// ---- 载入 -------------------------------------------------------------------

function onTabChange(key: string | number) {
  // 概览的统计和日志列表都不便宜，切到对应页签时才拉。
  if (key === 'overview') loadStats()
  if (key === 'logs') loadLogs()
}

onMounted(async () => {
  if (!auth.userInfo) {
    await auth.fetchCurrentUser()
  }
  if (!auth.isAdmin) {
    forbidden.value = true
    return
  }
  await Promise.all([loadOverview(), loadChannels(), loadRoutes(), loadKeys(), loadStats()])
})
</script>

<style scoped>
.toolbar {
  justify-content: flex-start;
}
/* 主操作按钮靠右，与用户管理页的 toolbar 布局一致。 */
.toolbar-action {
  margin-left: auto;
}
.filter-input {
  width: 200px;
}
.filter-select {
  width: 150px;
}
.filter-select-sm {
  width: 100px;
}
.text-danger {
  color: #cf1322;
}

/* ---- 概览 ---- */
.access-card {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  padding: 16px;
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.access-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.access-label {
  color: var(--text-3);
  font-size: 12px;
  min-width: 132px;
}
.access-value {
  font-size: 13px;
  background: var(--paper);
  padding: 2px 8px;
  border-radius: var(--radius-sm);
}
.access-snippet {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
  flex: 1;
  min-width: 260px;
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stat-card {
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: var(--text-1);
}
.stat-label {
  color: var(--text-3);
  font-size: 12px;
  margin-top: 4px;
}
.trend {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 140px;
  padding: 8px 0;
  overflow-x: auto;
}
.trend-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  min-width: 34px;
  flex: 1;
}
.trend-bars {
  height: 104px;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.trend-bar {
  width: 60%;
  max-width: 26px;
  background: var(--signal-text);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  transition: height 0.2s ease;
}
.trend-label {
  font-size: 11px;
  color: var(--text-3);
}
.breakdown {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-top: 16px;
}
.breakdown-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

/* ---- 密钥明文 ---- */
.secret-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
.secret-value {
  flex: 1;
  padding: 8px 10px;
  background: var(--paper);
  border-radius: var(--radius-sm);
  word-break: break-all;
  font-size: 13px;
}

/* ---- 日志抽屉 ---- */
.drawer-section {
  font-size: 13px;
  font-weight: 600;
  margin: 16px 0 8px;
}
.payload {
  margin: 0;
  padding: 12px;
  background: var(--paper);
  border-radius: var(--radius-md);
  font-size: 12px;
  line-height: 1.6;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
