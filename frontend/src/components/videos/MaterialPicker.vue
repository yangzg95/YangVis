<template>
  <a-modal
    v-model:open="visible"
    title="从素材库选择"
    width="760px"
    :ok-text="picked.length ? `确定（已选 ${picked.length}）` : '确定（不选用）'"
    cancel-text="取消"
    @ok="onConfirm"
  >
    <div class="toolbar toolbar--between toolbar--snug">
      <a-space :size="8" wrap>
        <a-radio-group v-model:value="kind" button-style="solid" size="small" @change="load">
          <a-radio-button value="">全部</a-radio-button>
          <a-radio-button value="image">图片</a-radio-button>
          <a-radio-button value="video">视频</a-radio-button>
        </a-radio-group>
        <a-input
          v-model:value="keyword"
          allow-clear
          size="small"
          class="picker-search"
          placeholder="按名称搜"
          @press-enter="load"
        >
          <template #suffix><SearchOutlined @click="load" /></template>
        </a-input>
      </a-space>
      <a-upload :before-upload="onUpload" :show-upload-list="false" :accept="ACCEPT" multiple>
        <a-button size="small" :loading="uploading"><UploadOutlined /> 传到库里</a-button>
      </a-upload>
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
            <!-- 缩略图没有时回退原图：缩略图是提速件，缺了不该让这格空白。 -->
            <img v-if="item.kind === 'image'" :src="item.thumb_url || item.url" :alt="item.name" loading="lazy" />
            <video v-else-if="item.url" :src="item.url" preload="metadata" muted playsinline />
            <span v-else class="thumb-missing">已丢失</span>
          </span>
          <span class="picker-name" :title="item.name">
            {{ item.name }}
            <a-tag v-if="item.tag" class="picker-tag">{{ item.tag }}</a-tag>
          </span>
          <span class="hint">
            {{ item.kind === 'video' ? '视频' : '图片' }} · {{ fmtSize(item.size) }}
          </span>
        </li>
      </ul>
      <a-empty
        v-else
        :image="simpleEmpty"
        :description="
          keyword.trim() || kind ? '库里没有符合的素材' : '素材库还是空的，先传一张人物形象图'
        "
      />
    </a-spin>

    <div class="hint picker-foot">
      一镜最多 {{ max }} 条：再多参考图会互相干扰，出片反而不稳。视频素材这一期只记录引用，
      进首帧请求的只有图片。
    </div>
  </a-modal>
</template>

<script setup lang="ts">
// 镜头级的素材选择框：从账号级素材库里挑，而不是在项目里再传一份。
// 库按账号归、镜头按项目走，所以这里只回 id 列表——素材本身的存在与删除归素材库页管。
import { computed, ref, watch } from 'vue'
import { Empty, message } from 'ant-design-vue'
import { SearchOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { materialsApi, type MaterialAsset, type MaterialKind } from '@/api'
import { errorText } from '@/utils/errors'
import { fmtSize } from '@/utils/format'

const props = withDefaults(
  defineProps<{ open: boolean; selected: string[]; max?: number }>(),
  { max: 4 },
)
const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [ids: string[]]
}>()

// 一次取满一屏：后端的上限就是账号上限，超出再靠搜索收窄。
const PAGE_LIMIT = 200
const ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/bmp,video/mp4,video/webm'

const visible = computed({
  get: () => props.open,
  set: (value: boolean) => emit('update:open', value),
})

const items = ref<MaterialAsset[]>([])
const picked = ref<string[]>([])
const loading = ref(false)
const uploading = ref(false)
const kind = ref<'' | MaterialKind>('')
const keyword = ref('')
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

async function load() {
  loading.value = true
  try {
    const data = await materialsApi.list({
      kind: kind.value || undefined,
      q: keyword.value.trim() || undefined,
      limit: PAGE_LIMIT,
    })
    items.value = data.items
  } catch (err) {
    message.error(errorText(err, '素材库读不出来'))
  } finally {
    loading.value = false
  }
}

function onOpenChange() {
  // 每次打开都重新对一遍库：素材可能在素材库页刚被删掉，用旧列表会挑出已经不存在的 id。
  picked.value = [...props.selected]
  void load()
}

// 这一版 ant-design-vue 的 Modal 只在关闭时 emit('change')，没有任何「打开」事件，
// 所以「打开时对一遍库」只能自己盯 props.open——挂在 @open-change 上是一次都不会触发的。
watch(
  () => props.open,
  (open) => {
    if (open) onOpenChange()
  },
)

function toggle(item: MaterialAsset) {
  const id = String(item.id)
  if (picked.value.includes(id)) {
    picked.value = picked.value.filter((value) => value !== id)
  } else if (picked.value.length < props.max) {
    picked.value = [...picked.value, id]
  }
}

function onConfirm() {
  // 只在点「确定」时回传：关掉这一框而不点确定，不该改镜头上已有的引用。
  emit('confirm', picked.value)
}

async function onUpload(file: File) {
  uploading.value = true
  try {
    await materialsApi.upload(file)
    message.success(`已存入素材库：${file.name}`)
    await load()
  } catch (err) {
    message.error(errorText(err, `上传 ${file.name} 失败`))
  } finally {
    uploading.value = false
  }
  return false
}
</script>

<style scoped>
.picker-search {
  width: 200px;
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
  width: 44px;
  height: 44px;
  overflow: hidden;
  border-radius: 6px;
  background: var(--surface-2);
}

.picker-thumb img,
.picker-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.thumb-missing {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.picker-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.picker-tag {
  margin-left: 6px;
  transform: scale(0.86);
  vertical-align: middle;
}

.picker-foot {
  margin-top: 10px;
}
</style>
