<template>
  <div class="page-card">
    <div class="page-title">用户管理</div>

    <a-result
      v-if="forbidden"
      status="403"
      title="需要管理员权限"
      sub-title="只有管理员可以查看和管理账号。"
    />

    <template v-else>
      <div class="toolbar">
        <a-input
          v-model:value="keyword"
          allow-clear
          class="search-input"
          placeholder="搜索用户名 / 昵称 / 邮箱"
        >
          <template #prefix><SearchOutlined /></template>
        </a-input>
        <a-button type="primary" @click="openCreate">
          <PlusOutlined /> 新增用户
        </a-button>
      </div>

      <a-table
        :data-source="visibleUsers"
        :columns="columns"
        row-key="id"
        :loading="loading"
        size="middle"
        :pagination="{ pageSize: 10 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'username'">
            <span>{{ record.username }}</span>
            <a-tag v-if="record.id === currentUserId" color="blue" class="self-tag">当前登录</a-tag>
          </template>

          <template v-else-if="column.key === 'role'">
            <a-tag :color="record.is_admin ? 'gold' : 'default'">
              {{ record.is_admin ? '管理员' : '普通用户' }}
            </a-tag>
            <a-tag v-if="record.ops_write && !record.is_admin" color="cyan">运维写</a-tag>
          </template>

          <template v-else-if="column.key === 'status'">
            <a-tag :color="record.status ? 'success' : 'error'">
              {{ record.status ? '启用' : '禁用' }}
            </a-tag>
          </template>

          <template v-else-if="column.key === 'last_login_at'">
            <span class="muted">{{ formatTime(record.last_login_at) }}</span>
          </template>

          <template v-else-if="column.key === 'created_at'">
            <span class="muted">{{ formatTime(record.created_at) }}</span>
          </template>

          <template v-else-if="column.key === 'action'">
            <a-space>
              <a-button size="small" @click="openEdit(record)">编辑</a-button>
              <a-tooltip :title="record.id === currentUserId ? '不能禁用当前登录的账号' : ''">
                <a-popconfirm
                  :title="record.status ? '确认禁用该账号？禁用后将无法登录。' : '确认启用该账号？'"
                  :disabled="record.id === currentUserId"
                  @confirm="onToggleStatus(record)"
                >
                  <a-button size="small" :disabled="record.id === currentUserId">
                    {{ record.status ? '禁用' : '启用' }}
                  </a-button>
                </a-popconfirm>
              </a-tooltip>
              <a-button size="small" @click="openReset(record)">重置密码</a-button>
            </a-space>
          </template>
        </template>
      </a-table>
    </template>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑用户' : '新增用户'"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submit"
    >
      <a-form layout="vertical">
        <a-form-item label="用户名" required>
          <a-input
            v-model:value="modal.form.username"
            :disabled="modal.editing"
            placeholder="登录用的账号"
            autocomplete="off"
          />
          <div v-if="modal.editing" class="hint">用户名是登录凭据，创建后不可修改。</div>
        </a-form-item>
        <a-form-item v-if="!modal.editing" label="初始密码" required>
          <a-input-password
            v-model:value="modal.form.password"
            placeholder="至少 8 位"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="昵称">
          <a-input v-model:value="modal.form.nickname" placeholder="留空则与用户名相同" />
        </a-form-item>
        <a-form-item label="邮箱">
          <a-input v-model:value="modal.form.email" placeholder="选填" />
        </a-form-item>
        <a-form-item label="管理员">
          <a-switch
            v-model:checked="modal.form.is_admin"
            :disabled="modal.editing && modal.form.id === currentUserId"
          />
          <div class="hint">
            管理员可以管理账号与运维台账；模型配置、知识库等数据仍然按用户各自隔离。
          </div>
        </a-form-item>
        <a-form-item label="运维写权限">
          <a-switch
            v-model:checked="modal.form.ops_write"
            :disabled="modal.editing && modal.form.id === currentUserId"
          />
          <div class="hint">
            允许对共享运维资产产生变更：确认 AI 提议的写命令、打开服务器终端、
            上传/删除文件、在可写数据库连接上执行写语句。管理员天然拥有全部权限，
            无需勾选。
          </div>
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="reset.open"
      :title="`重置密码 · ${reset.username}`"
      :confirm-loading="reset.loading"
      ok-text="重置"
      cancel-text="取消"
      @ok="submitReset"
    >
      <a-form layout="vertical">
        <a-form-item label="新密码" required>
          <a-input-password
            v-model:value="reset.password"
            placeholder="至少 8 位"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="确认新密码" required>
          <a-input-password v-model:value="reset.confirm" autocomplete="new-password" />
        </a-form-item>
      </a-form>
      <div class="hint">
        重置后该用户已签发的令牌仍然有效，直到自然过期。
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { usersApi, type UserItem } from '@/api'
import { useAuthStore } from '@/stores/auth'

