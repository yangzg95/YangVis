<template>
  <div class="page-shell">
    <div class="login-panel">
      <aside class="login-brand">
        <div class="login-brand-head">
          <img class="login-brand-logo" :src="yangvisLoginLogo" alt="杨维斯控制台" />
        </div>
        <div class="login-brand-middle">
          <span class="login-brand-signal"></span>
          <p class="login-brand-tagline">杨维斯智能控制台</p>
        </div>
        <div class="login-brand-foot">
          <p class="login-brand-sub">对话、知识库、服务器与数据库运维，一处完成。</p>
        </div>
      </aside>

      <main class="login-main">
        <img class="login-main-mark" :src="yangvisMark" alt="" aria-hidden="true" />
      </main>
    </div>

    <div class="login-main-inner">
      <div class="login-header">
        <h1 class="login-title">登录</h1>
        <p class="login-subtitle">使用管理员分配的账号登录</p>
      </div>

      <div class="login-form" @keydown.enter="handleSubmit">
        <a-input
          v-model:value="account"
          size="large"
          placeholder="请输入用户名"
          autocomplete="username"
        >
          <template #prefix><UserOutlined /></template>
        </a-input>
        <a-input-password
          v-model:value="password"
          size="large"
          placeholder="请输入密码"
          autocomplete="current-password"
          @keydown="syncCapsLock"
          @keyup="syncCapsLock"
        >
          <template #prefix><LockOutlined /></template>
        </a-input-password>
        <!-- 密码不显示明文，开着大写锁定连错几次很难自己发现。 -->
        <div v-if="capsLockOn" class="caps-hint">
          <WarningOutlined /> 大写锁定（Caps Lock）已开启
        </div>
      </div>

      <div v-if="errorMessage" class="form-error">{{ errorMessage }}</div>

      <div style="margin-top: 18px">
        <a-button
          class="login-submit"
          size="large"
          :loading="submitting"
          @click="handleSubmit"
        >
          登录系统
        </a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LockOutlined, UserOutlined, WarningOutlined } from '@ant-design/icons-vue'
import yangvisLoginLogo from '@/assets/yangvis-logo-login.svg'
import yangvisMark from '@/assets/yangvis-mark.svg'
import { useAuthStore } from '@/stores/auth'
import { isAuthenticated } from '@/utils/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const account = ref('')
const password = ref('')
const errorMessage = ref('')
const submitting = ref(false)
const capsLockOn = ref(false)

// keydown/keyup 都同步一次：各浏览器在 CapsLock 按下那一刻上报状态的时机不一致。
const syncCapsLock = (e: KeyboardEvent) => {
  capsLockOn.value = e.getModifierState?.('CapsLock') ?? false
}

onMounted(() => {
  if (isAuthenticated()) {
    router.replace('/')
  }
})

const handleSubmit = async () => {
  if (submitting.value) return
  if (!account.value.trim() || !password.value.trim()) {
    errorMessage.value = '请输入用户名和密码'
    return
  }

  submitting.value = true
  errorMessage.value = ''
  try {
    await auth.login(account.value.trim(), password.value.trim())
    await auth.fetchCurrentUser()
    const redirect = route.query.redirect
    router.replace(typeof redirect === 'string' && redirect ? redirect : '/')
  } catch (error) {
    errorMessage.value = (error as { message?: string })?.message || '登录失败，请稍后再试'
  } finally {
    submitting.value = false
  }
}
</script>