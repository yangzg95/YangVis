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
          <span class="sfp-btn-tip">
            <button
              type="button"
              class="sfp-icon-btn"
              :disabled="!currentPath || !!busy || !canWrite"
            >
              <UploadOutlined />
            </button>
          </span>
        </a-tooltip>
      </a-upload>
      <a-tooltip :title="canWrite ? '新建文件夹' : '没有运维写权限'">
        <span class="sfp-btn-tip">
          <button
            type="button"
            class="sfp-icon-btn"
            :disabled="!currentPath || !!busy || !canWrite"
            @click="openMkdir"
          >
            <FolderAddOutlined />
          </button>
        </span>
      </a-tooltip>
      <!-- 弹层挂在包裹层上而不是那颗按钮上，所以置灰要靠 popconfirm 自己的 disabled：
           按钮 pointer-events:none 之后点击会落到外层 span，少了这道闸门就能越权弹确认。 -->
      <a-popconfirm
        :title="selected?.is_dir ? '递归删除整个目录及其全部内容？' : `删除 ${selected?.name}？`"
        ok-text="删除"
        cancel-text="取消"
        :ok-button-props="{ danger: true }"
        :disabled="!selected || !!busy || !canWrite"
        @confirm="onDelete"
      >
        <a-tooltip :title="canWrite ? '删除选中项' : '没有运维写权限'">
          <span class="sfp-btn-tip">
            <button
              type="button"
              class="sfp-icon-btn danger"
              :disabled="!selected || !!busy || !canWrite"
            >
              <DeleteOutlined />
            </button>
          </span>
        </a-tooltip>
      </a-popconfirm>
    </div>

    <div class="sfp-path">
      <template v-for="(seg, i) in breadcrumbs" :key="seg.path">
        <span v-if="i > 0" class="sfp-path-sep">/</span>
        <button type="button" class="sfp-path-seg" @click="load(seg.path)">{{ seg.name }}</button>
      </template>
    </div>

    <div v-if="clipboard" class="sfp-clip">
      <ScissorOutlined v-if="clipboard.mode === 'cut'" class="sfp-clip-icon" />
      <CopyOutlined v-else class="sfp-clip-icon" />
      <span class="sfp-clip-name" :title="clipboard.path">{{ clipboard.name }}</span>
      <span class="sfp-clip-hint">右键目标位置粘贴</span>
      <button type="button" class="sfp-clip-clear" title="取消" @click="clipboard = null">
        <CloseOutlined />
      </button>
    </div>

    <div class="sfp-body" @contextmenu.prevent="openMenu($event, null)">
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
            :class="[
              'sfp-row',
              { selected: selected === entry, dir: entry.is_dir, clipped: isCut(entry) },
            ]"
            @click="selected = entry"
            @dblclick="onOpen(entry)"
            @contextmenu.prevent.stop="openMenu($event, entry)"
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
    <!-- 上传/下载在「传输」标签页里跑，这一行让用户在文件列表上也知道有事在跑，
         点一下跳过去看细节，不用先猜进度在哪。 -->
    <button
      v-else-if="activeTransfer"
      type="button"
      class="sfp-foot sfp-foot-link"
      @click="emit('showTransfers')"
    >
      <span class="sfp-foot-text">{{ activeText }}</span>
      <span class="sfp-foot-join">
        {{ moreRunning ? `还有 ${moreRunning} 条` : '查看' }}
      </span>
    </button>

    <!-- 右键菜单用 fixed 定位：.sfp-body 是滚动容器，absolute 会被它裁掉。 -->
    <DarkContextMenu
      :open="menu.open"
      :x="menu.left"
      :y="menu.top"
      :items="menuItems"
      @pick="onMenuPick"
      @close="closeMenu"
    />

    <!-- 菜单里的「上传」只能自己点开 file input：a-upload 的触发器绑在工具栏那颗按钮上。 -->
    <input ref="uploadInput" type="file" multiple class="sfp-file-input" @change="onPickFiles" />

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
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ArrowUpOutlined,
  CloseOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FileOutlined,
  FolderAddOutlined,
  FolderOutlined,
  ReloadOutlined,
  ScissorOutlined,
  SnippetsOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { opsApi, type SftpEntry } from '@/api'
