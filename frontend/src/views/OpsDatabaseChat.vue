<template>
  <div class="chat-page">
    <header class="chat-topbar">
      <div class="topbar-left">
        <a-tooltip title="返回列表">
          <button type="button" class="icon-btn" @click="back">
            <ArrowLeftOutlined />
          </button>
        </a-tooltip>
        <template v-if="conn">
          <span class="conn-name">{{ conn.name }}</span>
          <span class="conn-addr">{{ address }}</span>
          <a-tag :color="conn.writable ? 'orange' : 'blue'">{{ conn.writable ? '可写' : '只读' }}</a-tag>
        </template>
        <span v-else class="conn-name muted">{{ loadError || '加载中…' }}</span>
      </div>
      <div class="topbar-right">
        <a-button size="small" @click="back">返回列表</a-button>
      </div>
    </header>

    <div class="chat-main">
      <div class="stage">
        <!-- v-if=conn：连接信息没加载完之前不发任何查询/元数据请求。 -->
        <DbQueryTab
          v-if="conn"
          ref="queryRef"
          :conn-id="connId"
          :db-type="conn.db_type"
          :conn-name="conn.name"
          :writable="conn.writable"
          :schema="conn.db_name ?? undefined"
          @ask-ai="onAskAi"
        />
        <div v-else-if="loadError" class="stage-error">
          <DatabaseOutlined class="stage-error-icon" />
          <div class="stage-error-text">{{ loadError }}</div>
          <a-button size="small" @click="back">返回列表</a-button>
        </div>
      </div>

      <!-- 收起后右缘留一根竖条作为展开入口，和工作台页两侧面板的模式一致。 -->
      <div v-if="!showAi" class="rail" @click="showAi = true">
        <LeftOutlined />
        <span class="rail-text">AI 问答</span>
      </div>

      <!-- 藏在缝隙里的拖拽条：负边距叠在 gap 上，不改变布局（与工作台页同款）。 -->
      <div v-if="showAi" class="col-resizer" @mousedown="startAiDrag" />

      <!-- v-show 而不是 v-if：折叠只是藏起来，会话和 WebSocket 都保住。 -->
      <aside v-show="showAi" class="ai-panel" :style="{ width: `${aiWidth}px` }">
        <div class="ai-head">
          <span>AI 问答</span>
          <a-tooltip title="收起">
            <button type="button" class="icon-btn" @click="showAi = false">
              <RightOutlined />
            </button>
          </a-tooltip>
        </div>
        <div class="ai-body">
          <OpsChat
            ref="chatRef"
            target="database"
            :target-id="conn ? connId : null"
            placeholder="例如：哪张表最近增长最快"
            :empty-hint="loadError || '正在加载连接信息…'"
            :samples="CHAT_SAMPLES"
            @open-query="onChatSql"
          />
        </div>
      </aside>
    </div>

    <footer class="chat-statusbar">
      <span v-if="conn?.writable">此连接已开启写入：控制台可执行 DML/DDL，高危命令仍会被拦截；AI 通道始终只读</span>
      <span v-else>此连接是只读的：写操作会被安全网关直接拒绝</span>
      <span class="statusbar-hint">AI 给出的 SQL 只填入编辑器，确认无误后再手动执行</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons-vue'
import OpsChat from '@/components/OpsChat.vue'
import DbQueryTab from '@/components/ops/DbQueryTab.vue'
import { clampSize, readSize, saveSize, startDragResize } from '@/components/ops/resizer'
import '@/components/ops/resizer.css'
import { opsApi, type OpsDatabase } from '@/api'
import { storageKeys } from '@/utils/storage'

const CHAT_SAMPLES = [
  '这个库里最大的几张表是什么',
  '有没有正在跑的慢查询',
  '订单表的结构是怎样的',
]

const route = useRoute()
const router = useRouter()
// 路由有 \d+ 约束，到这里一定是数字。
const connId = Number(route.params.id)

const conn = ref<OpsDatabase | null>(null)
const loadError = ref('')
const showAi = ref(true)
const queryRef = ref<InstanceType<typeof DbQueryTab> | null>(null)

// ---- AI 面板拖拽调宽 -----------------------------------------------------------
// 默认 460px，比初版 400 宽一档；宽度偏好记 localStorage，与工作台页两侧面板
// 同一套机制。
const aiWidth = ref(readSize(storageKeys.opsDbAiWidth, 460))

function startAiDrag(event: MouseEvent) {
  const startWidth = aiWidth.value
  startDragResize(
    event,
    'x',
    // 面板在右侧：往左拖变宽，位移取反。
    (delta) => { aiWidth.value = clampSize(startWidth - delta, 320, 760) },
    () => saveSize(storageKeys.opsDbAiWidth, aiWidth.value),
  )
}

const address = computed(() => {
  const item = conn.value
  if (!item) return ''
  const suffix =
    item.db_type === 'mysql' ? (item.db_name ? `/${item.db_name}` : '') : `/db${item.db_name || '0'}`
  return `${item.db_type} · ${item.host}:${item.port}${suffix}`
})