const MIN_PASSWORD_LENGTH = 8

const auth = useAuthStore()

const users = ref<UserItem[]>([])
const loading = ref(false)
const forbidden = ref(false)
const keyword = ref('')

const currentUserId = computed(() => auth.userInfo?.user_id ?? null)

const columns = [
  { title: '用户名', key: 'username', dataIndex: 'username' },
  { title: '昵称', dataIndex: 'nickname', customRender: ({ text }: { text: string | null }) => text || '—' },
  { title: '邮箱', dataIndex: 'email', ellipsis: true, customRender: ({ text }: { text: string | null }) => text || '—' },
  { title: '角色', key: 'role', width: 110 },
  { title: '状态', key: 'status', width: 90 },
  { title: '最近登录', key: 'last_login_at', width: 180 },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'action', width: 240 },
]

// 用户量不大，搜索放在前端做，省掉一轮请求。
const visibleUsers = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter((user) =>
    [user.username, user.nickname, user.email].some((field) =>
      (field || '').toLowerCase().includes(q),
    ),
  )
})

const emptyForm = () => ({
  id: 0,
  username: '',
  password: '',
  nickname: '',
  email: '',
  is_admin: false,
  ops_write: false,
})

const modal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: emptyForm(),
})

const reset = reactive({
  open: false,
  loading: false,
  id: 0,
  username: '',
  password: '',
  confirm: '',
})

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

async function load() {
  loading.value = true
  try {
    const result = await usersApi.list()
    users.value = result.items
  } finally {
    loading.value = false
  }
}

function openCreate() {
  modal.editing = false
  modal.form = emptyForm()
  modal.open = true
}

function openEdit(user: UserItem) {
  modal.editing = true
  modal.form = {
    id: user.id,
    username: user.username,
    password: '',
    nickname: user.nickname || '',
    email: user.email || '',
    is_admin: user.is_admin,
    ops_write: user.ops_write,
  }
  modal.open = true
}

async function submit() {
  const username = modal.form.username.trim()
  const nickname = modal.form.nickname.trim()
  const email = modal.form.email.trim()

  if (!modal.editing) {
    if (!username) {
      message.warning('请填写用户名')
      return
    }
    if (modal.form.password.length < MIN_PASSWORD_LENGTH) {
      message.warning(`密码至少 ${MIN_PASSWORD_LENGTH} 位`)
      return
    }
  }

  modal.loading = true
  try {
    if (modal.editing) {
      await usersApi.update(modal.form.id, {
        nickname: nickname || null,
        email: email || null,
        is_admin: modal.form.is_admin,
        ops_write: modal.form.ops_write,
      })
      message.success('已保存')
    } else {
      await usersApi.create({
        username,
        password: modal.form.password,
        nickname: nickname || undefined,
        email: email || undefined,
        is_admin: modal.form.is_admin,
        ops_write: modal.form.ops_write,
      })
      message.success('用户已创建')
    }
    modal.open = false
    await load()
  } finally {
    modal.loading = false
  }
}

async function onToggleStatus(user: UserItem) {
  await usersApi.setStatus(user.id, !user.status)
  message.success(user.status ? '已禁用' : '已启用')
  await load()
}

function openReset(user: UserItem) {
  reset.id = user.id
  reset.username = user.username
  reset.password = ''
  reset.confirm = ''
  reset.open = true
}

async function submitReset() {
  if (reset.password.length < MIN_PASSWORD_LENGTH) {
    message.warning(`密码至少 ${MIN_PASSWORD_LENGTH} 位`)
    return
  }
  if (reset.password !== reset.confirm) {
    message.warning('两次输入的密码不一致')
    return
  }

  reset.loading = true
  try {
    await usersApi.resetPassword(reset.id, reset.password)
    message.success('密码已重置')
    reset.open = false
  } finally {
    reset.loading = false
  }
}

onMounted(async () => {
  // 刷新页面时 store 里还没有资料，先补一次再判断，否则会误报无权限。
  if (!auth.userInfo) {
    await auth.fetchCurrentUser()
  }
  if (!auth.isAdmin) {
    forbidden.value = true
    return
  }
  await load()
})
</script>

<style scoped>
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.toolbar {
  margin-bottom: 16px;
  justify-content: space-between;
}
.search-input {
  max-width: 280px;
}
.self-tag {
  margin-left: 8px;
}
/* .muted 用全局共享类；.hint 这里只留本页的 margin 差异（style.css）。 */
.hint {
  margin-top: 4px;
}
</style>
