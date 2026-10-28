<template>
  <div>
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
      <a-table-column key="test" title="连通测试" :width="200">
        <template #default="{ record }">
          <template v-if="record.last_test_ok === true">
            <a-button
              v-if="record.tools?.length"
              type="link"
              size="small"
              class="tools-link"
              @click="openTools(record)"
            >
              <AppstoreOutlined />
              {{ record.tools.length }} 个工具
            </a-button>
            <span v-else class="test-ok">已通过 · {{ formatTime(record.last_test_at) }}</span>
            <div v-if="record.tools?.length" class="test-time">
              {{ formatTime(record.last_test_at) }}
            </div>
          </template>
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

    <a-drawer
      v-model:open="toolsDrawer.open"
      placement="right"
      width="640"
      :title="`发现的工具 · ${toolsServer?.name ?? ''}`"
    >
      <template v-if="toolsServer">
        <div class="tools-sub">
          <span class="tools-time">
            测试于 {{ formatTime(toolsServer.last_test_at) }} · 重新测试可刷新清单
          </span>
          <a-button
            size="small"
            :loading="testingId === toolsServer.id"
            @click="onTest(toolsServer.id)"
          >
            重新测试
          </a-button>
        </div>

        <a-alert
          v-if="toolsServer.last_test_ok === false"
          type="error"
          show-icon
          class="tools-failed"
          :message="toolsServer.last_test_msg || '连接失败'"
        />

        <a-empty
          v-if="!drawerTools.length"
          description="暂无工具快照，请先通过连通测试"
        />

        <div v-else class="tool-cards">
          <div v-for="tool in drawerTools" :key="tool.name" class="tool-card">
            <div class="tool-head">
              <AppstoreOutlined class="tool-icon" />
              <span class="tool-name">{{ tool.name }}</span>
              <a-button
                type="link"
                size="small"
                class="tool-test-btn"
                @click="openToolTest(toolsServer?.id ?? 0, tool)"
              >
                <ThunderboltOutlined />
                方法测试
              </a-button>
            </div>
            <div v-if="tool.description" class="tool-desc">{{ tool.description }}</div>
            <div v-if="toolParams(tool).length" class="param-list">
              <div
                v-for="param in toolParams(tool)"
                :key="param.name"
                class="param-line"
              >
                <span class="param-name">{{ param.name }}</span>
                <span v-if="param.required" class="param-required" title="必填">*</span>
                <span class="param-type">{{ param.type }}</span>
                <span v-if="param.description" class="param-desc">
                  — {{ param.description }}
                </span>
                <span v-if="param.enumText" class="param-meta">
                  （可选值 {{ param.enumText }}）
                </span>
                <span v-else-if="param.hasDefault" class="param-meta">
                  （默认 {{ param.defaultText }}）
                </span>
              </div>
            </div>
            <div v-else class="param-none">无参数</div>
          </div>
        </div>
      </template>
    </a-drawer>

    <a-modal
      v-model:open="toolTest.open"
      :title="`方法测试 · ${toolTest.tool?.name ?? ''}`"
      width="780px"
      ok-text="执行"
      cancel-text="关闭"
      :confirm-loading="toolTest.loading"
      @ok="runToolTest"
    >
      <template v-if="toolTest.tool">
        <div v-if="toolTest.tool.description" class="tt-desc">
          {{ toolTest.tool.description }}
        </div>

        <div class="tt-args-head">
          <span class="tt-label">入参（JSON 对象）</span>
          <a-button type="link" size="small" @click="fillArgsTemplate">
            填入示例参数
          </a-button>
        </div>
        <a-textarea
          v-model:value="toolTest.argsText"
          class="tt-editor"
          :rows="8"
          spellcheck="false"
          placeholder='{"city": "北京"}'
        />
        <div v-if="toolTest.argsError" class="tt-args-error">
          {{ toolTest.argsError }}
        </div>

        <div v-if="toolTest.result" class="tt-result">
          <div class="tt-result-head">
            <span class="tt-label">返回结果</span>
            <a-tag v-if="toolTest.result.success && !toolTest.result.is_error" color="green">
              成功
            </a-tag>
            <a-tag v-else-if="toolTest.result.is_error" color="orange">工具报错</a-tag>
            <a-tag v-else color="red">调用失败</a-tag>
          </div>

          <a-alert
            v-if="!toolTest.result.success"
            type="error"
            show-icon
            :message="toolTest.result.message || '调用失败'"
            class="tt-alert"
          />

          <template v-else>
            <a-alert
              v-if="toolTest.result.is_error"
              type="warning"
              show-icon
              message="工具执行返回 isError=true，内容见下方结果块"
              class="tt-alert"
            />

            <a-empty
              v-if="!toolTest.result.content.length && !toolTest.result.structured_content"
              description="工具返回了空内容"
              :image="simpleEmptyImage"
            />

            <div
              v-for="(block, index) in toolTest.result.content"
              :key="index"
              class="tt-block"
            >
              <pre v-if="block.type === 'text'" class="tt-pre">{{ prettyBlockText(block.text) }}</pre>
              <img
                v-else-if="block.type === 'image'"
                class="tt-img"
                :src="toDataUrl(block.mimeType, block.data)"
                alt="工具返回图片"
              />
              <audio
                v-else-if="block.type === 'audio'"
                class="tt-audio"
                controls
                :src="toDataUrl(block.mimeType, block.data)"
              />
              <div v-else-if="block.type === 'resource_link'" class="tt-resource-link">
                <LinkOutlined />
                <span class="tt-resource-name">{{ block.name || block.uri }}</span>
                <span class="tt-resource-uri">{{ block.uri }}</span>
              </div>
              <pre v-else class="tt-pre">{{ formatJson(block) }}</pre>
            </div>

            <div v-if="toolTest.result.structured_content" class="tt-structured">
              <div class="tt-label">结构化输出（structuredContent）</div>
              <pre class="tt-pre">{{ formatJson(toolTest.result.structured_content) }}</pre>
            </div>

            <a-collapse ghost class="tt-raw">
              <a-collapse-panel key="raw" header="原始返回 JSON">
                <pre class="tt-pre">{{ formatJson(toolTest.result) }}</pre>
              </a-collapse-panel>
            </a-collapse>
          </template>
        </div>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Empty, Modal, message } from 'ant-design-vue'
