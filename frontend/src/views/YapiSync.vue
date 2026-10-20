<template>
  <div class="page-card">
    <div class="page-title">YApi 同步</div>
    <div class="subtitle">
      扫描本地 Java Spring 源码里的接口，每个 Controller 写入一个 YApi 分类，分类名取
      Swagger 的 @Tag（没有时用类名）。不勾选任何接口时默认同步全部，勾选后只同步勾选项；
      重复同步时按「HTTP 方法 + 路径」匹配，已存在的接口更新，YApi 中多出来的接口不会被删除。
      点击接口行可预览入参、请求体与出参结构。
    </div>

    <a-form layout="vertical" class="config-form">
      <a-row :gutter="16">
        <a-col :xs="24" :md="10">
          <a-form-item label="YApi 地址" required>
            <a-input
              v-model:value="form.base_url"
              placeholder="http://yapi.example.com"
              :disabled="busy"
            />
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="4">
          <a-form-item label="项目 ID" required>
            <a-input-number
              v-model:value="form.project_id"
              :min="1"
              :precision="0"
              placeholder="例如 12"
              style="width: 100%"
              :disabled="busy"
            />
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="10">
          <a-form-item label="项目 Token" required>
            <a-input-password
              v-model:value="form.token"
              placeholder="YApi 项目设置里的 token"
              autocomplete="new-password"
              :disabled="busy"
            />
          </a-form-item>
        </a-col>
        <a-col :span="24">
          <a-form-item label="本地源码路径" required>
            <div class="path-picker">
              <a-input
                v-model:value="form.root_path"
                placeholder="后端所在机器上的目录，例如 D:\code\demo\src\main\java"
                :disabled="busy || picking"
                allow-clear
                style="flex: 1"
              />
              <a-button
                :loading="picking"
                :disabled="busy"
                @click="onPickDirectory"
              >
                <FolderOpenOutlined /> 选择目录
              </a-button>
            </div>
            <div class="field-hint">
              填到包的上级目录即可；路径下可以有任意层级的包，每一级都能包含接口。
              扫描会跳过 target、build、src/test 等目录。
              「选择目录」弹出的是后端所在机器的系统对话框，也可以直接粘贴路径。
            </div>
          </a-form-item>
        </a-col>
      </a-row>
    </a-form>

    <div class="toolbar">
      <a-space wrap>
        <a-button :loading="scanning" :disabled="!scanReady || syncing || picking" @click="onScan">
          <ScanOutlined /> 扫描接口
        </a-button>
        <a-button
          type="primary"
          :loading="syncing"
          :disabled="!syncReady || scanning || picking"
          @click="openSyncConfirm"
        >
          <CloudUploadOutlined /> 同步到 YApi
        </a-button>
      </a-space>
      <span class="token-hint">Token 只随本次请求发送，不会保存到本地。</span>
    </div>

    <template v-if="scanResult">
      <a-row :gutter="12" class="stat-row">
        <a-col :xs="12" :sm="6">
          <a-statistic title="Java 文件" :value="scanResult.java_files" />
        </a-col>
        <a-col :xs="12" :sm="6">
          <a-statistic title="Controller" :value="scanResult.controller_count" />
        </a-col>
        <a-col :xs="12" :sm="6">
          <a-statistic title="接口" :value="scanResult.endpoint_count" />
        </a-col>
        <a-col :xs="12" :sm="6">
          <a-statistic title="分类" :value="scanResult.categories.length" />
        </a-col>
      </a-row>

      <a-alert
        v-if="warnings.length"
        type="warning"
        show-icon
        class="block-gap"
        message="扫描 / 同步提示"
      >
        <template #description>
          <ul class="warning-list">
            <li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li>
          </ul>
        </template>
      </a-alert>

      <a-alert
        v-if="syncResult"
        :type="syncResult.failed ? 'warning' : 'success'"
        show-icon
        class="block-gap"
      >
        <template #message>
          同步完成：新增 {{ syncResult.created }} 个，更新 {{ syncResult.updated }} 个，
          失败 {{ syncResult.failed }} 个，新建分类 {{ syncResult.categories_created }} 个，
          耗时 {{ formatDuration(syncResult.duration_ms) }}。
        </template>
        <template #description>
          目标：{{ syncResult.base_url }} · 项目 {{ syncResult.project_id }}。失败的接口会在下表标红，修正后可重新同步。
        </template>
      </a-alert>

      <div v-if="groups.length" class="selection-bar">
        <span v-if="selectedCount" class="selection-count">
          已选 {{ selectedCount }} / {{ endpointTotal }} 个接口，仅同步勾选项
        </span>
        <span v-else class="selection-count">
          未勾选：将同步全部 {{ endpointTotal }} 个接口
        </span>
        <a-button
          v-if="selectedCount"
          type="link"
          size="small"
          :disabled="busy"
          @click="clearSelection"
        >
          清空勾选
        </a-button>
      </div>

      <div v-if="groups.length" class="groups">
        <a-collapse v-model:activeKey="activeKeys">
          <a-collapse-panel
            v-for="group in groups"
            :key="group.key"
          >
            <template #header>
              <span class="group-title">{{ group.category }}</span>
              <span class="group-meta">
                {{ group.controllers.join('、') }} · {{ group.package || '(默认包)' }}
              </span>
            </template>
            <template #extra>
              <span class="group-count">{{ group.endpoints.length }} 个接口</span>
            </template>
            <a-table
              :data-source="group.endpoints"
              :row-key="rowKey"
              :row-selection="groupSelection(group)"
              :custom-row="previewRow"
              size="small"
              :pagination="{ pageSize: 10, size: 'small', hideOnSinglePage: true }"
              :scroll="{ x: 1130 }"
            >
              <a-table-column title="方法" key="method" :width="86">
                <template #default="{ record }">
                  <a-tag :color="methodColor(record.http_method)">
                    {{ record.http_method }}
                  </a-tag>
                </template>
              </a-table-column>
              <a-table-column title="路径" key="path" :width="280">
                <template #default="{ record }">
                  <span class="path-text">{{ record.path }}</span>
                </template>
              </a-table-column>
              <a-table-column title="名称" key="title">
                <template #default="{ record }">
                  <div>{{ record.title }}</div>
                  <div class="method-text">{{ record.controller }}.{{ record.method }}()</div>
                </template>
              </a-table-column>
              <a-table-column title="参数" key="params" :width="80">
                <template #default="{ record }">
                  <a-tooltip v-if="record.params.length">
                    <template #title>
                      <div v-for="param in record.params" :key="param.name">
                        {{ param.name }}（{{ locationLabel(param.location) }}）
                      </div>
                    </template>
                    <span>{{ record.params.length }} 个</span>
                  </a-tooltip>
                  <span v-else class="muted">—</span>
                </template>
              </a-table-column>
              <a-table-column title="请求体" key="body" :width="86">
                <template #default="{ record }">
                  <a-tag v-if="record.request_body_type === 'json'" color="blue">JSON</a-tag>
                  <a-tag v-else-if="record.request_body_type === 'form'" color="geekblue">
                    Form
                  </a-tag>
                  <span v-else class="muted">—</span>
                </template>
              </a-table-column>
              <a-table-column title="出参" key="response" :width="110">
                <template #default="{ record }">
                  <a-tooltip v-if="responseFields(record.response_schema).length">
                    <template #title>
                      <div
                        v-for="field in responseFields(record.response_schema)"
                        :key="field"
                      >
                        {{ field }}
                      </div>
                    </template>
                    <span class="response-text">
                      {{ responseLabel(record.response_schema) }}
                    </span>
                  </a-tooltip>
                  <span
                    v-else-if="record.response_schema"
                    class="response-text"
                  >
                    {{ responseLabel(record.response_schema) }}
                  </span>
                  <span v-else class="muted">无返回</span>
                </template>
              </a-table-column>
              <a-table-column title="源码" key="source" :width="170">
                <template #default="{ record }">
                  <a-tooltip :title="`${record.source}:${record.line}`">
                    <span class="source-text">
                      {{ shortSource(record.source) }}:{{ record.line }}
                    </span>
                  </a-tooltip>
                </template>
              </a-table-column>
              <a-table-column v-if="syncResult" title="结果" key="action" :width="96">
                <template #default="{ record }">
                  <a-tooltip v-if="record.action === 'failed'" :title="record.error">
                    <a-tag color="red">失败</a-tag>
                  </a-tooltip>
                  <a-tag v-else-if="record.action === 'created'" color="green">新增</a-tag>
                  <a-tag v-else-if="record.action === 'updated'" color="blue">更新</a-tag>
                  <span v-else class="muted">—</span>
                </template>
              </a-table-column>
            </a-table>
          </a-collapse-panel>
        </a-collapse>
      </div>

      <a-empty v-else class="empty-block" description="没有扫描到接口，请检查路径" />
    </template>

    <a-modal
      v-model:open="confirmOpen"
      title="确认同步到 YApi"
      ok-text="开始同步"
      cancel-text="取消"
      :confirm-loading="syncing"
      @ok="doSync"
    >
      <p v-if="selectedCount">
        将把选中的 {{ selectedCount }} 个接口（共扫描到 {{ endpointTotal }} 个）写入
        <strong>{{ form.base_url }}</strong>（项目 {{ form.project_id }}）。
      </p>
      <p v-else>
        未勾选具体接口，将把扫描到的 {{ endpointTotal }} 个接口全部写入
        <strong>{{ form.base_url }}</strong>（项目 {{ form.project_id }}）。
      </p>
      <p class="confirm-note">
        已存在的接口按 HTTP 方法 + 路径更新，YApi 中本地没有的接口不会被删除。
      </p>
    </a-modal>

    <a-drawer
      v-model:open="previewOpen"
      width="760"
      placement="right"
    >
      <template #title>
        <a-space v-if="previewEndpoint" :size="8">
          <a-tag :color="methodColor(previewEndpoint.http_method)">
            {{ previewEndpoint.http_method }}
          </a-tag>
          <span class="preview-path">{{ previewEndpoint.path }}</span>
        </a-space>
        <span v-else>接口预览</span>
      </template>

      <template v-if="previewEndpoint">
        <a-descriptions bordered size="small" :column="1">
          <a-descriptions-item label="名称">
            {{ previewEndpoint.title }}
          </a-descriptions-item>
          <a-descriptions-item label="Controller">
            {{ previewEndpoint.controller }}.{{ previewEndpoint.method }}()
          </a-descriptions-item>
          <a-descriptions-item label="分类">
            {{ previewEndpoint.category_name }}
          </a-descriptions-item>
          <a-descriptions-item label="Java 包">
            {{ previewEndpoint.package || '(默认包)' }}
          </a-descriptions-item>
          <a-descriptions-item label="源码">
            {{ previewEndpoint.source }}:{{ previewEndpoint.line }}
          </a-descriptions-item>
          <a-descriptions-item
            v-if="previewEndpoint.description || previewEndpoint.summary"
            label="说明"
          >
            {{ previewEndpoint.description || previewEndpoint.summary }}
          </a-descriptions-item>
          <a-descriptions-item v-if="previewEndpoint.action" label="同步结果">
            <a-tag v-if="previewEndpoint.action === 'created'" color="green">新增</a-tag>
            <a-tag v-else-if="previewEndpoint.action === 'updated'" color="blue">更新</a-tag>
            <a-tag v-else color="red">失败</a-tag>
            <span
              v-if="previewEndpoint.action === 'failed' && previewEndpoint.error"
              class="preview-error"
            >
              {{ previewEndpoint.error }}
            </span>
          </a-descriptions-item>
        </a-descriptions>

        <div class="preview-section-title">入参</div>
        <a-table
          v-if="previewParams.length"
          :data-source="previewParams"
          :row-key="paramRowKey"
          size="small"
          :pagination="false"
        >
          <a-table-column title="名称" data-index="name" :width="150" />
          <a-table-column title="位置" key="location" :width="90">
            <template #default="{ record }">
              {{ locationLabel(record.location) }}
            </template>
          </a-table-column>
          <a-table-column title="类型" data-index="type" :width="150" />
          <a-table-column title="必填" key="required" :width="70">
            <template #default="{ record }">{{ record.required ? '是' : '否' }}</template>
          </a-table-column>
          <a-table-column title="说明" data-index="description" />
          <a-table-column title="示例" data-index="example" :width="120" />
        </a-table>
        <span v-else class="muted">无参数</span>

        <div class="preview-section-title">请求体</div>
        <pre
          v-if="previewEndpoint.request_body_schema"
          class="schema-preview"
        >{{ prettySchema(previewEndpoint.request_body_schema) }}</pre>
        <span
          v-else-if="previewEndpoint.request_body_type === 'form'"
          class="muted"
        >
          表单字段见上方入参列表
        </span>
        <span v-else class="muted">无请求体</span>

        <div class="preview-section-title">出参</div>
        <pre
          v-if="previewEndpoint.response_schema"
          class="schema-preview"
        >{{ prettySchema(previewEndpoint.response_schema) }}</pre>
        <span v-else class="muted">无返回</span>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { CloudUploadOutlined, FolderOpenOutlined, ScanOutlined } from '@ant-design/icons-vue'
