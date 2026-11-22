<template>
  <a-modal
    :open="open"
    :title="kind === 'novel' ? 'AI 写章节' : 'AI 写段落'"
    :confirm-loading="submitting"
    :ok-button-props="{ disabled: okDisabled }"
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
      description="正文生成依赖一个用途为「对话」且通过连通性测试的模型，请先到系统设置里配置。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-form layout="vertical" class="generate-form">
      <a-form-item label="生成动作">
        <a-select v-model:value="form.action" :options="actionOptions" :disabled="actionOptions.length === 0" />
        <div v-if="allActionsDisabled" class="field-hint">
          空段落没有可做的编辑动作，请先手工写作或使用「整篇起草」。
        </div>
      </a-form-item>
      <a-form-item v-if="form.action === 'retone'" label="目标语气" required>
        <a-input
          v-model:value="form.tone"
          :maxlength="64"
          placeholder="如：轻松幽默 / 犀利克制"
          @press-enter="onSubmit"
        />
      </a-form-item>
      <a-form-item label="额外要求（可选）">
        <a-textarea
          v-model:value="form.instruction"
          :rows="3"
          :maxlength="512"
          show-count
          placeholder="如：节奏快一点、少用长句；留空则只按动作本身处理"
        />
      </a-form-item>
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
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  settingsApi,
  type WritingKind,
  type WritingPartGeneratePayload,
} from '@/api'
import { defaultAction, partActionsFor } from '@/utils/writingActions'

const props = defineProps<{
  open: boolean
  kind: WritingKind
  /** 当前块是否已有正文：决定五个编辑动作是否置灰。 */
  hasContent: boolean
  submitting: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  submit: [payload: WritingPartGeneratePayload]
}>()

const router = useRouter()

interface PartForm {
  action: WritingPartGeneratePayload['action']
  tone: string
  instruction: string
  model_config_id: number | undefined
}

const form = reactive<PartForm>({
  action: defaultAction(props.kind),
  tone: '',
  instruction: '',
  model_config_id: undefined,
})
const modelOptions = ref<{ value: number; label: string }[]>([])
const loadingModels = ref(false)

const actionOptions = computed(() =>
  partActionsFor(props.kind, props.hasContent).map((choice) => ({
    value: choice.action,
    label: choice.reason ? `${choice.label}（${choice.reason}）` : choice.label,
    disabled: choice.disabled,
  })),
)

const allActionsDisabled = computed(() => actionOptions.value.every((option) => option.disabled))
const okDisabled = computed(
  () => modelOptions.value.length === 0 || allActionsDisabled.value || actionDisabledNow.value,
)
const actionDisabledNow = computed(
  () => actionOptions.value.find((option) => option.value === form.action)?.disabled ?? false,
)

// 每次打开都重置并重拉：动作按作品 kind 给默认值，模型/语气/要求不跨块记忆，
// 模型列表不缓存——用户可能刚从「去配置」跳设置配好模型。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    form.action = defaultAction(props.kind)
    form.tone = ''
    form.instruction = ''
    form.model_config_id = undefined
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
  if (actionDisabledNow.value) {
    message.warning('这个动作需要块里先有正文，请先起草或手工写作')
    return
  }
  if (form.action === 'retone' && !form.tone.trim()) {
    message.warning('换语气需要填写目标语气')
    return
  }
  emit('submit', {
    action: form.action,
    tone: form.action === 'retone' ? form.tone.trim() : null,
    instruction: form.instruction.trim() || null,
    model_config_id: form.model_config_id ?? null,
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
