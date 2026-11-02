<template>
  <div class="chat-page">
    <header class="chat-topbar">
      <div class="topbar-left">
        <button type="button" class="icon-btn" aria-label="返回列表" @click="back">
          <ArrowLeftOutlined />
        </button>
        <template v-if="server">
          <span class="server-name">{{ server.name }}</span>
          <span class="server-addr">{{ address }}</span>
          <a-tag :color="server.last_check_ok ? 'green' : 'default'">
            {{ server.last_check_ok ? '连接正常' : '未验证' }}
          </a-tag>
        </template>
        <span v-else class="server-name muted">{{ loadError || '加载中…' }}</span>
      </div>
      <div class="topbar-right">
        <a-button v-if="!isMobile" size="small" @click="openTerminal">打开终端</a-button>
        <a-button size="small" @click="back">返回列表</a-button>
      </div>
    </header>

    <div class="chat-main">
      <div class="chat-card">
        <OpsChat
          v-if="server"
          target="server"
          :dark="themeStore.isDark"
          :target-id="serverId"
          placeholder="例如：磁盘为什么满了，哪个进程占的"
          :empty-hint="loadError || '正在加载服务器信息…'"
          :samples="CHAT_SAMPLES"
          @send-command="onSendCommand"
        />
        <div v-else-if="loadError" class="page-error">
          <CloudServerOutlined class="page-error-icon" />
          <div>{{ loadError }}</div>
          <a-button size="small" @click="back">返回列表</a-button>
        </div>
      </div>
    </div>

    <footer class="chat-statusbar">
      <span>AI 先做只读诊断；需要改动的命令会停下来等你确认</span>
      <span v-if="isMobile" class="statusbar-hint">手机端不提供终端，命令执行请在电脑上完成</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
/**
 * 服务器 AI 问答页：与「数据库问答」对称的独立页面。
 *
 * 后端 chat_ops 会为这次回答自己开 SSH 连接，不依赖终端页的会话，所以手机端
 * 也能正常诊断；写命令仍走「待确认」卡片，确认后由后端执行。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message as toast } from 'ant-design-vue'
import { ArrowLeftOutlined, CloudServerOutlined } from '@ant-design/icons-vue'
import OpsChat from '@/components/OpsChat.vue'
import { opsApi, type OpsServer } from '@/api'
import { useThemeStore } from '@/stores/theme'
import { useIsMobile } from '@/utils/useIsMobile'

const CHAT_SAMPLES = [
  '磁盘占用最高的几个目录是什么',
  '现在负载高吗，是哪个进程占的',
  '最近有没有异常的登录记录',
]

const route = useRoute()
const router = useRouter()
const themeStore = useThemeStore()
const isMobile = useIsMobile()

// 路由有 \d+ 约束，到这里一定是数字。
const serverId = Number(route.params.id)

const server = ref<OpsServer | null>(null)
const loadError = ref('')

const address = computed(() =>
  server.value ? `${server.value.username}@${server.value.host}:${server.value.port}` : '',
)

onMounted(async () => {
  // 没有单条 GET，台账量小，从列表里挑这一条（与终端页同一模式）。
  try {
    const res = await opsApi.listServers()
    server.value = res.items.find((item) => item.id === serverId) ?? null
    if (!server.value) loadError.value = '服务器不存在或已被删除'
  } catch {
    loadError.value = '服务器信息加载失败'
  }
})

function back() {
  void router.push({ name: 'OpsServer' })
}

function openTerminal() {
  void router.push({ name: 'OpsServerTerminal', params: { id: serverId } })
}

function onSendCommand() {
  toast.info('命令片段请到电脑端的服务器终端里执行')
}
</script>

<style scoped>
/* 项目没有全局盒模型重置，本页统一 border-box（与全屏终端、数据库问答页一致）。 */
.chat-page,
.chat-page * {
  box-sizing: border-box;
}

.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  background: var(--paper);
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

.server-name {
  font-size: var(--fs-base);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.server-name.muted {
  color: var(--text-3);
  font-weight: 400;
}

.server-addr {
  overflow: hidden;
  color: var(--text-3);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-main {
  display: flex;
  flex: 1;
  min-height: 0;
  padding: 12px;
}

.chat-card {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}

.page-error {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-3);
}

.page-error-icon {
  color: var(--text-3);
  font-size: var(--fs-5xl);
}

.chat-statusbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
  min-height: 32px;
  padding: 6px 16px;
  border-top: 1px solid var(--hairline);
  background: var(--surface);
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.statusbar-hint {
  color: var(--text-3);
  text-align: right;
}

@media (max-width: 768px) {
  .chat-topbar {
    padding: 0 8px;
  }

  .server-addr {
    display: none;
  }

  /* 左上角已有返回箭头，右上角的文字按钮重复且占宽度。 */
  .topbar-right {
    display: none;
  }

  .chat-main {
    padding: 8px;
  }

  .chat-card {
    padding: 8px;
  }

  .chat-statusbar {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 6px 10px calc(6px + env(safe-area-inset-bottom));
  }
}
</style>
