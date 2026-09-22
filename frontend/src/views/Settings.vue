<template>
  <div class="page-card">
    <div class="page-title">通用设置</div>

    <a-tabs v-model:activeKey="activeTab">
      <!-- ================= 模型管理 ================= -->
      <a-tab-pane key="models" tab="模型管理">
        <a-alert
          v-if="!loading && !status.embedding.ready"
          type="warning"
          show-icon
          class="gate-alert"
          message="未配置可用的向量模型"
          description="对话与知识库检索依赖向量模型。请添加一个用途为「向量」的模型，并通过连通性测试。"
        >
          <template #action>
            <a-button size="small" type="primary" @click="openCreate('embedding')">
              立即添加
            </a-button>
          </template>
        </a-alert>

        <a-alert
          v-if="!loading && !status.chat.ready"
          type="warning"
          show-icon
          class="gate-alert"
          message="未配置可用的对话模型"
          description="对话功能需要一个已通过测试的对话模型才能回答问题。"
        >
          <template #action>
            <a-button size="small" type="primary" @click="openCreate('chat')">立即添加</a-button>
          </template>
        </a-alert>

        <div class="toolbar">
          <a-radio-group v-model:value="filterPurpose" button-style="solid" @change="load">
            <a-radio-button value="">全部</a-radio-button>
            <a-radio-button value="chat">对话</a-radio-button>
            <a-radio-button value="embedding">向量</a-radio-button>
          </a-radio-group>
          <a-button type="primary" @click="openCreate()">
            <PlusOutlined /> 新增模型
          </a-button>
        </div>

        <a-table
          :data-source="models"
          :columns="columns"
          row-key="id"
          :loading="loading"
          size="middle"
          :pagination="{ pageSize: 8 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'purpose'">
              <a-tag :color="record.purpose === 'chat' ? 'blue' : 'purple'">
                {{ record.purpose === 'chat' ? '对话' : '向量' }}
              </a-tag>
              <a-tag v-if="record.is_default" color="green">默认</a-tag>
            </template>

            <template v-else-if="column.key === 'api_key'">
              <a-tooltip v-if="record.api_key_error" :title="record.api_key_error">
                <span class="key-error">密钥无法解密</span>
              </a-tooltip>
              <span v-else class="muted">{{ record.api_key || '—' }}</span>
            </template>

            <template v-else-if="column.key === 'state'">
              <a-tooltip v-if="record.last_test_ok" :title="testedAtText(record)">
                <a-tag color="success">
                  可用<template v-if="record.vector_size"> · {{ record.vector_size }} 维</template>
                </a-tag>
              </a-tooltip>
              <a-tooltip v-else-if="record.last_test_error" :title="record.last_test_error">
                <a-tag color="error">测试失败</a-tag>
              </a-tooltip>
              <a-tag v-else>未测试</a-tag>
            </template>

            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" :loading="testingId === record.id" @click="onTest(record)">
                  测试
                </a-button>
                <a-tooltip :title="record.last_test_ok ? '' : '需先通过连通性测试'">
                  <a-button
                    size="small"
                    :disabled="record.is_default || !record.last_test_ok"
                    @click="onSetDefault(record)"
                  >
                    设为默认
                  </a-button>
                </a-tooltip>
                <a-button size="small" @click="openEdit(record)">编辑</a-button>
                <a-popconfirm title="确认删除该模型？" @confirm="onDelete(record.id)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ================= 百度网盘 ================= -->
      <a-tab-pane key="netdisk" tab="百度网盘">
        <template v-if="netdisk.configured">
          <a-space v-if="netdisk.bound" wrap>
            <a-tag color="blue" class="netdisk-tag">
              <CloudOutlined /> {{ netdisk.baidu_name || '已绑定网盘' }}
            </a-tag>
            <span class="hint">
              简历等模块的原文件会同步到你网盘的应用目录（/apps/应用名/）。
            </span>
            <a-button size="small" @click="openBindNetdisk">重新绑定</a-button>
            <a-popconfirm
              title="解除绑定不会删除网盘里已同步的文件，确认解绑？"
              ok-text="解绑"
              cancel-text="取消"
              @confirm="onUnbindNetdisk"
            >
              <a-button size="small" danger>解绑</a-button>
            </a-popconfirm>
          </a-space>
          <a-space v-else wrap>
            <span class="hint">绑定后，简历等模块的原文件会同步一份到你的网盘。</span>
            <a-button type="primary" size="small" @click="openBindNetdisk">
              <CloudOutlined /> 绑定百度网盘
            </a-button>
          </a-space>
        </template>
        <div v-else class="hint">
          未启用：请在后端 .env 配置 BAIDU_NETDISK_APP_KEY / SECRET_KEY / APP_NAME。
        </div>
      </a-tab-pane>

      <!-- ================= 长期记忆 ================= -->
      <a-tab-pane key="memory" tab="长期记忆">
        <a-alert
          type="info"
          show-icon
          class="gate-alert"
          message="长期记忆是 AI 在对话中自动沉淀的、关于你的持久事实（职业背景、偏好、长期目标等）。"
          description="只有开了「长期记忆」开关的智能体会使用并积累记忆；你可以在这里查看、修改或删除任何一条。"
        />

        <div class="toolbar">
          <a-button type="primary" @click="openMemoryCreate">
            <PlusOutlined /> 手动添加
          </a-button>
          <a-popconfirm
            title="确认清空全部记忆？此操作不可恢复。"
            ok-text="清空"
            cancel-text="取消"
            @confirm="onMemoryClear"
          >
            <a-button danger :disabled="!memories.length">清空全部</a-button>
          </a-popconfirm>
        </div>

        <a-list :data-source="memories" :loading="memoryLoading" item-layout="horizontal">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta :description="`更新于 ${new Date(item.updated_at).toLocaleString()}`">
                <template #title>{{ item.content }}</template>
              </a-list-item-meta>
              <template #actions>
                <a-button size="small" @click="openMemoryEdit(item)">编辑</a-button>
                <a-popconfirm title="删除这条记忆？" @confirm="onMemoryDelete(item.id)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </template>
            </a-list-item>
          </template>
          <template #empty>
            <a-empty description="还没有记忆。与开了「长期记忆」的智能体对话后会自动积累。" />
          </template>
        </a-list>
      </a-tab-pane>
    </a-tabs>

    <!-- ================= 记忆编辑弹窗 ================= -->
    <a-modal
      v-model:open="memoryModal.open"
      :title="memoryModal.editing ? '编辑记忆' : '添加记忆'"
      :confirm-loading="memoryModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitMemory"
    >
      <a-form layout="vertical">
        <a-form-item label="内容" required>
          <a-textarea
            v-model:value="memoryModal.content"
            :rows="3"
            :maxlength="512"
            placeholder="一句自包含的话，例如：用户是有 5 年经验的 Go 后端工程师"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 网盘绑定弹窗 ================= -->
    <a-modal
      v-model:open="bindModal.open"
      title="绑定百度网盘"
      :confirm-loading="bindModal.loading"
      ok-text="完成绑定"
      cancel-text="取消"
      @ok="submitBindNetdisk"
    >
      <ol class="bind-steps">
        <li>点击下方按钮，在新窗口打开的百度授权页中登录并同意授权；</li>
        <li>授权后页面会显示一串授权码，复制它；</li>
        <li>把授权码粘贴到下方输入框，点击「完成绑定」。</li>
      </ol>
      <a-button block :loading="bindModal.authLoading" @click="openAuthPage">
        打开百度授权页（新窗口）
      </a-button>
      <a-input
        v-model:value="bindModal.code"
        class="bind-code-input"
        :maxlength="128"
        placeholder="粘贴授权码"
        @press-enter="submitBindNetdisk"
      />
      <p class="hint bind-tip">
        绑定后，各模块的原文件会同步到你网盘的应用目录（/apps/应用名/）；之后解绑不会删除已同步的文件。
      </p>
    </a-modal>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑模型' : '新增模型'"
      @ok="submit"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form layout="vertical">
        <a-form-item label="用途" required>
          <a-radio-group v-model:value="modal.form.purpose" button-style="solid">
            <a-radio-button value="chat">对话</a-radio-button>
            <a-radio-button value="embedding">向量</a-radio-button>
          </a-radio-group>
          <div class="hint">
            对话模型用于生成回答；向量模型用于知识库检索。两者需分别配置。
          </div>
        </a-form-item>
        <a-form-item label="标题" required>
          <a-input v-model:value="modal.form.title" placeholder="例如：主力对话模型" />
        </a-form-item>
        <a-form-item label="模型名称" required>
          <a-input v-model:value="modal.form.model_name" placeholder="例如：gpt-4o-mini" />
          <div class="hint">
            填服务商要求的模型标识。部分服务商此处要求填部署 / 接入点 ID 而非模型名，填错会导致调用全部失败，保存后请用「测试」验证。
          </div>
        </a-form-item>
        <a-form-item label="模型地址" required>
          <a-input v-model:value="modal.form.base_url" placeholder="https://api.openai.com/v1" />
          <div class="hint">
            兼容 OpenAI 协议的服务地址，通常以 /v1 结尾（火山方舟：https://ark.cn-beijing.volces.com/api/v3）。
            注意：Coding Plan、Agent Plan 等套餐仅限厂商官方工具内使用，其 key 不能在此配置
            （API 调用有封号风险），请使用按量付费的 key。
          </div>
        </a-form-item>
        <a-form-item label="密钥">
          <a-input-password
            v-model:value="modal.form.api_key"
            :placeholder="modal.editing ? '留空则不修改已保存的密钥' : 'sk-...'"
            autocomplete="off"
          />
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="modal.form.remark" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { CloudOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  memoryApi,
  netdiskApi,
  settingsApi,
  type MemoryItem,
  type ModelConfig,
  type ModelPurpose,
  type NetdiskStatus,
  type ReadinessStatus,
} from '@/api'

