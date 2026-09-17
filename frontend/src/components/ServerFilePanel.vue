<template>
  <div
    class="sfp"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 拖拽遮罩 pointer-events:none：事件始终落在底层元素上，
         遮罩自己的出现/消失不会引发 dragenter/leave 抖动。 -->
    <div v-if="dragDepth > 0" class="sfp-drop-mask">
      <UploadOutlined class="sfp-drop-icon" />
      <div>松开鼠标，上传到 {{ currentPath }}</div>
    </div>

    <div class="sfp-toolbar">
      <a-tooltip title="上一级">
        <button type="button" class="sfp-icon-btn" :disabled="!canGoUp || !!busy" @click="goUp">
          <ArrowUpOutlined />
        </button>
      </a-tooltip>
      <a-tooltip title="刷新">
        <button type="button" class="sfp-icon-btn" :disabled="loading || !!busy" @click="reload">
          <ReloadOutlined />
        </button>
      </a-tooltip>
      <!-- 写操作（上传/新建/删除/拖拽）按运维写权限置灰；后端同样 403 强制。 -->
      <a-upload
        :show-upload-list="false"
        :before-upload="onUpload"
        :disabled="!currentPath || !!busy || !canWrite"
      >
        <a-tooltip :title="canWrite ? '上传到当前目录' : '没有运维写权限'">
          <button type="button" class="sfp-icon-btn" :disabled="!currentPath || !!busy || !canWrite">
            <UploadOutlined />
          </button>
        </a-tooltip>
      </a-upload>
      <a-tooltip :title="canWrite ? '新建文件夹' : '没有运维写权限'">
        <button
          type="button"
          class="sfp-icon-btn"
          :disabled="!currentPath || !!busy || !canWrite"
          @click="openMkdir"
        >
          <FolderAddOutlined />
        </button>
      </a-tooltip>
      <a-popconfirm
        :title="selected?.is_dir ? '递归删除整个目录及其全部内容？' : `删除 ${selected?.name}？`"
        ok-text="删除"
        cancel-text="取消"
        :ok-button-props="{ danger: true }"
        @confirm="onDelete"
      >
        <a-tooltip :title="canWrite ? '删除选中项' : '没有运维写权限'">
          <button
            type="button"
            class="sfp-icon-btn danger"
            :disabled="!selected || !!busy || !canWrite"
          >
            <DeleteOutlined />
          </button>
        </a-tooltip>
      </a-popconfirm>
    </div>

    <div class="sfp-path">
      <template v-for="(seg, i) in breadcrumbs" :key="seg.path">
        <span v-if="i > 0" class="sfp-path-sep">/</span>
        <button type="button" class="sfp-path-seg" @click="load(seg.path)">{{ seg.name }}</button>
      </template>
    </div>

    <div class="sfp-body">
      <a-spin :spinning="loading" size="small">
        <div v-if="errorMsg" class="sfp-error">
          <WarningOutlined class="sfp-error-icon" />
          <div class="sfp-error-text">{{ errorMsg }}</div>
          <button type="button" class="sfp-retry" @click="reload">重试</button>
        </div>
        <div v-else-if="!entries.length && !loading" class="sfp-empty">空目录</div>
        <template v-else>
          <div
            v-for="entry in entries"
            :key="entry.name"
            :class="['sfp-row', { selected: selected === entry, dir: entry.is_dir }]"
            @click="selected = entry"
            @dblclick="onOpen(entry)"
          >
            <FolderOutlined v-if="entry.is_dir" class="sfp-row-icon dir" />
            <FileOutlined v-else class="sfp-row-icon" />
            <span class="sfp-row-name" :title="entry.name">{{ entry.name }}</span>
            <span class="sfp-row-size">{{ entry.is_dir ? '' : fmtSize(entry.size) }}</span>
            <span class="sfp-row-time">{{ fmtTime(entry.mtime) }}</span>
            <a-tooltip v-if="!entry.is_dir" title="下载">
              <button
                type="button"
                class="sfp-row-download"
                :disabled="!!busy"
                @click.stop="onDownload(entry)"
              >
                <DownloadOutlined />
              </button>
            </a-tooltip>
          </div>
          <div v-if="truncated" class="sfp-truncated">目录过大，仅显示前 2000 条</div>
        </template>
      </a-spin>
    </div>

    <div v-if="busy || dropping" class="sfp-foot">
      {{ dropping ? `正在上传 ${dropDone} / ${dropTotal} …` : busyText }}
    </div>

    <a-modal
      v-model:open="mkdirOpen"
      title="新建文件夹"
      :confirm-loading="mkdirSaving"
      ok-text="创建"
      cancel-text="取消"
      @ok="submitMkdir"
    >
      <a-input
        v-model:value="mkdirName"
        placeholder="文件夹名称"
        @keydown.enter="submitMkdir"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ArrowUpOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FileOutlined,
  FolderAddOutlined,
  FolderOutlined,
  ReloadOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { opsApi, type SftpEntry } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useTransfersStore } from '@/stores/transfers'
import { fmtSize } from '@/utils/format'

