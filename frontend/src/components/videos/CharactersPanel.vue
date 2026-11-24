<template>
  <div class="characters-panel">
    <div class="toolbar toolbar--between">
      <a-input-search
        v-model:value="keyword"
        placeholder="按角色名搜"
        allow-clear
        size="small"
        style="width: 240px"
        @search="load"
      />
      <a-space :size="10" wrap>
        <span class="muted">
          {{ filtered.length === items.length ? `共 ${total} 个角色` : `筛出 ${filtered.length} / ${total} 个` }}
          · 一个账号最多 {{ maxPerOwner }} 个
        </span>
        <a-badge :count="trashCount" :offset="[-6, 4]">
          <a-button @click="openTrash">
            <DeleteOutlined /> 回收站
          </a-button>
        </a-badge>
        <a-button type="primary" :disabled="items.length >= maxPerOwner" @click="openCreate">
          <PlusOutlined /> 新建人物
        </a-button>
      </a-space>
    </div>

    <a-spin :spinning="loading">
      <ul v-if="filtered.length" class="character-grid">
        <li v-for="item in filtered" :key="item.id" class="character-cell">
          <div class="character-portrait">
            <img
              v-if="item.thumb_url || item.url"
              :src="item.thumb_url || item.url"
              :alt="item.name"
              loading="lazy"
              @click="preview(item)"
            />
            <span v-else class="portrait-missing">形象图已丢失</span>
            <a-tag v-if="item.project_count" class="project-tag" color="processing">
              {{ item.project_count }} 项目在用
            </a-tag>
          </div>
          <div class="character-meta">
            <div class="character-name" :title="item.name">{{ item.name }}</div>
            <div v-if="item.persona" class="character-line" :title="item.persona">
              <span class="line-label">人设</span>{{ item.persona }}
            </div>
            <div v-if="item.outfit" class="character-line" :title="item.outfit">
              <span class="line-label">衣着</span>{{ item.outfit }}
            </div>
            <div v-if="item.features" class="character-line" :title="item.features">
              <span class="line-label">特色</span>{{ item.features }}
            </div>
          </div>
          <div class="character-ops">
            <a-button size="small" type="link" @click="openEdit(item)">编辑</a-button>
            <a-button size="small" type="link" danger @click="askDelete(item)">删除</a-button>
          </div>
        </li>
      </ul>
      <a-empty
        v-else
        :image="simpleEmpty"
        :description="keyword.trim() ? '没有符合的角色' : '人物库还是空的，建一个带形象图的角色吧'"
      />
    </a-spin>

    <!-- 建档 / 编辑：同一套表单，区别只在有没有 id 与是否已存在形象图。 -->
    <a-modal
      v-model:open="editor.open"
      :title="editor.id ? '编辑人物' : '新建人物'"
      :confirm-loading="editor.saving"
      ok-text="保存"
      cancel-text="取消"
      width="560px"
      @ok="onSave"
    >
      <a-form layout="vertical" class="editor-form">
        <a-form-item label="形象图" required>
          <a-upload
            list-type="picture-card"
            :show-upload-list="false"
            :before-upload="pickFile"
            accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
          >
            <img v-if="previewUrl" class="upload-preview" :src="previewUrl" alt="形象图预览" />
            <div v-else class="upload-placeholder">
              <UploadOutlined />
              <span>{{ editor.id ? '更换形象图' : '上传形象图' }}</span>
            </div>
          </a-upload>
          <div class="hint">只收图片（PNG / JPEG / GIF / WebP / BMP），视频会被服务端拒绝。</div>

          <!-- AI 生成：后端同步出一张正面/侧面/背面三视角合一的设定图，转成 File
               走与手工上传完全相同的保存路径，取消或重生成都不会在服务器留垃圾。 -->
          <div class="ai-portrait">
            <div class="ai-portrait-head">
              <span class="ai-portrait-title"><ThunderboltOutlined /> AI 生成三视图形象</span>
              <a-button type="link" size="small" @click="fillPromptFromProfile">
                填入档案描述
              </a-button>
            </div>
            <a-textarea
              v-model:value="genPrompt"
              :rows="3"
              :maxlength="1000"
              show-count
              placeholder="描述性别年龄、发型、脸型、体型、服装与标志物，例如：28 岁女性，齐肩黑发、圆框眼镜，米白色西装外套、牛仔裤、白球鞋"
            />
            <div class="ai-portrait-actions">
              <a-button
                type="primary"
                ghost
                :loading="generating"
                :disabled="!genPrompt.trim()"
                @click="onGeneratePortrait"
              >
                <template #icon><ThunderboltOutlined /></template>
                生成三视图
              </a-button>
              <span class="hint">
                正面 / 侧面 / 背面三视角合一，按次计费；生成后替换当前形象图，点保存才落库
              </span>
            </div>
          </div>
        </a-form-item>
        <a-form-item label="角色名" required>
          <a-input v-model:value="editor.name" :maxlength="64" placeholder="例如：林小夏" />
        </a-form-item>
        <a-form-item label="人设">
          <a-textarea
            v-model:value="editor.persona"
            :rows="2"
            :maxlength="512"
            show-count
            placeholder="身份、性格、背景、说话方式。脚本生成时会原样喂给策划师与分镜师。"
          />
        </a-form-item>
        <a-form-item label="衣着">
          <a-textarea
            v-model:value="editor.outfit"
            :rows="2"
            :maxlength="512"
            show-count
            placeholder="固定穿搭，例如：米白色西装外套、牛仔裤、白球鞋"
          />
        </a-form-item>
        <a-form-item label="外形特色">
          <a-textarea
            v-model:value="editor.features"
            :rows="2"
            :maxlength="512"
            show-count
            placeholder="一眼能认出的视觉特征：发型、体型、标志物"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 删除确认：先进回收期，窗口内可在回收站撤销，过期才真删行与形象图。 -->
    <a-modal
      v-model:open="deleteOpen"
      :title="`删除人物「${deleteTarget?.name ?? ''}」`"
      ok-text="移入回收站"
      cancel-text="取消"
      :confirm-loading="deleting"
      :ok-button-props="{ danger: true }"
      @ok="confirmDelete"
    >
      <a-spin :spinning="loadingUsage">
        <a-alert
          type="warning"
          show-icon
          :message="`先移入回收站，${undoWindowLabel}内可在回收站撤销；过期才真正删除形象图。`"
          class="delete-alert"
        />
        <div v-if="usageProjects.length" class="usage-block">
          <div class="hint">这些短视频项目仍把它选在角色组里，回收期内引用保留，过期真删后保存或渲染时自动跳过：</div>
          <ul class="usage-list">
            <li v-for="project in usageProjects" :key="project.project_id">《{{ project.title }}》</li>
          </ul>
        </div>
        <div v-else-if="!loadingUsage" class="hint">没有短视频项目引用它。</div>
      </a-spin>
    </a-modal>

    <!-- 回收站：软删的角色在撤销窗口内都在这里，到期才真删行与形象图。 -->
    <a-drawer v-model:open="trashOpen" title="回收站" width="680">
      <div class="hint trash-hint">
        删除的角色先在这里放 {{ undoWindowLabel }}，到期才真正删除；恢复后项目角色组里
        对它的引用原样生效。名额已满时，恢复会先放回更早删除的，其余继续留在这里。
      </div>
      <a-button
        size="small"
        style="margin-bottom: 10px"
        :disabled="!trashItems.length"
        @click="restoreAllTrash"
      >
        <UndoOutlined /> 全部恢复
      </a-button>
      <a-spin :spinning="trashLoading">
        <ul v-if="trashItems.length" class="trash-list">
          <li v-for="item in trashItems" :key="item.id" class="trash-row">
            <span class="trash-thumb">
              <img
                v-if="item.thumb_url || item.url"
                :src="item.thumb_url || item.url"
                :alt="item.name"
                loading="lazy"
              />
              <span v-else class="portrait-missing">已丢失</span>
            </span>
            <span class="trash-body">
              <span class="trash-name" :title="item.name">{{ item.name }}</span>
              <span class="hint">删除于 {{ fmtShortDateTime(item.deleted_at) }}</span>
            </span>
            <span class="trash-side">
              <a-tag :color="remainColor(remainingOf(item))">{{ remainLabel(remainingOf(item)) }}</a-tag>
              <a-button size="small" type="link" @click="restoreOne(item.id)">
                <UndoOutlined /> 恢复
              </a-button>
            </span>
          </li>
        </ul>
        <a-empty v-else :image="simpleEmpty" description="回收站是空的" />
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 人物库（短视频页的「人物库」页签）：账号级的角色档案与形象图。
// 这个面板做五件事：拉列表（名称搜索走后端，带项目引用数）、卡片网格展示档案、
// 建档/编辑（multipart 一次收图与文字，重名给二次确认但不硬拦）、删除（先进回收期，
// 回收站可撤销，过期才真删）、回收站恢复。残留引用交给视频侧在保存/渲染时按 id 缺失跳过。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  DeleteOutlined,
  PlusOutlined,
  ThunderboltOutlined,
  UndoOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { Empty, Modal, message } from 'ant-design-vue'
