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
      <a-button size="small" @click="goLibrary">
        <PlusOutlined /> 去人物库新建
      </a-button>
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
// 与 MaterialPicker 的分工：素材选择器允许弹窗内直传，角色建档要填人设/衣着/特色，
// 那是一张完整表单，这里只给「去人物库新建」的入口，不在弹窗里塞半套建档流程。
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, message } from 'ant-design-vue'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { charactersApi, MAX_CHARACTERS_PER_PROJECT, type Character } from '@/api'
import { errorText } from '@/utils/errors'

const props = withDefaults(
  defineProps<{ open: boolean; selected: string[]; max?: number }>(),
  { max: MAX_CHARACTERS_PER_PROJECT },
)
const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [ids: string[]]
}>()

const router = useRouter()
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
    void load()
  },
)

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

function goLibrary() {
  visible.value = false
  router.push('/office/characters')
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
</style>