const props = defineProps<{ serverId: number }>()
const transfers = useTransfersStore()
const auth = useAuthStore()
// 只读账号能浏览和下载，写操作全部置灰（后端 upload/mkdir/delete 同样 403）。
const canWrite = computed(() => auth.canOpsWrite)

const currentPath = ref('')
const entries = ref<SftpEntry[]>([])
const truncated = ref(false)
const loading = ref(false)
const errorMsg = ref('')
const selected = ref<SftpEntry | null>(null)
// 上传/下载是后台任务（进度在「传输」标签页），这里的 busy 只管删除。
const busy = ref<'delete' | ''>('')
const busyText = ref('')

const mkdirOpen = ref(false)
const mkdirName = ref('')
const mkdirSaving = ref(false)

// ---- 路径工具（posix 语义） ----------------------------------------------------

function join(dir: string, name: string): string {
  return dir === '/' ? `/${name}` : `${dir}/${name}`
}

function parent(path: string): string {
  if (!path || path === '/') return '/'
  const cut = path.lastIndexOf('/')
  return cut <= 0 ? '/' : path.slice(0, cut)
}

const canGoUp = computed(() => !!currentPath.value && currentPath.value !== '/')

const breadcrumbs = computed(() => {
  const path = currentPath.value
  if (!path) return []
  const segs = [{ name: '/', path: '/' }]
  if (path === '/') return segs
  let acc = ''
  for (const part of path.split('/').filter(Boolean)) {
    acc += `/${part}`
    segs.push({ name: part, path: acc })
  }
  return segs
})

// ---- 加载 --------------------------------------------------------------------

async function load(path?: string) {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await opsApi.sftpList(props.serverId, path || undefined)
    currentPath.value = res.path
    entries.value = res.items
    truncated.value = res.truncated
    selected.value = null
  } catch (err: any) {
    errorMsg.value = err?.message || '加载失败'
    entries.value = []
  } finally {
    loading.value = false
  }
}

function reload() {
  load(currentPath.value)
}

function goUp() {
  load(parent(currentPath.value))
}

function onOpen(entry: SftpEntry) {
  if (entry.is_dir) load(join(currentPath.value, entry.name))
  else onDownload(entry)
}

// ---- 操作 --------------------------------------------------------------------

function onDownload(entry: SftpEntry) {
  // 后台任务：进度和结果都在「传输」标签页里，这里发出去就完事。
  transfers.startDownload(props.serverId, join(currentPath.value, entry.name), entry.name)
}

async function onUpload(file: File) {
  const item = await transfers.startUpload(props.serverId, currentPath.value, file)
  if (item.status === 'done') await load(currentPath.value)
  // 拦下 a-upload 的默认请求，上传已经由上面的调用完成。
  return false
}

// ---- 拖拽上传 -------------------------------------------------------------------
// dragenter/dragleave 会在子元素之间反复冒泡，用深度计数而不是布尔值，
// 才能真正判断「拖出了整个面板」。
const dragDepth = ref(0)
const dropping = ref(false)
const dropDone = ref(0)
const dropTotal = ref(0)

function isFileDrag(e: DragEvent): boolean {
  return !!e.dataTransfer?.types.includes('Files')
}

function onDragEnter(e: DragEvent) {
  if (!isFileDrag(e) || !canWrite.value) return
  e.preventDefault()
  dragDepth.value += 1
}

function onDragOver(e: DragEvent) {
  // 必须 preventDefault，浏览器才允许 drop。
  if (!isFileDrag(e)) return
  e.preventDefault()
}

function onDragLeave(e: DragEvent) {
  if (!isFileDrag(e)) return
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}

async function onDrop(e: DragEvent) {
  if (!isFileDrag(e)) return
  e.preventDefault()
  dragDepth.value = 0
  if (!canWrite.value) {
    message.warning('没有运维写权限，无法上传')
    return
  }
  if (!currentPath.value || dropping.value) return

  // 文件夹没有对应的上传通道（递归建目录+逐文件传是另一套活），跳过并讲明。
  const items = [...(e.dataTransfer?.items ?? [])]
  const dirCount = items.filter((it) => it.webkitGetAsEntry()?.isDirectory).length
  if (dirCount) message.warning('暂不支持拖拽文件夹，请先打包再上传')
  const files = items
    .filter((it) => it.kind === 'file' && !it.webkitGetAsEntry()?.isDirectory)
    .map((it) => it.getAsFile())
    .filter((f): f is File => !!f)
  if (!files.length) return

  // 多个文件串行传：进度都进「传输」标签页，并行只会互相抢带宽。
  dropping.value = true
  dropDone.value = 0
  dropTotal.value = files.length
  let ok = 0
  try {
    for (const file of files) {
      const item = await transfers.startUpload(props.serverId, currentPath.value, file)
      dropDone.value += 1
      if (item.status === 'done') ok += 1
    }
  } finally {
    dropping.value = false
  }
  if (ok) await load(currentPath.value)
}

function openMkdir() {
  mkdirName.value = ''
  mkdirOpen.value = true
}