import DarkContextMenu, { type DarkMenuItem } from './DarkContextMenu.vue'
import { useAuthStore } from '@/stores/auth'
import { useTransfersStore } from '@/stores/transfers'
import { fmtSize } from '@/utils/format'

const props = defineProps<{ serverId: number }>()
// 传输列表在父页面的另一个标签里，这里只负责跳过去。
const emit = defineEmits<{ showTransfers: [] }>()
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
// 上传/下载是后台任务（进度在「传输」标签页），这里的 busy 只管删除和粘贴。
const busy = ref<'delete' | 'paste' | ''>('')
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
  // 列表要换了，挂在旧条目上的菜单先收掉。
  closeMenu()
  try {
    const res = await opsApi.sftpList(props.serverId, path || undefined)
    currentPath.value = res.path
    entries.value = res.items
    truncated.value = res.truncated
    // 上传上限随列目录下发，选完文件就能当场拒绝超限的那些。
    transfers.noteUploadLimit(res.max_upload_bytes ?? 0)
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
  await uploadFiles(files, currentPath.value)
}

/** 一批文件串行传：进度都进「传输」标签页，并行只会互相抢带宽。 */
async function uploadFiles(files: File[], dir: string) {
  if (!files.length || !dir || dropping.value) return
  dropping.value = true
  dropDone.value = 0
  dropTotal.value = files.length
  let ok = 0
  try {
    for (const file of files) {
      const item = await transfers.startUpload(props.serverId, dir, file)
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

// ---- 右键菜单：复制 / 剪切 / 粘贴 / 上传 ----------------------------------------
// 复制、剪切只在面板里记一条「待粘贴」，搬运本身交给服务端做（字节不出服务器，
// 也就不需要走「传输」标签页那套后台任务）。粘贴的目标目录由右键位置决定：
// 右键目录行 = 粘进那个目录，右键文件或空白处 = 粘进当前目录。
type ClipMode = 'copy' | 'cut'

interface ClipItem {
  name: string
  path: string
  mode: ClipMode
}

interface MenuItem extends DarkMenuItem {
  run: () => void
}

const clipboard = ref<ClipItem | null>(null)
const menu = reactive({ open: false, entry: null as SftpEntry | null, left: 0, top: 0 })
const uploadInput = ref<HTMLInputElement | null>(null)
const uploadDir = ref('')

function menuDir(entry: SftpEntry | null): string {
  if (!currentPath.value) return ''
  return entry?.is_dir ? join(currentPath.value, entry.name) : currentPath.value
}

function dirLabel(dir: string): string {
  if (!dir || dir === '/') return '/'
  return dir.split('/').filter(Boolean).pop() || '/'
}

const menuItems = computed<MenuItem[]>(() => {
  const entry = menu.entry
  const dir = menuDir(entry)
  const target = dirLabel(dir)
  const noWrite = !canWrite.value
  const noWriteHint = '没有运维写权限'
  const items: MenuItem[] = []
  if (entry) {
    items.push(
      {
        key: 'copy',
        label: '复制',
        icon: CopyOutlined,
        group: 0,
        disabled: noWrite,
        disabledHint: noWriteHint,
        run: () => setClip('copy', entry),
      },
      {
        key: 'cut',
        label: '剪切',
        icon: ScissorOutlined,
        group: 0,
        disabled: noWrite,
        disabledHint: noWriteHint,
        run: () => setClip('cut', entry),
      },
    )
  }
  const clip = clipboard.value
  items.push({
    key: 'paste',
    label: '粘贴',
    icon: SnippetsOutlined,
    group: 1,
    disabled: !clip || noWrite || !!busy.value || !dir,
    disabledHint: noWrite
      ? noWriteHint
      : !clip
        ? '剪贴板是空的，先右键复制或剪切'
        : !dir
          ? '目录还没加载好'
          : '正在处理上一步操作',
    target,
    targetTitle: dir,
    run: () => doPaste(dir),
  })
  items.push({
    key: 'upload',
    label: '上传',
    icon: UploadOutlined,
    group: 1,
    disabled: noWrite || !dir,
    disabledHint: noWrite ? noWriteHint : '目录还没加载好',
    target,
    targetTitle: dir,
    run: () => pickFiles(dir),
  })
  return items
})

function openMenu(e: MouseEvent, entry: SftpEntry | null) {
  if (entry) selected.value = entry
  menu.entry = entry
  menu.left = e.clientX
  menu.top = e.clientY
  menu.open = true
}

function closeMenu() {
  menu.open = false
  menu.entry = null
}

function onMenuPick(key: string) {
  const item = menuItems.value.find((it) => it.key === key)
  if (!item) return
  // 执行前收菜单：菜单项的 run 闭包已经抓住了目标目录，不依赖 menu.entry。
  closeMenu()
  item.run()
}

function isCut(entry: SftpEntry): boolean {
  const clip = clipboard.value
  return (
    !!clip && clip.mode === 'cut' && clip.path === join(currentPath.value, entry.name)
  )
}

function setClip(mode: ClipMode, entry: SftpEntry) {
  clipboard.value = { name: entry.name, path: join(currentPath.value, entry.name), mode }
}

async function doPaste(dir: string) {
  const clip = clipboard.value
  if (!clip || !dir || busy.value) return
  const verb = clip.mode === 'cut' ? '移动' : '复制'
  busy.value = 'paste'
  busyText.value = `正在${verb} ${clip.name} 到 ${dir} …`
  try {
    const res =
      clip.mode === 'cut'
        ? await opsApi.sftpMove(props.serverId, clip.path, dir)
        : await opsApi.sftpCopy(props.serverId, clip.path, dir)
    // 剪切是一次性的，粘完就清；复制留着，可以连着粘到几个目录。
    if (clip.mode === 'cut') clipboard.value = null
    // 粘进当前目录、或把条目从当前目录剪切走，都要重列；往子目录里粘不动这一屏。
    if (dir === currentPath.value || parent(clip.path) === currentPath.value) {
      await load(currentPath.value)
    }
    message.success(res.renamed ? `已${verb}为 ${dirLabel(res.path)}` : `已${verb} ${clip.name}`)
  } catch (err: any) {
    message.error(err?.message || `${verb}失败`)
  } finally {
    busy.value = ''
    busyText.value = ''
  }
}

function pickFiles(dir: string) {
  uploadDir.value = dir
  uploadInput.value?.click()
}

async function onPickFiles(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  // 清空 value：连着两次选同一个文件时 change 不再被浏览器判定为「没变化」。
  input.value = ''
  await uploadFiles(files, uploadDir.value)
}

// ---- 展示格式 -----------------------------------------------------------------

/** 最新发起的那条在跑的任务（列表头一个 running）。 */
const activeTransfer = computed(() => transfers.items.find((t) => t.status === 'running') ?? null)

const moreRunning = computed(() => Math.max(0, transfers.runningCount - (activeTransfer.value ? 1 : 0)))

const activeText = computed(() => {
  const item = activeTransfer.value
  if (!item) return ''
  const verb = item.kind === 'upload' ? '上传' : '下载'
  const done = item.total
    ? `${Math.min(100, Math.round((item.loaded / item.total) * 100))}%`
    : fmtSize(item.loaded)
  // 上传到 100% 还在跑，是网关和后端在写盘，这里别写成「100%」让人以为完了。
  const tail = transfers.inServerTail(item) ? '写入中' : item.speed ? `${fmtSize(item.speed)}/s` : ''
  return [`${verb} ${item.name}`, done, tail].filter(Boolean).join(' · ')
})

function fmtTime(mtime: number): string {
  if (!mtime) return ''
  const date = new Date(mtime * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

onMounted(() => {
  load('')
})

// 终端右键「在文件浏览器打开」：父页面直接让面板跳到那个目录。
defineExpose({ openPath: (path: string) => load(path) })
</script>

<style scoped>
/* 与终端页同族的深色面板：底 #11181f，发丝线 var(--on-dark-hover)，
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
  z-index: var(--z-dropdown);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 2px dashed rgba(58, 214, 222, 0.6);
  border-radius: var(--tool-radius);
  background: rgba(17, 24, 31, 0.85);
  color: var(--signal-bright);
  font-size: var(--fs-sm);
  pointer-events: none;
}

.sfp-drop-icon {
  font-size: var(--fs-4xl);
}

.sfp-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--on-dark-hover);
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
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dark-2);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: all 0.15s;
}

.sfp-icon-btn:hover:not(:disabled) {
  background: var(--on-dark-hover);
  color: var(--text-dark);
}

.sfp-icon-btn.danger:hover:not(:disabled) {
  background: rgba(255, 77, 79, 0.15);
  color: var(--danger-soft);
}

.sfp-icon-btn:disabled {
  opacity: 0.35;
  cursor: default;
  /* 命中让给外面那层 .sfp-btn-tip：浏览器不给 disabled 控件派发鼠标事件，
     外层 a-tooltip 收不到 mouseenter 就弹不出「没有运维写权限」这句话。 */
  pointer-events: none;
}

/* tooltip 的实际挂载点，尺寸完全由内部按钮撑开，不额外占位。 */
.sfp-btn-tip {
  display: inline-flex;
}

.sfp-path {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-dark-hover);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.6;
  word-break: break-all;
}

.sfp-path-seg {
  padding: 0 2px;
  border: none;
  background: transparent;
  color: var(--text-dark-2);
  font: inherit;
  cursor: pointer;
}

.sfp-path-seg:hover {
  color: var(--signal-bright);
}

.sfp-path-seg:last-of-type {
  color: var(--text-dark);
}

.sfp-path-sep {
  color: #4a5560;
}

.sfp-clip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-bottom: 1px solid var(--on-dark-hover);
  background: rgba(58, 214, 222, 0.07);
  color: var(--text-dark-2);
  font-size: var(--fs-xs);
}

.sfp-clip-icon {
  flex-shrink: 0;
  color: var(--signal-bright);
}

.sfp-clip-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dark);
}