const activeTab = ref('models')

const models = ref<ModelConfig[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const filterPurpose = ref<'' | ModelPurpose>('')

const status = ref<ReadinessStatus>({
  chat: { ready: false, model: null, config_id: null },
  embedding: { ready: false, model: null, config_id: null },
  kb: { status: 'uninitialized', doc_count: 0, chunk_count: 0, model_mismatch: false },
})

const columns = [
  { title: '用途', key: 'purpose', width: 130 },
  { title: '标题', dataIndex: 'title' },
  { title: '模型名称', dataIndex: 'model_name' },
  { title: '模型地址', dataIndex: 'base_url', ellipsis: true },
  { title: '密钥', key: 'api_key', width: 130 },
  { title: '状态', key: 'state', width: 130 },
  { title: '操作', key: 'action', width: 290 },
]

const emptyForm = () => ({
  id: 0,
  purpose: 'chat' as ModelPurpose,
  title: '',
  model_name: '',
  base_url: '',
  api_key: '',
  remark: '',
})

const modal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: emptyForm(),
})

function testedAtText(record: ModelConfig): string {
  return record.last_tested_at
    ? `最近测试：${new Date(record.last_tested_at).toLocaleString()}`
    : ''
}

async function load() {
  loading.value = true
  try {
    const [list, readiness] = await Promise.all([
      settingsApi.listModels(filterPurpose.value || undefined),
      settingsApi.status(),
    ])
    models.value = list.items
    status.value = readiness
  } finally {
    loading.value = false
  }
}

