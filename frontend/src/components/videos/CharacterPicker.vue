<template>
  <a-modal
    v-model:open="visible"
    title="从人物库选择出场角色"
    width="760px"
    :ok-text="picked.length ? `确定（已选 ${picked.length}）` : '确定（不选用）'"
    cancel-text="取消"
    @ok="onConfirm"
  >
    <div class="toolbar toolbar--between toolbar--snug">
      <a-input
        v-model:value="keyword"
        allow-clear
        size="small"
        class="picker-search"
        placeholder="按角色名搜，回车确认"
        @press-enter="load"
      >
        <template #suffix><SearchOutlined @click="load" /></template>
      </a-input>
      <a-button size="small" type="primary" ghost @click="toggleCreate">
        <PlusOutlined /> {{ showCreate ? '收起新建' : '新建角色' }}
      </a-button>
    </div>

    <!-- 内联简版建档：名字 + 形象图即可，档案三字段折叠可选，建好自动勾选，不跳页。 -->
    <div v-if="showCreate" class="create-panel">
      <div class="create-row">
        <a-upload
          list-type="picture-card"
          :show-upload-list="false"
          :before-upload="pickFile"
          accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
          class="create-upload"
        >
          <img v-if="previewUrl" class="create-preview" :src="previewUrl" alt="形象图预览" />
          <div v-else class="create-upload-ph">
            <UploadOutlined />
            <span>形象图</span>
          </div>
        </a-upload>
        <div class="create-fields">
          <a-input
            v-model:value="createName"
            size="small"
            :maxlength="64"
            placeholder="角色名（必填），例如：林小夏"
            @press-enter="submitCreate"
          />
          <a-button size="small" type="link" class="create-more" @click="showMore = !showMore">
            {{ showMore ? '收起档案字段' : '补充人设/衣着/特色（可选）' }}
          </a-button>
          <a-textarea
            v-if="showMore"
            v-model:value="createPersona"
            size="small"
            :rows="2"
            :maxlength="512"
            placeholder="人设：身份、性格、说话方式"
          />
          <a-textarea
            v-if="showMore"
            v-model:value="createOutfit"
            size="small"
            :rows="2"
            :maxlength="512"
            placeholder="衣着：固定穿搭"
            class="create-textarea"
          />
          <a-textarea
            v-if="showMore"
            v-model:value="createFeatures"
            size="small"
            :rows="2"
            :maxlength="512"
            placeholder="外形特色：发型、体型、标志物"
            class="create-textarea"
          />
        </div>
      </div>
      <div class="create-actions">
        <span class="hint">建档后自动勾选；完整档案之后可在人物库页补。</span>
        <a-button size="small" type="primary" :loading="creating" @click="submitCreate">
          保存并选用
        </a-button>
      </div>
    </div>

    <a-spin :spinning="loading">
      <ul v-if="items.length" class="picker-list">
        <li v-for="item in items" :key="item.id" class="picker-row">
          <a-checkbox
            :checked="picked.includes(String(item.id))"
            :disabled="!picked.includes(String(item.id)) && picked.length >= max"
            @change="toggle(item)"
          />
          <span class="picker-thumb">
            <img
              v-if="item.thumb_url || item.url"
              :src="item.thumb_url || item.url"
              :alt="item.name"
              loading="lazy"
            />
            <span v-else class="thumb-missing">已丢失</span>
          </span>
          <span class="picker-body">
            <span class="picker-name" :title="item.name">{{ item.name }}</span>
            <span class="picker-meta">
              <span v-if="item.persona" :title="item.persona">人设：{{ item.persona }}</span>
              <span v-if="item.outfit" :title="item.outfit">衣着：{{ item.outfit }}</span>
            </span>
          </span>
        </li>
      </ul>
      <a-empty
        v-else
        :image="simpleEmpty"
        :description="keyword.trim() ? '人物库里没有符合的角色' : '人物库还是空的，先去建一个角色'"
      />
    </a-spin>

    <div class="hint picker-foot">
      一条片子最多选 {{ max }} 个出场角色：他们的档案会进脚本提示词，形象图会作为参考图
      注入每个镜头的首帧渲染（与镜头素材合计最多 4 张参考图）。
    </div>
  </a-modal>
</template>

<script setup lang="ts">
// 项目级角色组选择框：从账号级人物库挑，只回 id 列表。
// 弹窗内带一套简版建档（名字 + 形象图必填，人设/衣着/特色折叠可选）：建好就地勾选，
// 不用跳去人物库页再回来重选；完整档案的维护仍归人物库页。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Empty, message } from 'ant-design-vue'
import { PlusOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { charactersApi, MAX_CHARACTERS_PER_PROJECT, type Character } from '@/api'
import { errorText } from '@/utils/errors'

const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp']

const props = withDefaults(
  defineProps<{ open: boolean; selected: string[]; max?: number }>(),
  { max: MAX_CHARACTERS_PER_PROJECT },
)
const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [ids: string[]]
}>()