import {
  AppstoreOutlined,
  DownloadOutlined,
  LinkOutlined,
  PlusOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import {
  mcpApi,
  type McpConflictStrategy,
  type McpImportReport,
  type McpServer,
  type McpToolCallResult,
  type McpToolSnapshot,
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

// ---- 发现的工具抽屉 -----------------------------------------------------------

const toolsDrawer = reactive({ open: false, serverId: 0 })

const toolsServer = computed(() =>
  servers.value.find((server) => server.id === toolsDrawer.serverId) ?? null,
)
// 数据源始终是列表：抽屉里「重新测试」+ load() 后快照自动刷新。
const drawerTools = computed<McpToolSnapshot[]>(() => toolsServer.value?.tools ?? [])

function openTools(server: McpServer) {
  toolsDrawer.serverId = server.id
  toolsDrawer.open = true
}

interface ToolParamView {
  name: string
  type: string
  required: boolean
  description: string
  enumText: string
  hasDefault: boolean
  defaultText: string
}

/** 默认值/枚举的目录展示：对象数组收成一行 JSON，其余转字符串。 */
function formatDefault(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'object' && value !== null) {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

/** 把工具的 JSON Schema 展开成逐行参数视图（口径对齐本地工具目录）。 */
function toolParams(tool: McpToolSnapshot): ToolParamView[] {
  const schema = (tool.input_schema ?? {}) as Record<string, unknown>
  const properties =
    (schema.properties as Record<string, Record<string, unknown> | undefined> | undefined) ?? {}
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as unknown[]) : [],
  )
  return Object.entries(properties).map(([name, rawDef]) => {
    const def = rawDef ?? {}
    let type = def.type
    if (Array.isArray(type)) type = (type as unknown[]).join(' | ')
    const enumValues = Array.isArray(def.enum) ? (def.enum as unknown[]) : undefined
    return {
      name,
      // 缺 type（少见，如纯 anyOf 定义）时统一标 any。
      type: typeof type === 'string' && type ? type : 'any',
      required: required.has(name),
      description: typeof def.description === 'string' ? def.description : '',
      enumText: enumValues ? enumValues.map(formatDefault).join(' | ') : '',
      hasDefault: 'default' in def && def.default !== undefined,
      defaultText: 'default' in def ? formatDefault(def.default) : '',
    }
  })
}

// ---- 方法测试弹窗（参数与结果只活在页面上，不落库）----------------------------

const simpleEmptyImage = Empty.PRESENTED_IMAGE_SIMPLE

const toolTest = reactive({
  open: false,
  loading: false,
  serverId: 0,
  tool: null as McpToolSnapshot | null,
  argsText: '{}',
  argsError: '',
  result: null as McpToolCallResult | null,
})

/** 按 JSON Schema 造一个占位值：default > enum 首项 > 类型空壳。 */
function sampleValue(def: unknown): unknown {
  if (def && typeof def === 'object') {
    const schema = def as Record<string, unknown>
    if ('default' in schema && schema.default !== undefined) return schema.default
    if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0]
    const rawType = schema.type
    const type = Array.isArray(rawType) ? rawType[0] : rawType
    switch (type) {
      case 'string':
        return ''
      case 'integer':
      case 'number':
        return 0
      case 'boolean':
        return false
      case 'array':
        return []
      case 'object': {
        const obj: Record<string, unknown> = {}
        const properties =
          (schema.properties as Record<string, unknown> | undefined) ?? {}
        for (const [key, propDef] of Object.entries(properties)) {
          obj[key] = sampleValue(propDef)
        }
        return obj
      }
      default:
        return null
    }
  }
  return null
}