function openCreate(purpose: ModelPurpose = 'chat') {
  modal.editing = false
  modal.form = { ...emptyForm(), purpose }
  modal.open = true
}

function openEdit(m: ModelConfig) {
  modal.editing = true
  modal.form = {
    id: m.id,
    purpose: m.purpose,
    title: m.title,
    model_name: m.model_name,
    base_url: m.base_url,
    // 这里故意留空：把掩码原样送回去毫无意义，而留空正好告诉后端
    // 保留已保存的那把密钥。
    api_key: '',
    remark: m.remark || '',
  }
  modal.open = true
}

async function submit() {
  const { title, model_name, base_url } = modal.form
  if (!title.trim() || !model_name.trim() || !base_url.trim()) {
    message.warning('请填写标题、模型名称、模型地址')
    return
  }

  modal.loading = true
  try {
    const payload = {
      purpose: modal.form.purpose,
      title: title.trim(),
      model_name: model_name.trim(),
      base_url: base_url.trim(),
      remark: modal.form.remark.trim() || null,
      // 编辑时如果密钥留空就整个字段都不传，这样只改其他字段的编辑
      // 不会把已保存的密钥弄丢。
      ...(modal.form.api_key ? { api_key: modal.form.api_key } : {}),
    }

    if (modal.editing) {
      await settingsApi.updateModel(modal.form.id, payload)
      message.success('已更新，请重新测试连通性')
    } else {
      await settingsApi.createModel(payload)
      message.success('已创建，请点击「测试」验证配置')
    }
    modal.open = false
    await load()
  } finally {
    modal.loading = false
  }
}

async function onTest(record: ModelConfig) {
  testingId.value = record.id
  try {
    const result = await settingsApi.testModel(record.id)
    if (result.success) {
      message.success(result.message)
    } else {
      // 服务商返回的错误往往很长，塞在 toast 里根本看不清。
      Modal.error({ title: '连通性测试失败', content: result.message })
    }
  } catch {
    message.error('测试请求失败，请稍后重试')
  } finally {
    testingId.value = null
    await load()
  }
}

async function onSetDefault(record: ModelConfig) {
  await settingsApi.setDefault(record.id)
  message.success('已设为默认')
  await load()
}

async function onDelete(id: number) {
  await settingsApi.deleteModel(id)
  message.success('已删除')
  await load()
}

// ---- 百度网盘 -----------------------------------------------------------------
// 绑定是全局能力（简历等模块共用），放在「百度网盘」Tab 维护；configured=false
// （后端没配 AppKey）时只展示一条提示，不渲染操作入口。