import {
  devtoolsApi,
  type YapiEndpointParam,
  type YapiParamLocation,
  type YapiScanEndpoint,
  type YapiScanResult,
  type YapiSyncAction,
  type YapiSyncResult,
} from '@/api/devtools'

type DisplayEndpoint = YapiScanEndpoint & {
  action?: YapiSyncAction
  error?: string | null
}

interface EndpointGroup {
  key: string
  package: string
  category: string
  controllers: string[]
  endpoints: DisplayEndpoint[]
}

const form = reactive({
  base_url: '',
  project_id: null as number | null,
  token: '',
  root_path: '',
})

const scanning = ref(false)
const syncing = ref(false)
const picking = ref(false)
const scanResult = ref<YapiScanResult | null>(null)
const syncResult = ref<YapiSyncResult | null>(null)
const confirmOpen = ref(false)
const activeKeys = ref<string[]>([])
const selectedKeys = ref<string[]>([])
const previewOpen = ref(false)
const previewEndpoint = ref<DisplayEndpoint | null>(null)

const busy = computed(() => scanning.value || syncing.value)
const scanReady = computed(() => Boolean(form.root_path.trim()))
const endpointTotal = computed(() => scanResult.value?.endpoints.length ?? 0)
const selectedCount = computed(() => selectedKeys.value.length)
const syncReady = computed(
  () =>
    Boolean(scanResult.value) &&
    Boolean(form.base_url.trim()) &&
    Boolean(form.project_id) &&
    Number(form.project_id) > 0 &&
    Boolean(form.token.trim()),
)
const warnings = computed(
  () => syncResult.value?.warnings ?? scanResult.value?.warnings ?? [],
)
const previewParams = computed(() =>
  (previewEndpoint.value?.params ?? []).filter((param) => param.location !== 'body'),
)