onMounted(async () => {
  // 没有单条 GET，台账量小，从列表里挑这一条（与服务器终端页同一模式）。
  try {
    const res = await opsApi.listDatabases()
    conn.value = res.items.find((item) => item.id === connId) ?? null
    if (!conn.value) loadError.value = '连接不存在或已被删除'
  } catch {
    loadError.value = '连接信息加载失败'
  }
})

function back() {
  router.push({ name: 'OpsDatabase' })
}

/** AI 回答里 SQL 代码块的「在查询框中打开」：填进编辑器，不自动执行。 */
function onChatSql(sql: string) {
  queryRef.value?.setSql(sql)
}

const chatRef = ref<InstanceType<typeof OpsChat> | null>(null)

/** 查询页「询问 AI」：面板是 v-show 常驻的，展开后直接发，不用等挂载。 */
function onAskAi(payload: { connId: number; sql: string; error?: string; schema?: string }) {
  showAi.value = true
  // 带上连接和默认库上下文，AI 才知道这条 SQL 是跑在哪个库上的。
  const where = `连接「${conn.value?.name ?? payload.connId}」${payload.schema ? ` 的库 \`${payload.schema}\`` : ''}`
  const prompt = payload.error
    ? `在${where}上执行这条 SQL 报错了，帮我分析原因并给出修正：\n\`\`\`sql\n${payload.sql || '（空）'}\n\`\`\`\n错误信息：${payload.error}`
    : `帮我解释这条 SQL（${where}）：\n\`\`\`sql\n${payload.sql}\n\`\`\``
  void chatRef.value?.ask(prompt)
}
</script>

<style scoped>
/* 项目没有全局盒模型重置，显式高度/拉伸 + padding 的元素在 content-box 下会
   比预期高出一截。本页统一 border-box（与全屏终端页同样的处理）。 */
.chat-page,
.chat-page * {
  box-sizing: border-box;
}

.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f5f6f8;
}

.chat-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 48px;
  flex-shrink: 0;
  padding: 0 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--hairline);
}

.topbar-left,
.topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.conn-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-name.muted {
  color: var(--text-3);
  font-weight: 400;
}

.conn-addr {
  color: var(--text-3);
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
}

/* .icon-btn 用全局共享类（style.css）。 */

.chat-main {
  display: flex;
  flex: 1;
  min-height: 0;
  padding: 12px;
  gap: 12px;
}

/* 中央查询台：与全站 .page-card 同一套语言——发丝线描边 + 卡片级圆角，无投影。 */
.stage {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}

.stage-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex: 1;
  color: var(--text-3);
}

.stage-error-icon {
  font-size: 36px;
  color: rgba(0, 0, 0, 0.15);
}

.stage-error-text {
  font-size: 13px;
}

/* AI 面板收起后的展开入口用全局 .rail / .rail-text（style.css）。 */

/* 拖拽条：12px 热区用负边距叠在 flex gap 上，视觉上不占位；
   悬停 / 拖拽（body.dragging-col）时中间亮出一条 2px 的线。 */
.col-resizer {
  flex-shrink: 0;
  width: 12px;
  margin: 0 -12px;
  display: flex;
  justify-content: center;
  cursor: col-resize;
  z-index: 5;
}

.col-resizer::after {
  content: '';
  width: 2px;
  border-radius: 1px;
  background: transparent;
  transition: background 0.15s;
}

.col-resizer:hover::after,
body.dragging-col .col-resizer::after {
  background: var(--signal-border, #91caff);
}

/* 宽度由模板里的内联样式给（可拖拽），这里只定布局。 */
.ai-panel {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  min-height: 0;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}

/* 窄屏：查询台与 AI 面板上下堆叠、各自定高，整页滚动；顶栏地址让位给名称。
   展开竖条转成横条（scoped 特异性高于全局 .rail）。 */
@media (max-width: 900px) {
  .conn-addr {
    display: none;
  }
  .chat-main {
    flex-direction: column;
    overflow-y: auto;
  }
  .stage {
    flex: none;
    height: 60vh;
  }
  /* 内联宽度优先级高于样式表，窄屏堆叠时必须 !important 压掉它。 */
  .ai-panel {
    flex: none;
    width: auto !important;
    height: 60vh;
  }
  .col-resizer {
    display: none;
  }
  .rail {
    flex-direction: row;
    width: auto;
    height: 36px;
    padding: 0 12px;
  }
  .rail-text {
    writing-mode: horizontal-tb;
  }
}

.ai-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--hairline);
  font-size: 13px;
  font-weight: 600;
}

.ai-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 8px 12px 12px;
}

.chat-statusbar {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 28px;
  flex-shrink: 0;
  padding: 0 16px;
  background: var(--surface);
  border-top: 1px solid var(--hairline);
  color: var(--text-3);
  font-size: 12px;
}

.statusbar-hint {
  margin-left: auto;
  white-space: nowrap;
}
</style>
