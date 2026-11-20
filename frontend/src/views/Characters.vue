<template>
  <div class="page-card">
    <div class="page-title">人物库</div>

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

    <!-- 删除确认：先拉引用影响面，硬删除后项目里的残留 id 在保存/渲染时自动跳过。 -->
    <a-modal
      v-model:open="deleteOpen"
      :title="`删除人物「${deleteTarget?.name ?? ''}」`"
      ok-text="删除"
      cancel-text="取消"
      :confirm-loading="deleting"
      :ok-button-props="{ danger: true }"
      @ok="confirmDelete"
    >
      <a-spin :spinning="loadingUsage">
        <a-alert
          type="warning"
          show-icon
          message="删除后形象图文件一并清除，且不能撤销。"
          class="delete-alert"
        />
        <div v-if="usageProjects.length" class="usage-block">
          <div class="hint">这些短视频项目仍把它选在角色组里，删除后保存或渲染时会自动跳过该角色：</div>
          <ul class="usage-list">
            <li v-for="project in usageProjects" :key="project.project_id">《{{ project.title }}》</li>
          </ul>
        </div>
        <div v-else-if="!loadingUsage" class="hint">没有短视频项目引用它，可以放心删除。</div>
      </a-spin>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 人物库：账号级的角色档案与形象图。
// 这一页做四件事：拉列表（名称搜索走后端）、卡片网格展示档案、建档/编辑
// （multipart 一次收图与文字，编辑只改文字时不带文件）、删除（先列引用项目，
// 硬删除，残留引用交给视频侧在保存/渲染时按 id 缺失跳过）。
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { PlusOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import {
  charactersApi,
  type Character,
  type CharacterUsageProject,
} from '@/api'
import { errorText } from '@/utils/errors'

const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp']
const FETCH_LIMIT = 200

const items = ref<Character[]>([])
const total = ref(0)
const maxPerOwner = ref(50)
const loading = ref(false)
const keyword = ref('')
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

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
  } catch (err) {
    message.error(errorText(err, '人物库读不出来'))
  } finally {
    loading.value = false
  }
}

function resetEditor() {
  editor.id = null
  editor.name = ''
  editor.persona = ''
  editor.outfit = ''
  editor.features = ''
  editor.file = null
  editor.currentUrl = ''
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

async function onSave() {
  if (!editor.name.trim()) {
    message.warning('请先填写角色名')
    return
  }
  if (editor.id === null && !editor.file) {
    message.warning('请先上传一张形象图')
    return
  }
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
    message.success('已删除人物')
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