/* 提示语没有信息量时才占位：面板窄就整句藏掉，别让文件名被挤没。 */
.sfp-clip-hint {
  flex-shrink: 0;
  margin-left: auto;
  color: #6e7a86;
}

.sfp-clip-clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dark-2);
  font-size: 10px;
  cursor: pointer;
}

.sfp-clip-clear:hover {
  background: var(--on-dark-hover);
  color: var(--text-dark);
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

/* 剪切中的条目减淡并虚线下划线，和文件管理器「等着被搬走」的观感一致。 */
.sfp-row.clipped .sfp-row-name {
  opacity: 0.5;
  text-decoration: underline dashed;
  text-decoration-color: var(--signal-bright);
  text-underline-offset: 3px;
}

.sfp-row-icon {
  flex-shrink: 0;
  color: var(--text-dark-2);
  font-size: var(--fs-sm);
}

.sfp-row-icon.dir {
  color: var(--signal-bright);
}

.sfp-row-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dark);
  font-size: var(--fs-xs);
}

.sfp-row-size {
  flex-shrink: 0;
  width: 48px;
  color: var(--text-dark-2);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  text-align: right;
}

.sfp-row-time {
  flex-shrink: 0;
  width: 72px;
  color: var(--text-dark-2);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
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
  border-radius: var(--radius-sm);
  background: rgba(58, 214, 222, 0.15);
  color: var(--signal-bright);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.sfp-row:hover .sfp-row-download {
  display: inline-flex;
}

.sfp-empty,
.sfp-truncated {
  padding: 16px 10px;
  color: var(--text-dark-2);
  font-size: var(--fs-xs);
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
  color: var(--warning-bright);
  font-size: var(--fs-2xl);
}

.sfp-error-text {
  color: var(--text-dark-2);
  font-size: var(--fs-xs);
  text-align: center;
  word-break: break-all;
}

.sfp-retry {
  padding: 2px 14px;
  border: 1px solid var(--on-dark-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: #aab6c2;
  font-size: var(--fs-xs);
  cursor: pointer;
}

.sfp-retry:hover {
  border-color: rgba(58, 214, 222, 0.45);
  color: var(--signal-bright);
}

.sfp-foot {
  padding: 5px 10px;
  border-top: 1px solid var(--on-dark-hover);
  color: var(--text-dark-2);
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 传输中的可点击一行：外观沿用 .sfp-foot，只在需要时抢一下注意力。 */
.sfp-foot-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 5px 10px;
  border: none;
  border-top: 1px solid var(--on-dark-hover);
  background: transparent;
  color: var(--signal-bright);
  font: inherit;
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  text-align: left;
  cursor: pointer;
}

.sfp-foot-link:hover {
  background: rgba(58, 214, 222, 0.08);
}

.sfp-foot-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sfp-foot-join {
  flex-shrink: 0;
  color: var(--text-dark-2);
}

/* 菜单项的样式在 DarkContextMenu.vue 里（teleport 到 body，跟着组件走）。 */
.sfp-file-input {
  display: none;
}
</style>