const netdisk = reactive<NetdiskStatus>({
  configured: false,
  bound: false,
  baidu_name: null,
  expires_at: null,
})
let netdiskLoaded = false

const bindModal = reactive({
  open: false,
  loading: false,
  authLoading: false,
  code: '',
})

async function loadNetdiskStatus() {
  try {
    Object.assign(netdisk, await netdiskApi.status())
    netdiskLoaded = true
  } catch {
    // 状态拿不到就当作功能未启用，不挡设置页主流程。
    netdisk.configured = false
  }
}

// 网盘状态按需加载：第一次切到该 Tab 才请求。
watch(activeTab, (key) => {
  if (key === 'netdisk' && !netdiskLoaded) {
    loadNetdiskStatus()
  }
  if (key === 'memory' && !memoryLoaded) {
    loadMemories()
  }
})

// ---- 长期记忆 ---------------------------------------------------------------
// 记忆主要由后台任务自动提取，这里提供查看与手动管理入口。

const memories = ref<MemoryItem[]>([])
const memoryLoading = ref(false)
let memoryLoaded = false

const memoryModal = reactive({
  open: false,
  editing: false,
  loading: false,
  id: 0,
  content: '',
})

async function loadMemories() {
  memoryLoading.value = true
  try {
    const data = await memoryApi.list()
    memories.value = data.items
    memoryLoaded = true
  } finally {
    memoryLoading.value = false
  }
}

function openMemoryCreate() {
  memoryModal.editing = false
  memoryModal.id = 0
  memoryModal.content = ''
  memoryModal.open = true
}

function openMemoryEdit(item: MemoryItem) {
  memoryModal.editing = true
  memoryModal.id = item.id
  memoryModal.content = item.content
  memoryModal.open = true
}

async function submitMemory() {
  if (!memoryModal.content.trim()) {
    message.warning('请填写记忆内容')
    return
  }
  memoryModal.loading = true
  try {
    if (memoryModal.editing) {
      await memoryApi.update(memoryModal.id, memoryModal.content.trim())
      message.success('已更新')
    } else {
      await memoryApi.create(memoryModal.content.trim())
      message.success('已添加')
    }
    memoryModal.open = false
    await loadMemories()
  } finally {
    memoryModal.loading = false
  }
}

async function onMemoryDelete(id: number) {
  await memoryApi.remove(id)
  message.success('已删除')
  await loadMemories()
}

async function onMemoryClear() {
  await memoryApi.clear()
  message.success('已清空全部记忆')
  await loadMemories()
}

function openBindNetdisk() {
  bindModal.code = ''
  bindModal.open = true
}

async function openAuthPage() {
  bindModal.authLoading = true
  try {
    const { url } = await netdiskApi.authUrl()
    window.open(url, '_blank')
  } catch (err) {
    message.error(errorText(err, '获取授权地址失败'))
  } finally {
    bindModal.authLoading = false
  }
}

async function submitBindNetdisk() {
  if (!bindModal.code.trim()) {
    message.warning('请先粘贴授权码')
    return
  }
  bindModal.loading = true
  try {
    const status = await netdiskApi.bind(bindModal.code.trim())
    Object.assign(netdisk, status)
    message.success('网盘绑定成功')
    bindModal.open = false
  } catch (err) {
    message.error(errorText(err, '绑定失败'))
  } finally {
    bindModal.loading = false
  }
}

async function onUnbindNetdisk() {
  try {
    await netdiskApi.unbind()
    netdisk.bound = false
    netdisk.baidu_name = null
    message.success('已解绑（网盘里的文件未受影响）')
  } catch (err) {
    message.error(errorText(err, '解绑失败'))
  }
}

function errorText(err: unknown, fallback: string): string {
  const envelope = err as { message?: string } | undefined
  return envelope?.message || fallback
}

onMounted(load)
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.toolbar {
  margin-bottom: 16px;
  justify-content: space-between;
}
/* 打码后的 API key 用等宽显示；颜色/字号继承全局 .muted（style.css）。 */
.muted {
  font-family: var(--font-mono);
}
.key-error {
  color: #cf1322;
  font-size: 12px;
}
/* 颜色/字号/行高用全局 .hint（style.css），这里只留本页差异。 */
.hint {
  margin-top: 4px;
}
.netdisk-tag {
  margin-inline-end: 0;
}
.bind-steps {
  margin: 0 0 12px;
  padding-left: 18px;
  line-height: 1.9;
  color: rgba(0, 0, 0, 0.75);
}
.bind-code-input {
  margin-top: 12px;
}
.bind-tip {
  margin-top: 10px;
}
</style>