async function submitMkdir() {
  const name = mkdirName.value.trim()
  if (!name) return
  if (name.includes('/') || name === '.' || name === '..') {
    message.error('名称不能包含 /')
    return
  }
  mkdirSaving.value = true
  try {
    await opsApi.sftpMkdir(props.serverId, join(currentPath.value, name))
    mkdirOpen.value = false
    await load(currentPath.value)
  } catch (err: any) {
    message.error(err?.message || '创建失败')
  } finally {
    mkdirSaving.value = false
  }
}

async function onDelete() {
  const entry = selected.value
  if (!entry) return
  busy.value = 'delete'
  busyText.value = `正在删除 ${entry.name} …`
  try {
    await opsApi.sftpRemove(props.serverId, join(currentPath.value, entry.name))
    message.success(`已删除 ${entry.name}`)
    selected.value = null
    await load(currentPath.value)
  } catch (err: any) {
    message.error(err?.message || '删除失败')
  } finally {
    busy.value = ''
    busyText.value = ''
  }
}

// ---- 展示格式 -----------------------------------------------------------------

function fmtTime(mtime: number): string {
  if (!mtime) return ''
  const date = new Date(mtime * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

onMounted(() => load(''))
</script>

<style scoped>
/* 与终端页同族的深色面板：底 #11181f，发丝线 rgba(255,255,255,0.08)，
   强调青沿用终端光标色 #3ad6de。 */

.sfp {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  width: 100%;
  min-height: 0;
}

/* 拖拽上传遮罩：盖住整个面板，虚线框 + 强调青。 */
.sfp-drop-mask {
  position: absolute;
  inset: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 2px dashed rgba(58, 214, 222, 0.6);
  border-radius: 6px;
  background: rgba(17, 24, 31, 0.85);
  color: #3ad6de;
  font-size: 13px;
  pointer-events: none;
}

.sfp-drop-icon {
  font-size: 28px;
}

.sfp-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

/* a-upload 会在 trigger 外包一层 span，让它不产生额外布局影响。 */
.sfp-toolbar :deep(.ant-upload-wrapper),
.sfp-toolbar :deep(.ant-upload) {
  display: inline-flex;
}

.sfp-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: #8b98a5;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}

.sfp-icon-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  color: #d8dee4;
}

.sfp-icon-btn.danger:hover:not(:disabled) {
  background: rgba(255, 77, 79, 0.15);
  color: #ff7875;
}

.sfp-icon-btn:disabled {
  opacity: 0.35;
  cursor: default;
}

.sfp-path {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  padding: 6px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  word-break: break-all;
}

.sfp-path-seg {
  padding: 0 2px;
  border: none;
  background: transparent;
  color: #8b98a5;
  font: inherit;
  cursor: pointer;
}

.sfp-path-seg:hover {
  color: #3ad6de;
}

.sfp-path-seg:last-of-type {
  color: #d8dee4;
}

.sfp-path-sep {
  color: #4a5560;
}

.sfp-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 0;
}

/* 深色底上 antd 的 loading 遮罩默认是半透明白，换成深色。 */
.sfp-body :deep(.ant-spin-nested-loading > div > .ant-spin) {
  background: rgba(17, 24, 31, 0.6);
  max-height: none;
}

.sfp-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  cursor: pointer;
  user-select: none;
}

.sfp-row:hover {
  background: rgba(255, 255, 255, 0.05);
}

.sfp-row.selected {
  background: rgba(58, 214, 222, 0.12);
}

.sfp-row-icon {
  flex-shrink: 0;
  color: #8b98a5;
  font-size: 13px;
}

.sfp-row-icon.dir {
  color: #3ad6de;
}

.sfp-row-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #d8dee4;
  font-size: 12px;
}

.sfp-row-size {
  flex-shrink: 0;
  width: 48px;
  color: #8b98a5;
  font-family: var(--font-mono);
  font-size: 11px;
  text-align: right;
}

.sfp-row-time {
  flex-shrink: 0;
  width: 72px;
  color: #5f6b78;
  font-family: var(--font-mono);
  font-size: 11px;
  text-align: right;
}

/* 下载按钮压在行尾（盖住时间列），hover 行时才现身。 */
.sfp-row-download {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  display: none;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: rgba(58, 214, 222, 0.15);
  color: #3ad6de;
  font-size: 12px;
  cursor: pointer;
}

.sfp-row:hover .sfp-row-download {
  display: inline-flex;
}

.sfp-empty,
.sfp-truncated {
  padding: 16px 10px;
  color: #5f6b78;
  font-size: 12px;
  text-align: center;
}

.sfp-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 24px 16px;
}

.sfp-error-icon {
  color: #faad14;
  font-size: 20px;
}

.sfp-error-text {
  color: #8b98a5;
  font-size: 12px;
  text-align: center;
  word-break: break-all;
}

.sfp-retry {
  padding: 2px 14px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 4px;
  background: transparent;
  color: #aab6c2;
  font-size: 12px;
  cursor: pointer;
}

.sfp-retry:hover {
  border-color: rgba(58, 214, 222, 0.45);
  color: #3ad6de;
}

.sfp-foot {
  padding: 5px 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  color: #8b98a5;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
