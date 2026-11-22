<template>
  <a-modal
    :open="open"
    title="整篇起草"
    :confirm-loading="submitting"
    :ok-button-props="{ disabled: modelOptions.length === 0 }"
    ok-text="开始生成"
    cancel-text="取消"
    width="560px"
    @ok="onSubmit"
    @cancel="emit('update:open', false)"
  >
    <a-alert
      v-if="!loadingModels && modelOptions.length === 0"
      type="warning"
      show-icon
      class="gate-alert"
      message="还没有测通的对话模型"
      description="整篇起草依赖一个用途为「对话」且通过连通性测试的模型，请先到系统设置里配置。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-alert
      type="info"
      show-icon
      class="gate-alert"
      message="模型会按大纲一次写出全文"
      description="成稿按二级标题（##）切回各个段落；若模型给出的段落数与大纲对不上，整篇会落进第一个段落并给出提示，由你手工拆分。"
    />

    <a-alert
      v-if="hasContent"
      type="warning"
      show-icon
      class="gate-alert"
      message="将覆盖已有正文"
      description="当前段落里已经有正文，整篇起草会逐段替换全部现有内容，且不会保留历史版本。需要保留请先导出。"
    />

    <a-form layout="vertical" class="generate-form">
      <a-form-item label="生成模型">
        <a-select
          v-model:value="modelConfigId"
          :options="modelOptions"
          :loading="loadingModels"
          allow-clear
          show-search
          option-filter-prop="label"
          placeholder="默认对话模型"
        />
        <div class="field-hint">不选就用系统默认对话模型；只列出已测通的本人配置。</div>
      </a-form-item>
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { settingsApi, type WritingDraftGeneratePayload } from '@/api'

const props = defineProps<{
  open: boolean
  submitting: boolean
  /** 任一段落已有正文：弹窗显式警示整篇起草会覆盖现有内容。 */
  hasContent: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  submit: [payload: WritingDraftGeneratePayload]
}>()

const router = useRouter()

const modelConfigId = ref<number | undefined>(undefined)
const modelOptions = ref<{ value: number; label: string }[]>([])
const loadingModels = ref(false)

// 每次打开都重置并重拉：用户可能刚从「去配置」跳设置配好模型，缓存会让新配置不出现。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    modelConfigId.value = undefined
    void loadModels()
  },
)

async function loadModels() {
  loadingModels.value = true
  try {
    const data = await settingsApi.listModels('chat')
    // 后端门槛只认测通的对话模型，下拉里就别给注定 4004 的选项。
    modelOptions.value = data.items
      .filter((item) => item.last_test_ok)
      .map((item) => ({
        value: item.id,
        label: item.is_default ? `${item.title}（默认）` : item.title,
      }))
  } catch {
    // 信封/网络错误已由 http 拦截器统一弹提示；下拉留空，弹窗的告警条会引导去配置。
  } finally {
    loadingModels.value = false
  }
}

function onSubmit() {
  emit('submit', { model_config_id: modelConfigId.value ?? null })
}

function goToSettings() {
  emit('update:open', false)
  void router.push('/system/settings')
}
</script>

<style scoped>
.gate-alert {
  margin-bottom: 16px;
}

.field-hint {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 12px;
}
</style>