/** 依据入参 schema 生成模板：必填参数排前面，无参数给 {}。 */
function buildArgsTemplate(tool: McpToolSnapshot): string {
  const schema = (tool.input_schema ?? {}) as Record<string, unknown>
  const properties =
    (schema.properties as Record<string, unknown> | undefined) ?? {}
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as unknown[]) : [],
  )
  const names = Object.keys(properties)
  names.sort((a, b) => Number(required.has(b)) - Number(required.has(a)))
  const args: Record<string, unknown> = {}
  for (const name of names) args[name] = sampleValue(properties[name])
  return JSON.stringify(args, null, 2)
}

function openToolTest(serverId: number, tool: McpToolSnapshot) {
  toolTest.serverId = serverId
  toolTest.tool = tool
  toolTest.argsText = buildArgsTemplate(tool)
  toolTest.argsError = ''
  toolTest.result = null
  toolTest.loading = false
  toolTest.open = true
}

function fillArgsTemplate() {
  if (!toolTest.tool) return
  toolTest.argsText = buildArgsTemplate(toolTest.tool)
  toolTest.argsError = ''
}

async function runToolTest() {
  if (!toolTest.tool) return
  toolTest.argsError = ''

  let args: Record<string, unknown> = {}
  const text = toolTest.argsText.trim()
  if (text) {
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch (err) {
      toolTest.argsError = `入参 JSON 解析失败：${(err as Error).message}`
      return
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      toolTest.argsError = '入参必须是 JSON 对象，例如 {"city": "北京"}'
      return
    }
    args = parsed as Record<string, unknown>
  }

  toolTest.loading = true
  try {
    toolTest.result = await mcpApi.callTool(
      toolTest.serverId,
      toolTest.tool.name,
      args,
    )
  } finally {
    toolTest.loading = false
  }
}

/** text 块内容是 JSON 时美化展示，否则按原文。 */
function prettyBlockText(text: unknown): string {
  if (typeof text !== 'string') return String(text ?? '')
  const trimmed = text.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2)
    } catch {
      // 不是合法 JSON，下面按原文返回。
    }
  }
  return text
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function toDataUrl(mimeType: unknown, data: unknown): string {
  return `data:${typeof mimeType === 'string' ? mimeType : 'application/octet-stream'};base64,${data ?? ''}`
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
  color: var(--success);
}
.test-failed {
  color: var(--danger);
  cursor: help;
}
.test-none {
  color: var(--text-3);
}
.tools-link {
  height: auto;
  padding: 0;
  font-family: var(--font-mono, monospace);
}
.test-time {
  color: var(--text-3);
  font-size: var(--fs-xs);
  line-height: 1.4;
}
.tools-sub {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.tools-time {
  color: var(--text-3);
  font-size: var(--fs-sm);
}
.tools-failed {
  margin-bottom: 12px;
}
.tool-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.tool-card {
  border: 1px solid var(--hairline);
  border-radius: 8px;
  padding: 12px 14px;
}
.tool-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tool-test-btn {
  margin-left: auto;
  padding: 0 4px;
  height: auto;
  font-size: var(--fs-sm);
}
.tool-icon {
  color: var(--text-3);
}
.tool-name {
  font-family: var(--font-mono, monospace);
  font-weight: 600;
  color: var(--text-1);
}
.tool-desc {
  margin-top: 4px;
  color: var(--text-2);
  font-size: var(--fs-sm);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.param-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 8px;
}
.param-line {
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.param-name {
  font-family: var(--font-mono, monospace);
  color: var(--text-1);
}
.param-required {
  margin-left: 2px;
  color: var(--danger);
}
.param-type {
  margin-left: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}
.param-desc {
  margin-left: 6px;
  color: var(--text-2);
}
.param-meta {
  color: var(--text-3);
}
.param-none {
  margin-top: 8px;
  color: var(--text-3);
  font-size: var(--fs-sm);
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
  color: var(--danger);
}
/* 方法测试弹窗 */
.tt-desc {
  margin-bottom: 12px;
  color: var(--text-2);
  font-size: var(--fs-sm);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.tt-args-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.tt-label {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-1);
}
.tt-editor :deep(textarea) {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-sm);
}
.tt-args-error {
  margin-top: 6px;
  color: var(--danger);
  font-size: var(--fs-sm);
}
.tt-result {
  margin-top: 16px;
}
.tt-result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.tt-alert {
  margin-bottom: 8px;
}
.tt-block {
  margin-bottom: 8px;
}
.tt-pre {
  margin: 0;
  padding: 10px 12px;
  background: var(--chrome);
  border: 1px solid var(--hairline);
  border-radius: 6px;
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-sm);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
.tt-img {
  max-width: 100%;
  border: 1px solid var(--hairline);
  border-radius: 6px;
}
.tt-audio {
  width: 100%;
}
.tt-resource-link {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 6px;
  font-size: var(--fs-sm);
}
.tt-resource-name {
  font-weight: 600;
}
.tt-resource-uri {
  color: var(--text-3);
  font-family: var(--font-mono, monospace);
  word-break: break-all;
}
.tt-structured {
  margin-top: 12px;
}
.tt-structured .tt-label {
  margin-bottom: 6px;
}
.tt-raw {
  margin-top: 4px;
}
.tt-raw :deep(.ant-collapse-header) {
  font-size: var(--fs-sm);
  color: var(--text-3) !important;
}
</style>
