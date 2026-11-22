<template>
  <a-modal
    :open="open"
    :title="kind === 'novel' ? '生成小说大纲' : '生成文章大纲'"
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
      description="大纲生成依赖一个用途为「对话」且通过连通性测试的模型，请先到系统设置里配置。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-form layout="vertical" class="generate-form">
      <a-form-item label="生成模型">
        <a-select
          v-model:value="form.model_config_id"
          :options="modelOptions"
          :loading="loadingModels"
          allow-clear
          show-search
          option-filter-prop="label"
          placeholder="默认对话模型"
        />
        <div class="field-hint">不选就用系统默认对话模型；只列出已测通的本人配置。</div>
      </a-form-item>
      <a-form-item label="本次补充要求（可选）">
        <a-textarea
          v-model:value="form.requirement"
          :rows="4"
          :maxlength="512"
          show-count
          placeholder="如：节奏快一点、不要感情戏；留空则沿用作品信息里的补充要求"
        />
      </a-form-item>
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { settingsApi, type WritingKind, type WritingOutlineGeneratePayload } from '@/api'

const props = defineProps<{
  open: boolean
  kind: WritingKind
  /** 作品当前的补充要求，打开时带进来当默认值。 */
  requirement?: string | null
  submitting: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  submit: [payload: WritingOutlineGeneratePayload]
}>()

const router = useRouter()

interface GenerateForm {
  model_config_id: number | undefined
  requirement: string
}

const form = reactive<GenerateForm>({ model_config_id: undefined, requirement: '' })
const modelOptions = ref<{ value: number; label: string }[]>([])
const loadingModels = ref(false)

// 每次打开都重置并重拉：要求回填作品现值；模型列表不缓存，配置变更即时可见。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    form.model_config_id = undefined
    form.requirement = props.requirement ?? ''
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
  emit('submit', {
    model_config_id: form.model_config_id ?? null,
    requirement: form.requirement.trim() || null,
  })
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