const groups = computed<EndpointGroup[]>(() => {
  const syncedByKey = new Map(
    (syncResult.value?.endpoints ?? []).map((item) => [rowKey(item), item]),
  )
  const source: DisplayEndpoint[] = (scanResult.value?.endpoints ?? []).map(
    (endpoint) => {
      const synced = syncedByKey.get(rowKey(endpoint))
      return synced
        ? { ...endpoint, action: synced.action, error: synced.error }
        : { ...endpoint }
    },
  )
  const grouped = new Map<string, EndpointGroup>()
  for (const item of scanResult.value?.categories ?? []) {
    grouped.set(item.category_name, {
      key: item.category_name,
      package: item.package,
      category: item.category_name,
      controllers: [...item.controllers],
      endpoints: [],
    })
  }
  for (const endpoint of source) {
    let group = grouped.get(endpoint.category_name)
    if (!group) {
      group = {
        key: endpoint.category_name,
        package: endpoint.package,
        category: endpoint.category_name,
        controllers: [endpoint.controller],
        endpoints: [],
      }
      grouped.set(endpoint.category_name, group)
    }
    group.endpoints.push(endpoint)
  }
  return [...grouped.values()]
})

watch(
  () => form.root_path,
  () => {
    scanResult.value = null
    syncResult.value = null
    previewOpen.value = false
    previewEndpoint.value = null
  },
)