const PAGE_LIMIT = 200

const visible = computed({
  get: () => props.open,
  set: (value: boolean) => emit('update:open', value),
})

const items = ref<Character[]>([])
const picked = ref<string[]>([])
const loading = ref(false)
const keyword = ref('')
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

async function load() {
  loading.value = true
  try {
    const data = await charactersApi.list({
      q: keyword.value.trim() || undefined,
      limit: PAGE_LIMIT,
    })
    items.value = data.items
  } catch (err) {
    message.error(errorText(err, '人物库读不出来'))
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    // 每次打开都对一遍库：角色可能刚被删掉，用旧列表会挑出不存在的 id。
    picked.value = [...props.selected]
    keyword.value = ''
    resetCreate()
    void load()
  },
)

// -- 内联建档：名字 + 形象图是必填，档案三字段可选，建好就地勾选，不打断选角流程。
const showCreate = ref(false)
const showMore = ref(false)
const creating = ref(false)
const createName = ref('')
const createPersona = ref('')
const createOutfit = ref('')
const createFeatures = ref('')
const createFile = ref<File | null>(null)
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

function resetCreate() {
  showCreate.value = false
  showMore.value = false
  createName.value = ''
  createPersona.value = ''
  createOutfit.value = ''
  createFeatures.value = ''
  createFile.value = null
  releasePreview()
}

function toggleCreate() {
  if (showCreate.value) resetCreate()
  else showCreate.value = true
}

function pickFile(file: File) {
  if (!ACCEPTED_TYPES.includes(file.type)) {
    message.warning('只支持 PNG / JPEG / GIF / WebP / BMP 图片')
    return false
  }
  createFile.value = file
  releasePreview()
  previewObjectUrl = URL.createObjectURL(file)
  previewUrl.value = previewObjectUrl
  // 拦住 antd 自动上传，保存时随 multipart 一起发。
  return false
}

async function submitCreate() {
  const name = createName.value.trim()
  if (!name) {
    message.warning('请先填写角色名')
    return
  }
  if (!createFile.value) {
    message.warning('请先上传一张形象图')
    return
  }
  creating.value = true
  try {
    const created = await charactersApi.create(createFile.value, {
      name,
      persona: createPersona.value.trim() || undefined,
      outfit: createOutfit.value.trim() || undefined,
      features: createFeatures.value.trim() || undefined,
    })
    await load()
    const id = String(created.id)
    if (!picked.value.includes(id)) {
      if (picked.value.length >= props.max) {
        message.warning(`一条片子最多选 ${props.max} 个角色，已建档但没自动勾选，请手动调整`)
      } else {
        picked.value = [...picked.value, id]
      }
    }
    message.success(`已建立角色「${name}」并选用`)
    resetCreate()
  } catch (err) {
    message.error(errorText(err, '建档失败'))
  } finally {
    creating.value = false
  }
}

function toggle(item: Character) {
  const id = String(item.id)
  if (picked.value.includes(id)) {
    picked.value = picked.value.filter((value) => value !== id)
  } else if (picked.value.length < props.max) {
    picked.value = [...picked.value, id]
  }
}

function onConfirm() {
  // 只在点「确定」时回传：关掉这一框而不确定，不该改项目上已有的角色组。
  emit('confirm', picked.value)
}
</script>

<style scoped>
.picker-search {
  width: 240px;
}

.picker-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 46vh;
  margin: 8px 0 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.picker-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
}

.picker-thumb {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 48px;
  height: 48px;
  overflow: hidden;
  border-radius: 6px;
  background: var(--surface-2);
}

.picker-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.thumb-missing {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.picker-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.picker-name {
  font-weight: 600;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.picker-meta {
  display: flex;
  flex-direction: column;
  color: var(--text-3);
  font-size: var(--fs-xs);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.picker-foot {
  margin-top: 10px;
}

/* 内联建档面板：上传格压到 72px，与右侧字段并排，不把选择列表挤到折叠下面。 */
.create-panel {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface-2);
}

.create-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.create-upload :deep(.ant-upload.ant-upload-select) {
  width: 72px;
  height: 72px;
  margin: 0;
}

.create-upload-ph {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.create-preview {
  width: 72px;
  height: 72px;
  object-fit: cover;
}

.create-fields {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.create-more {
  align-self: flex-start;
  padding: 0;
  height: auto;
}

.create-textarea {
  margin-top: -2px;
}

.create-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}
</style>