import {
  charactersApi,
  type Character,
  type CharacterTrashItem,
  type CharacterUsageProject,
} from '@/api'
import { blobErrorMessage, errorText } from '@/utils/errors'
import { fmtShortDateTime } from '@/utils/format'

const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp']
const FETCH_LIMIT = 200

const items = ref<Character[]>([])
const total = ref(0)
const maxPerOwner = ref(50)
const loading = ref(false)
const keyword = ref('')
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE
const trashCount = ref(0)
const trashTtlSeconds = ref(600)

const undoWindowLabel = computed(() => {
  const seconds = trashTtlSeconds.value
  if (seconds <= 0) return '撤销窗口'
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
})

const filtered = computed(() => {
  // 后端已按关键字过滤；本地这层只在没翻页（一次拉满）时兜住输入过程的即时感。
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter((item) => item.name.toLowerCase().includes(kw))
})

const editor = reactive<{
  open: boolean
  saving: boolean
  id: number | null
  name: string
  persona: string
  outfit: string
  features: string
  file: File | null
  currentUrl: string
}>({
  open: false,
  saving: false,
  id: null,
  name: '',
  persona: '',
  outfit: '',
  features: '',
  file: null,
  currentUrl: '',
})

const previewUrl = ref('')
let previewObjectUrl: string | null = null

