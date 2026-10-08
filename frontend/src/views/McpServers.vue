<template>
  <div class="page-card">
    <div class="page-title">MCP 服务</div>
    <div class="subtitle">
      配置 MCP（Model Context Protocol）服务，并在工作流上挂载。运行工作流时，
      模型可以调用这些服务提供的工具获取信息或执行动作。
    </div>

    <div class="toolbar">
      <a-space>
        <a-button @click="openImport">
          <UploadOutlined /> 导入
        </a-button>
        <a-button @click="onExport">
          <DownloadOutlined /> 导出
        </a-button>
        <a-button type="primary" @click="openCreate">
          <PlusOutlined /> 新建 MCP 服务
        </a-button>
      </a-space>
    </div>

    <a-table
      :data-source="servers"
      :loading="loading"
      :pagination="false"
      row-key="id"
      :row-selection="{ selectedRowKeys, onChange: onSelectionChange }"
    >
      <a-table-column key="name" title="名称" :width="180">
        <template #default="{ record }">
          <span>{{ record.name }}</span>
          <a-tag v-if="record.secret_error" color="red" class="secret-tag">密钥异常</a-tag>
        </template>
      </a-table-column>
      <a-table-column key="transport" title="传输方式" :width="170">
        <template #default="{ record }">
          <a-tag :color="transportColor(record.transport)">
            {{ transportLabel(record.transport) }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column key="target" title="命令 / 地址">
        <template #default="{ record }">
          <span v-if="record.transport === 'stdio'" class="target-text">
            {{ record.command }}<template v-if="record.args?.length"> {{ record.args.join(' ') }}</template>
          </span>
          <span v-else class="target-text">{{ record.url }}</span>
        </template>
      </a-table-column>
      <a-table-column key="enabled" title="启用" :width="80">
        <template #default="{ record }">
          <a-tag :color="record.enabled ? 'green' : 'default'">
            {{ record.enabled ? '启用' : '停用' }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column key="test" title="连通测试" :width="170">
        <template #default="{ record }">
          <span v-if="record.last_test_ok === true" class="test-ok">
            已通过 · {{ formatTime(record.last_test_at) }}
          </span>
          <a-tooltip v-else-if="record.last_test_ok === false" :title="record.last_test_msg">
            <span class="test-failed">未通过 · {{ formatTime(record.last_test_at) }}</span>
          </a-tooltip>
          <span v-else class="test-none">未测试</span>
        </template>
      </a-table-column>
      <a-table-column key="actions" title="操作" :width="200">
        <template #default="{ record }">
          <a-space>
            <a-button size="small" :loading="testingId === record.id" @click="onTest(record.id)">
              测试
            </a-button>
            <a-button size="small" @click="openEdit(record)">编辑</a-button>
            <a-popconfirm title="确定删除该 MCP 服务？" @confirm="onDelete(record.id)">
              <a-button size="small" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </a-table-column>
    </a-table>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑 MCP 服务' : '新建 MCP 服务'"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
      width="680px"
      @ok="submit"
    >
      <a-alert
        v-if="form.transport === 'stdio'"
        type="error"
        show-icon
        class="stdio-alert"
        message="stdio 会在服务器上以本进程权限启动子进程并执行任意命令，请只配置可信来源的服务。"
      />

      <a-form layout="vertical">
        <a-form-item label="服务名称" required>
          <a-input v-model:value="form.name" placeholder="例如：内部工单、时间服务" />
        </a-form-item>

        <a-form-item label="传输方式" required>
          <a-radio-group v-model:value="form.transport" button-style="solid">
            <a-radio-button value="stdio">stdio</a-radio-button>
            <a-radio-button value="sse">SSE</a-radio-button>
            <a-radio-button value="streamable_http">Streamable HTTP</a-radio-button>
          </a-radio-group>
        </a-form-item>

        <template v-if="form.transport === 'stdio'">
          <a-form-item label="启动命令" required>
            <a-input v-model:value="form.command" placeholder="例如：uvx、node、python" />
          </a-form-item>
          <a-form-item label="参数">
            <a-select
              v-model:value="form.args"
              mode="tags"
              placeholder="输入参数后回车，例如 mcp-server-time"
              :token-separators="[',']"
            />
          </a-form-item>
          <a-form-item label="环境变量">
            <SecretRows v-model:rows="form.envRows" key-placeholder="变量名，例如 API_KEY" />
          </a-form-item>
        </template>

        <template v-else>
          <a-form-item label="服务 URL" required>
            <a-input v-model:value="form.url" placeholder="https://example.com/mcp" />
          </a-form-item>
          <a-form-item label="请求头">
            <SecretRows v-model:rows="form.headersRows" key-placeholder="头名称，例如 Authorization" />
          </a-form-item>
        </template>

        <a-form-item label="启用">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="form.remark" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="importModal.open"
      title="导入 mcpServers 配置"
      :confirm-loading="importModal.loading"
      ok-text="导入"
      cancel-text="关闭"
      width="680px"
      @ok="doImport"
    >
      <a-alert
        type="info"
        show-icon
        class="import-alert"
        message="支持 Claude Desktop / Cursor 通用的 mcpServers JSON：可粘贴文本或选择 .json 文件。stdio 条目会在服务器上启动子进程，请确认命令来源可信。"
      />

      <a-form layout="vertical">
        <a-form-item label="同名服务处理">
          <a-radio-group v-model:value="importModal.conflict" button-style="solid">
            <a-radio-button value="rename">自动改名</a-radio-button>
            <a-radio-button value="skip">跳过</a-radio-button>
            <a-radio-button value="overwrite">覆盖</a-radio-button>
          </a-radio-group>
        </a-form-item>

        <a-form-item label="配置内容">
          <div class="import-text-head">
            <a-button size="small" @click="pickFile">选择文件</a-button>
            <input
              ref="fileInput"
              type="file"
              accept=".json,application/json"
              class="hidden-file"
              @change="onFilePicked"
            />
          </div>
          <a-textarea
            v-model:value="importModal.text"
            :rows="9"
            placeholder='{"mcpServers": {"lark-mcp": {"command": "npx", "args": []}}}'
          />
        </a-form-item>
      </a-form>

      <a-alert
        v-if="importModal.report"
        :type="importModal.report.errors.length ? 'warning' : 'success'"
        class="import-result"
        show-icon
        :message="`创建 ${importModal.report.created} 个 · 跳过 ${importModal.report.skipped} 个 · 覆盖 ${importModal.report.overwritten} 个`"
      >
        <template #description>
          <div v-for="line in importModal.report.renamed" :key="line" class="report-line">
            改名：{{ line }}
          </div>
          <div v-for="line in importModal.report.errors" :key="line" class="report-line error">
            {{ line }}
          </div>
        </template>
      </a-alert>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { DownloadOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import {
  mcpApi,
  type McpConflictStrategy,
  type McpImportReport,
  type McpServer,
  type McpTransport,
} from '@/api'
import SecretRows from '@/components/SecretRows.vue'
import { saveTextFile } from '@/utils/download'

const servers = ref<McpServer[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)

// 导出用的勾选：未勾选时导出按钮导出全部。
const selectedRowKeys = ref<number[]>([])
function onSelectionChange(keys: (number | string)[]) {
  selectedRowKeys.value = keys.map(Number)
}

interface SecretRow {
  key: string
  value: string
  /** 该行来自已存在的密钥（回显为掩码）：值清空表示删除该键。 */
  existed: boolean
}

const emptyForm = () => ({
  id: 0,
  name: '',
  transport: 'stdio' as McpTransport,
  command: '',
  args: [] as string[],
  url: '',
  envRows: [] as SecretRow[],
  headersRows: [] as SecretRow[],
  enabled: true,
  remark: '',
})

const form = reactive(emptyForm())
const modal = reactive({ open: false, editing: false, loading: false })

function toRows(secret: Record<string, string>): SecretRow[] {
  // 回显的值是掩码，保持原样回传即不修改。
  return Object.entries(secret || {}).map(([key, value]) => ({
    key,
    value,
    existed: true,
  }))
}

async function load() {
  loading.value = true
  try {
    const data = await mcpApi.list()
    servers.value = data.items
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  modal.editing = false
  modal.open = true
}

function openEdit(server: McpServer) {
  Object.assign(form, emptyForm(), {
    id: server.id,
    name: server.name,
    transport: server.transport,
    command: server.command || '',
    args: [...server.args],
    url: server.url || '',
    envRows: toRows(server.env),
    headersRows: toRows(server.headers),
    enabled: server.enabled,
    remark: server.remark || '',
  })
  modal.editing = true
  modal.open = true
}

/** 密钥行 → 提交对象：空值且是新增行的键直接跳过；已存在键的空值保留（删键）。 */
function secretPayload(rows: SecretRow[]): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    if (!row.existed && row.value === '') continue
    payload[key] = row.value
  }
  return payload
}

async function submit() {
  if (!form.name.trim()) {
    message.warning('请填写服务名称')
    return
  }
  if (form.transport === 'stdio' && !form.command.trim()) {
    message.warning('stdio 类型必须填写启动命令')
    return
  }
  if (form.transport !== 'stdio' && !form.url.trim()) {
    message.warning('请填写服务 URL')
    return
  }

  modal.loading = true
  try {
    const payload = {
      name: form.name.trim(),
      transport: form.transport,
      enabled: form.enabled,
      remark: form.remark || null,
    }
    if (form.transport === 'stdio') {
      Object.assign(payload, {
        command: form.command.trim(),
        args: [...form.args],
        env: secretPayload(form.envRows),
      })
    } else {
      Object.assign(payload, {
        url: form.url.trim(),
        headers: secretPayload(form.headersRows),
      })
    }

    if (modal.editing) {
      await mcpApi.update(form.id, payload)
      message.success('已更新')
    } else {
      await mcpApi.create(payload)
      message.success('已创建')
    }
    modal.open = false
    await load()
  } finally {
    modal.loading = false
  }
}

async function onTest(id: number) {
  testingId.value = id
  try {
    const result = await mcpApi.test(id)
    if (result.success) message.success(result.message)
    else message.error(result.message)
    await load()
  } finally {
    testingId.value = null
  }
}

async function onDelete(id: number) {
  try {
    await mcpApi.remove(id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

// ---- 导出 -------------------------------------------------------------------

function onExport() {
  const ids = selectedRowKeys.value
  const scopeText = ids.length ? `选中的 ${ids.length} 个` : '全部'
  Modal.confirm({
    title: '导出 MCP 配置',
    content: `将导出${scopeText}服务为 mcpServers JSON。文件包含明文环境变量/请求头，请妥善保管、不要随意外发。`,
    okText: '导出',
    cancelText: '取消',
    onOk: async () => {
      const data = await mcpApi.exportConfig(ids.length ? ids : undefined)
      saveTextFile(
        `mcp-servers-${dayjs().format('YYYYMMDD-HHmm')}.json`,
        JSON.stringify({ mcpServers: data.mcpServers }, null, 2),
        'application/json',
      )
      for (const warning of data.warnings) message.warning(warning)
      message.success('已导出')
    },
  })
}

// ---- 导入 -------------------------------------------------------------------

const fileInput = ref<HTMLInputElement | null>(null)
const importModal = reactive<{
  open: boolean
  loading: boolean
  text: string
  conflict: McpConflictStrategy
  report: McpImportReport | null
}>({
  open: false,
  loading: false,
  text: '',
  conflict: 'rename',
  report: null,
})

function openImport() {
  importModal.text = ''
  importModal.conflict = 'rename'
  importModal.report = null
  importModal.loading = false
  importModal.open = true
  if (fileInput.value) fileInput.value.value = ''
}

function pickFile() {
  fileInput.value?.click()
}

async function onFilePicked(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  importModal.text = await file.text()
}

async function doImport() {
  if (!importModal.text.trim()) {
    message.warning('请粘贴配置内容或选择文件')
    return
  }
  let config: unknown
  try {
    config = JSON.parse(importModal.text)
  } catch {
    message.error('JSON 格式不正确，请检查粘贴内容')
    return
  }

  importModal.loading = true
  try {
    const report = await mcpApi.importConfig(
      config as Record<string, unknown>,
      importModal.conflict,
    )
    importModal.report = report
    if (!report.errors.length) {
      message.success(`成功导入 ${report.created} 个服务`)
    }
    await load()
  } finally {
    importModal.loading = false
  }
}

function transportLabel(transport: McpTransport) {
  if (transport === 'sse') return 'SSE'
  if (transport === 'streamable_http') return 'Streamable HTTP'
  return 'stdio'
}
function transportColor(transport: McpTransport) {
  return transport === 'stdio' ? 'purple' : 'cyan'
}
function formatTime(value?: string | null) {
  return value ? dayjs(value).format('MM-DD HH:mm') : ''
}

onMounted(load)
</script>

<style scoped>
.subtitle {
  color: var(--text-3);
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
.toolbar {
  margin-bottom: 16px;
  justify-content: flex-end;
}
.secret-tag {
  margin-left: 6px;
}
.target-text {
  word-break: break-all;
}
.test-ok {
  color: #389e0d;
}
.test-failed {
  color: #cf1322;
  cursor: help;
}
.test-none {
  color: var(--text-3);
}
.stdio-alert {
  margin-bottom: 16px;
}
.hidden-file {
  display: none;
}
.import-alert {
  margin-bottom: 16px;
}
.import-text-head {
  margin-bottom: 8px;
}
.import-result {
  margin-top: 8px;
}
.report-line {
  word-break: break-all;
}
.report-line.error {
  color: #cf1322;
}
</style>