watch(
  () => [form.base_url, form.project_id, form.token],
  () => {
    syncResult.value = null
  },
)

watch(scanResult, (value) => {
  activeKeys.value = value?.categories.map((item) => item.category_name) ?? []
  selectedKeys.value = []
})

function validateCredentials(): boolean {
  if (!form.base_url.trim()) {
    message.warning('请填写 YApi 地址')
    return false
  }
  if (!form.project_id || form.project_id < 1) {
    message.warning('请填写项目 ID')
    return false
  }
  if (!form.token.trim()) {
    message.warning('请填写项目 Token')
    return false
  }
  return true
}

async function onPickDirectory() {
  if (picking.value) return
  picking.value = true
  try {
    const path = await devtoolsApi.pickDirectory()
    if (path) {
      form.root_path = path
    }
  } catch {
    // 错误提示由 http 拦截器统一弹出。
  } finally {
    picking.value = false
  }
}

async function onScan() {
  if (!scanReady.value) {
    message.warning('请填写本地源码路径')
    return
  }
  scanning.value = true
  syncResult.value = null
  try {
    const result = await devtoolsApi.scanYapi({
      root_path: form.root_path.trim(),
    })
    scanResult.value = result
    if (!result.endpoint_count) {
      message.warning('没有扫描到接口，请检查路径和注解')
    } else {
      message.success(
        `识别到 ${result.endpoint_count} 个接口，分属 ${result.categories.length} 个 Controller 分类`,
      )
    }
  } catch {
    scanResult.value = null
  } finally {
    scanning.value = false
  }
}