function releasePreview() {
  if (previewObjectUrl) {
    URL.revokeObjectURL(previewObjectUrl)
    previewObjectUrl = null
  }
  previewUrl.value = ''
}

onBeforeUnmount(releasePreview)

// AI 三视图生成：genPrompt 是用户可改的角色描述，generating 跟一次真计费调用。
const genPrompt = ref('')
const generating = ref(false)

// 后端 sniff 认出来的图片 MIME 都在这张表里；认不出时按 png 落，保存时后端还会再判一次。
const MIME_EXTENSIONS: Record<string, string> = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/webp': '.webp',
  'image/gif': '.gif',
  'image/bmp': '.bmp',
}

const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTarget = ref<Character | null>(null)
const loadingUsage = ref(false)
const usageProjects = ref<CharacterUsageProject[]>([])

async function load() {
  loading.value = true
  try {
    const data = await charactersApi.list({
      q: keyword.value.trim() || undefined,
      limit: FETCH_LIMIT,
    })
    items.value = data.items
    total.value = data.total
    maxPerOwner.value = data.max_per_owner
    trashCount.value = data.trash_count ?? 0
    trashTtlSeconds.value = data.trash_ttl_seconds ?? 600
  } catch (err) {
    message.error(errorText(err, '人物库读不出来'))
  } finally {
    loading.value = false
  }
}

// -- 回收站 ----------------------------------------------------------------

const trashOpen = ref(false)
const trashLoading = ref(false)
const trashItems = ref<CharacterTrashItem[]>([])
let trashFetchedAt = 0
const trashTick = ref(0)
let trashTimer: ReturnType<typeof setInterval> | null = null

function remainingOf(item: CharacterTrashItem): number {
  void trashTick.value
  const elapsed = trashFetchedAt ? Math.floor((Date.now() - trashFetchedAt) / 1000) : 0
  return Math.max(0, item.expires_in_seconds - elapsed)
}

function remainLabel(seconds: number): string {
  if (seconds <= 0) return '即将清除'
  if (seconds < 60) return `还剩 ${seconds} 秒`
  return `还剩 ${Math.ceil(seconds / 60)} 分钟`
}

function remainColor(seconds: number): string {
  if (seconds <= 0) return 'error'
  if (seconds <= 60) return 'warning'
  return 'default'
}

async function loadTrash() {
  trashLoading.value = true
  try {
    const data = await charactersApi.trash({ limit: FETCH_LIMIT })
    trashItems.value = data.items
    trashCount.value = data.total
    trashFetchedAt = Date.now()
    trashTick.value = 0
  } catch (err) {
    message.error(errorText(err, '回收站读不出来'))
  } finally {
    trashLoading.value = false
  }
}

