<template>
  <div class="mermaid-card">
    <!-- 预览态 -->
    <template v-if="!editing">
      <div v-if="rendering" class="state">
        <a-spin size="small" />
        <span>正在渲染图…</span>
      </div>
      <!-- 语法错了不藏：把错误和源码都摆出来，用户照着改或让 AI 修。 -->
      <div v-else-if="error" class="state error">
        <div class="error-text">图渲染失败：{{ error }}</div>
        <pre class="error-source">{{ source }}</pre>
      </div>
      <!-- mermaid 自己生成 SVG，securityLevel: 'strict' 下标签里的 HTML 会被
           转义，这里的 v-html 不等于把模型输出直接塞进 DOM。 -->
      <div v-else class="diagram" v-html="svgHtml" />
    </template>

    <!-- 编辑态：直接改 Mermaid 源码。 -->
    <a-textarea
      v-else
      v-model:value="draft"
      class="editor"
      :auto-size="{ minRows: 6, maxRows: 20 }"
      spellcheck="false"
    />

    <div class="actions">
      <template v-if="!editing">
        <a-tooltip v-if="canEdit" title="编辑源码">
          <button type="button" class="action-btn" @click="startEdit">
            <EditOutlined />
          </button>
        </a-tooltip>
        <a-tooltip title="复制源码">
          <button type="button" class="action-btn" @click="copySource">
            <CopyOutlined />
          </button>
        </a-tooltip>
        <a-tooltip v-if="svgHtml && !error" title="下载 SVG">
          <button type="button" class="action-btn" @click="downloadSvg">
            <DownloadOutlined />
          </button>
        </a-tooltip>
      </template>
      <template v-else>
        <span v-if="editError" class="edit-error">{{ editError }}</span>
        <a-button size="small" @click="cancelEdit">取消</a-button>
        <a-button size="small" type="primary" :loading="validating" @click="saveEdit">
          保存
        </a-button>
      </template>
    </div>
  </div>
</template>

<script lang="ts">
// 模块级计数器：mermaid.render 的 id 全局唯一，跨卡片实例也不能撞。
let mermaidRenderSeq = 0
</script>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { message as toast } from 'ant-design-vue'
import { CopyOutlined, DownloadOutlined, EditOutlined } from '@ant-design/icons-vue'
import { copyText } from '@/utils/clipboard'

const props = defineProps<{
  /** Mermaid 图源码（不含围栏）。 */
  source: string
  /** 是否允许手动编辑；流式中或消息还没落库时为 false。 */
  canEdit: boolean
}>()

// 保存成功由父组件回写消息内容（连同调接口落库），source prop 随之更新。
const emit = defineEmits<{ save: [source: string] }>()

// mermaid 体积不小（含布局引擎 dagre），按需动态加载：只有真的出现图卡片
// 时才会拉这个 chunk，普通对话不受影响。
type Mermaid = typeof import('mermaid')['default']
let mermaidPromise: Promise<Mermaid> | null = null

function loadMermaid(): Promise<Mermaid> {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((mod) => {
      mod.default.initialize({
        startOnLoad: false,
        // strict：标签里的 HTML 一律转义。模型输出不可信，这条不能松。
        securityLevel: 'strict',
        theme: 'default',
      })
      return mod.default
    })
  }
  return mermaidPromise
}

const svgHtml = ref('')
const error = ref('')
const rendering = ref(false)

// mermaid.render 需要一个全局唯一的元素 id；渲染失败时它会把一个同 id 的
// 错误节点挂到 body 上，得顺手清掉，否则页面上会凭空多出一坨红字。
// 计数器必须在模块级（见上方 <script> 块）：script setup 顶层是实例级的，
// 两张图卡片并发渲染会生成相同的 id，错误节点清理互相串扰。

async function render(source: string) {
  rendering.value = true
  error.value = ''
  try {
    const mermaid = await loadMermaid()
    const id = `mermaid-card-${++mermaidRenderSeq}`
    try {
      const { svg } = await mermaid.render(id, source)
      svgHtml.value = svg
    } catch (err) {
      document.getElementById(id)?.remove()
      svgHtml.value = ''
      error.value = err instanceof Error ? err.message.split('\n')[0] : String(err)
    }
  } finally {
    rendering.value = false
  }
}

// source 变化（含父组件保存后回写）时重新渲染。immediate 覆盖首屏。
watch(() => props.source, (source) => void render(source), { immediate: true })

// ---- 编辑 -------------------------------------------------------------------

const editing = ref(false)
const draft = ref('')
const validating = ref(false)
const editError = ref('')

function startEdit() {
  draft.value = props.source
  editError.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
}

async function saveEdit() {
  const source = draft.value.trim()
  if (!source) {
    editError.value = '图源码不能为空'
    return
  }
  // 先本地试渲染再交给父组件落库：保存一份渲染不出来的源码，AI 下轮看到的
  // 也是这份坏代码，错误会一路延续下去。
  validating.value = true
  editError.value = ''
  try {
    const mermaid = await loadMermaid()
    await mermaid.parse(source)
  } catch (err) {
    editError.value = err instanceof Error ? err.message.split('\n')[0] : String(err)
    validating.value = false
    return
  }
  validating.value = false
  editing.value = false
  emit('save', source)
}

// ---- 操作 -------------------------------------------------------------------

async function copySource() {
  if (await copyText(props.source)) toast.success('已复制源码')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

function downloadSvg() {
  const blob = new Blob([svgHtml.value], { type: 'image/svg+xml' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `diagram-${Date.now()}.svg`
  anchor.click()
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.mermaid-card {
  margin: 8px 0;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.diagram {
  padding: 12px;
  overflow-x: auto;
  text-align: center;
}
.diagram :deep(svg) {
  max-width: 100%;
}
.state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px;
  color: var(--text-3);
  font-size: 13px;
}
.state.error {
  flex-direction: column;
  align-items: stretch;
}
.error-text {
  color: #ff4d4f;
}
.error-source {
  margin: 8px 0 0;
  padding: 8px;
  max-height: 160px;
  overflow: auto;
  background: var(--paper);
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.editor {
  border: none;
  border-radius: 0;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 13px;
}
.editor:focus {
  box-shadow: none;
}
.actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  padding: 4px 8px;
  border-top: 1px solid var(--hairline);
}
.action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-3);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.action-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--signal-text);
}
.edit-error {
  margin-right: auto;
  font-size: 12px;
  color: #ff4d4f;
}
</style>