function openSyncConfirm() {
  if (!syncReady.value || !validateCredentials()) return
  confirmOpen.value = true
}

async function doSync() {
  if (!scanResult.value) return
  confirmOpen.value = false
  previewOpen.value = false
  syncing.value = true
  try {
    const result = await devtoolsApi.syncYapi({
      root_path: form.root_path.trim(),
      base_url: form.base_url.trim(),
      project_id: Number(form.project_id),
      token: form.token.trim(),
      // 不勾选时省略该字段，后端按「同步全部扫描结果」处理。
      ...(selectedKeys.value.length
        ? { endpoint_keys: [...selectedKeys.value] }
        : {}),
    })
    syncResult.value = result
    if (result.failed) {
      message.warning(
        `同步完成：新增 ${result.created}，更新 ${result.updated}，失败 ${result.failed}`,
      )
    } else {
      message.success(
        `同步完成：新增 ${result.created}，更新 ${result.updated}`,
      )
    }
  } catch {
    // 错误提示由 http 拦截器统一弹出。
  } finally {
    syncing.value = false
  }
}

function rowKey(record: DisplayEndpoint) {
  return `${record.http_method} ${record.path}`
}

function groupSelection(group: EndpointGroup) {
  const keys = group.endpoints.map(rowKey)
  const keySet = new Set(keys)
  return {
    selectedRowKeys: selectedKeys.value.filter((key) => keySet.has(key)),
    onChange: (picked: (string | number)[]) => {
      const next = new Set(selectedKeys.value)
      for (const key of keys) {
        next.delete(key)
      }
      for (const key of picked) {
        next.add(String(key))
      }
      selectedKeys.value = [...next]
    },
  }
}

function clearSelection() {
  selectedKeys.value = []
}

function previewRow(record: DisplayEndpoint) {
  return {
    style: { cursor: 'pointer' },
    onClick: (event: MouseEvent) => {
      // 勾选框自己处理点击，别顺带把预览抽屉打开。
      if ((event.target as HTMLElement | null)?.closest('.ant-table-selection-column')) {
        return
      }
      previewEndpoint.value = record
      previewOpen.value = true
    },
  }
}