function openTrash() {
  trashOpen.value = true
  void loadTrash()
}

async function restoreOne(id: number) {
  try {
    await charactersApi.restore([id])
    message.success('已恢复人物')
    await Promise.all([loadTrash(), load()])
  } catch (err) {
    message.error(errorText(err, '恢复失败'))
  }
}

async function restoreAllTrash() {
  const ids = trashItems.value.map((item) => item.id)
  if (!ids.length) return
  try {
    const result = await charactersApi.restore(ids)
    message.success(`已恢复 ${result.ids.length} 个人物`)
    await Promise.all([loadTrash(), load()])
  } catch (err) {
    message.error(errorText(err, '恢复失败'))
  }
}

watch(trashOpen, (open) => {
  if (open && !trashTimer) {
    trashTimer = setInterval(() => {
      trashTick.value += 1
      if (trashItems.value.some((item) => remainingOf(item) <= 0)) void loadTrash()
    }, 30_000)
  } else if (!open && trashTimer) {
    clearInterval(trashTimer)
    trashTimer = null
  }
})

onBeforeUnmount(() => {
  if (trashTimer) clearInterval(trashTimer)
})

function resetEditor() {
  editor.id = null
  editor.name = ''
  editor.persona = ''
  editor.outfit = ''
  editor.features = ''
  editor.file = null
  editor.currentUrl = ''
  genPrompt.value = ''
  releasePreview()
}

function openCreate() {
  resetEditor()
  editor.open = true
}

function openEdit(item: Character) {
  resetEditor()
  editor.id = item.id
  editor.name = item.name
  editor.persona = item.persona
  editor.outfit = item.outfit
  editor.features = item.features
  editor.currentUrl = item.thumb_url || item.url
  previewUrl.value = editor.currentUrl
  editor.open = true
}

function pickFile(file: File) {
  if (!ACCEPTED_TYPES.includes(file.type)) {
    message.warning('只支持 PNG / JPEG / GIF / WebP / BMP 图片')
    return false
  }
  editor.file = file
  releasePreview()
  previewObjectUrl = URL.createObjectURL(file)
  previewUrl.value = previewObjectUrl
  // 返回 false 阻止 antd 自动上传，保存时随表单一起发。
  return false
}

/** 把已填的文字档案拼成一段描述，给不会写提示词的人起步，拼完仍可手工改。 */
function fillPromptFromProfile() {
  const parts = [
    editor.name.trim() && `角色：${editor.name.trim()}`,
    editor.persona.trim() && `身份人设：${editor.persona.trim()}`,
    editor.outfit.trim() && `衣着：${editor.outfit.trim()}`,
    editor.features.trim() && `外形特色：${editor.features.trim()}`,
  ].filter(Boolean)
  if (!parts.length) {
    message.info('先填写角色名或人设 / 衣着 / 外形，也可以直接手写描述')
    return
  }
  genPrompt.value = parts.join('；')
}

/** 调后端同步生成三视图，拿到的二进制转成 File 走与手工上传完全相同的保存路径。 */
async function onGeneratePortrait() {
  const prompt = genPrompt.value.trim()
  if (!prompt) {
    message.warning('先写一段角色外形描述')
    return
  }
  generating.value = true
  try {
    const blob = await charactersApi.portraitPreview(prompt)
    const mime = blob.type || 'image/png'
    const file = new File([blob], `ai-turnaround${MIME_EXTENSIONS[mime] ?? '.png'}`, {
      type: mime,
    })
    editor.file = file
    releasePreview()
    previewObjectUrl = URL.createObjectURL(file)
    previewUrl.value = previewObjectUrl
    message.success('三视图已生成，确认无误后点保存')
  } catch (err) {
    // 失败响应是 blob 里的 JSON 信封，拦截器拆不到，自己读 message。
    message.error(await blobErrorMessage(err, '三视图生成失败'))
  } finally {
    generating.value = false
  }
}

/** 同名角色（排除编辑中的自身）：后端允许重名，这里只做提示，不硬拦。 */
function duplicateNames(): Character[] {
  const name = editor.name.trim().toLowerCase()
  if (!name) return []
  return items.value.filter(
    (item) => item.id !== editor.id && item.name.trim().toLowerCase() === name,
  )
}