function paramRowKey(record: YapiEndpointParam) {
  return `${record.location}:${record.name}`
}

function prettySchema(schema: Record<string, unknown>) {
  return JSON.stringify(schema, null, 2)
}

function methodColor(method: string) {
  switch (method) {
    case 'GET':
      return 'blue'
    case 'POST':
      return 'green'
    case 'PUT':
      return 'orange'
    case 'DELETE':
      return 'red'
    case 'PATCH':
      return 'purple'
    default:
      return 'default'
  }
}

function locationLabel(location: YapiParamLocation) {
  switch (location) {
    case 'query':
      return '查询参数'
    case 'path':
      return '路径参数'
    case 'header':
      return '请求头'
    case 'body':
      return '请求体'
    case 'form':
      return '表单'
    default:
      return location
  }
}

type SchemaNode = Record<string, unknown> | null

function responseLabel(schema: SchemaNode): string {
  if (!schema) return '无返回'
  const type = String(schema.type ?? '')
  if (!type) return '未解析'
  if (type === 'array') {
    return `数组 · ${schemaTypeLabel(schema.items as SchemaNode)}`
  }
  if (type === 'object') {
    const fields = responseFields(schema)
    return fields.length ? `${fields.length} 个字段` : '未解析'
  }
  return schemaTypeLabel(schema)
}

function responseFields(schema: SchemaNode): string[] {
  if (!schema) return []
  if (schema.type === 'array') {
    return responseFields(schema.items as SchemaNode)
  }
  const props = schema.properties as Record<string, SchemaNode> | undefined
  if (!props) return []
  return Object.entries(props)
    .slice(0, 30)
    .map(([name, value]) => `${name}：${schemaTypeLabel(value)}`)
}

function schemaTypeLabel(schema: SchemaNode): string {
  if (!schema) return '任意'
  const type = String(schema.type ?? '')
  if (type === 'array') {
    return `数组<${schemaTypeLabel(schema.items as SchemaNode)}>`
  }
  if (type === 'object') {
    const count = Object.keys(
      (schema.properties as Record<string, unknown> | undefined) ?? {},
    ).length
    return count ? `对象(${count})` : '对象'
  }
  if (!type) return '任意'
  return schema.format ? `${type}(${String(schema.format)})` : type
}

function shortSource(source: string) {
  return source.split('/').pop() || source
}

function formatDuration(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`
}
</script>

<style scoped>
.config-form {
  margin-top: 20px;
}

.field-hint,
.token-hint {
  color: #8a94a0;
  font-size: 12px;
}

.path-picker {
  display: flex;
  gap: 8px;
}

.field-hint {
  margin-top: 4px;
  line-height: 1.5;
}

.toolbar {
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 4px 0 8px;
}

.stat-row {
  margin: 20px 0 4px;
}

.block-gap {
  margin-top: 16px;
}

.warning-list {
  margin: 0;
  padding-left: 18px;
  max-height: 200px;
  overflow: auto;
}

.groups {
  margin-top: 16px;
}

.selection-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.selection-count {
  color: #0a7e86;
  font-size: 13px;
  font-weight: 500;
}

.group-title {
  font-weight: 500;
  margin-right: 8px;
}

.group-meta {
  color: #8a94a0;
  font-size: 12px;
}

.group-count {
  color: #0a7e86;
  font-size: 12px;
}

.path-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.method-text,
.source-text,
.muted {
  color: #8a94a0;
  font-size: 12px;
}

.response-text {
  font-size: 12px;
}

.empty-block {
  padding: 32px 0;
}

.confirm-note {
  color: #5a6572;
  font-size: 13px;
  margin-bottom: 0;
}

.preview-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
}

.preview-error {
  color: #c0392b;
  font-size: 12px;
  margin-left: 8px;
}

.preview-section-title {
  font-weight: 600;
  margin: 20px 0 8px;
}

.schema-preview {
  margin: 0;
  padding: 12px;
  max-height: 320px;
  overflow: auto;
  background: #f6f8fa;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