async function onSave() {
  if (!editor.name.trim()) {
    message.warning('请先填写角色名')
    return
  }
  if (editor.id === null && !editor.file) {
    message.warning('请先上传一张形象图')
    return
  }
  const dup = duplicateNames()
  if (dup.length) {
    // 重名在选择器里容易选错，给一次显式确认；用户坚持就照存，后端不做唯一约束。
    Modal.confirm({
      title: '存在同名角色',
      content: `人物库里已有 ${dup.length} 个叫「${editor.name.trim()}」的角色，仍要保存？`,
      okText: '仍要保存',
      cancelText: '取消',
      onOk: () => doSave(),
    })
    return
  }
  await doSave()
}

async function doSave() {
  editor.saving = true
  try {
    if (editor.id === null) {
      // 建档：图与文字一次 multipart 提交。
      await charactersApi.create(editor.file as File, {
        name: editor.name.trim(),
        persona: editor.persona,
        outfit: editor.outfit,
        features: editor.features,
      })
      message.success('已建立人物档案')
    } else {
      // 编辑：文字档案先存；换了图再补发一次换图请求，两步各自独立可重试。
      await charactersApi.update(editor.id, {
        name: editor.name.trim(),
        persona: editor.persona,
        outfit: editor.outfit,
        features: editor.features,
      })
      if (editor.file) {
        await charactersApi.replacePortrait(editor.id, editor.file)
      }
      message.success('已更新人物档案')
    }
    editor.open = false
    await load()
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    editor.saving = false
  }
}

async function askDelete(item: Character) {
  deleteTarget.value = item
  usageProjects.value = []
  deleteOpen.value = true
  loadingUsage.value = true
  try {
    const usage = await charactersApi.usage([item.id])
    usageProjects.value = usage[0]?.projects ?? []
  } catch {
    // 影响面读不出来不阻断删除：后端删除本身不依赖它，提示文案按「未知」处理。
    usageProjects.value = []
  } finally {
    loadingUsage.value = false
  }
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await charactersApi.remove(deleteTarget.value.id)
    message.success('已移入回收站，可在撤销窗口内恢复')
    deleteOpen.value = false
    await load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  } finally {
    deleting.value = false
  }
}

function preview(item: Character) {
  if (!item.url) return
  window.open(item.url, '_blank', 'noopener')
}

onMounted(load)
</script>

<style scoped>
.character-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.character-cell {
  display: flex;
  flex-direction: column;
  padding: 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--surface, transparent);
}

.character-portrait {
  position: relative;
  width: 100%;
  aspect-ratio: 3 / 4;
  overflow: hidden;
  border-radius: 8px;
  background: var(--surface-2);
}

.character-portrait img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  cursor: zoom-in;
}

.portrait-missing {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.project-tag {
  position: absolute;
  top: 6px;
  right: 6px;
  margin: 0;
  transform: scale(0.9);
  transform-origin: right top;
}

/* 回收站抽屉：与素材回收站同口径，头像 + 名称 + 倒计时 + 恢复。 */
.trash-hint {
  margin-bottom: 10px;
}

.trash-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.trash-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface);
}

.trash-thumb {
  flex: 0 0 auto;
  width: 48px;
  height: 64px;
  overflow: hidden;
  border-radius: 6px;
  background: var(--surface-2);
}

.trash-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.trash-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.trash-name {
  font-weight: 600;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.trash-side {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.character-meta {
  flex: 1 1 auto;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.character-name {
  font-weight: 600;
  font-size: var(--fs-md);
}

.character-line {
  display: -webkit-box;
  overflow: hidden;
  color: var(--text-2);
  font-size: var(--fs-xs);
  line-height: 1.5;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.line-label {
  margin-right: 4px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--surface-raised, rgba(0, 0, 0, 0.05));
  color: var(--text-3);
}

.character-ops {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
}

.upload-preview {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.upload-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

/* AI 生成区：虚线块与上传区视觉分组，表达「两条任选其一」的形象图来源。 */
.ai-portrait {
  margin-top: 10px;
  padding: 12px;
  border: 1px dashed var(--hairline);
  border-radius: 8px;
  background: var(--surface-2);
}

.ai-portrait-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.ai-portrait-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: var(--fs-sm);
}

.ai-portrait-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
}

.delete-alert {
  margin-bottom: 10px;
}

.usage-block {
  margin-top: 4px;
}

.usage-list {
  margin: 6px 0 0;
  padding-left: 18px;
}

.editor-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}
</style>
