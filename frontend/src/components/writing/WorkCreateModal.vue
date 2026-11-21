<template>
  <a-modal
    :open="open"
    :title="kind === 'novel' ? '新建小说' : '新建文章'"
    :confirm-loading="submitting"
    ok-text="创建"
    cancel-text="取消"
    width="620px"
    @ok="onSubmit"
    @cancel="emit('update:open', false)"
  >
    <a-form ref="formRef" :model="form" :rules="rules" layout="vertical">
      <a-form-item label="标题" name="title">
        <a-input
          v-model:value="form.title"
          :maxlength="128"
          :placeholder="kind === 'novel' ? '如：星河彼岸' : '如：普通人如何做年度复盘'"
        />
      </a-form-item>
      <a-form-item :label="kind === 'novel' ? '灵感 / 梗概' : '主题 / 素材'" name="brief">
        <a-textarea
          v-model:value="form.brief"
          :rows="4"
          :maxlength="20000"
          show-count
          :placeholder="
            kind === 'novel'
              ? '一句话灵感或故事梗概，后续生成大纲的输入快照'
              : '想写的主题、手头的素材与结论，后续生成大纲的输入快照'
          "
        />
      </a-form-item>

      <div class="param-grid">
        <a-form-item
          v-for="field in paramFields"
          :key="field.key"
          :label="field.label"
          class="param-item"
        >
          <a-select
            v-if="field.options"
            v-model:value="form.params[field.key]"
            :options="selectOptions(field)"
            allow-clear
            show-search
            :placeholder="'选择' + field.label"
          />
          <a-input
            v-else
            v-model:value="form.params[field.key]"
            :maxlength="512"
            :placeholder="field.placeholder"
          />
        </a-form-item>
        <a-form-item label="目标字数" class="param-item">
          <a-input-number
            v-model:value="form.params.word_target"
            :min="1"
            :max="6000"
            :precision="0"
            style="width: 100%"
            placeholder="单章 / 单篇目标"
          />
        </a-form-item>
      </div>

      <a-form-item label="补充要求（可选）" name="requirement">
        <a-input
          v-model:value="form.requirement"
          :maxlength="512"
          placeholder="如：节奏快一点、不要感情戏；每次生成都仍会带上它"
        />
      </a-form-item>
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { Rule } from 'ant-design-vue/es/form'
import {
  ARTICLE_PARAM_FIELDS,
  NOVEL_PARAM_FIELDS,
  writingApi,
  type WritingKind,
  type WritingWorkDetail,
} from '@/api'

const props = defineProps<{ open: boolean; kind: WritingKind }>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  created: [work: WritingWorkDetail]
}>()

const formRef = ref()
const submitting = ref(false)

interface CreateForm {
  title: string
  brief: string
  requirement: string
  params: Record<string, string | number>
}

function emptyForm(): CreateForm {
  return { title: '', brief: '', requirement: '', params: {} }
}

const form = reactive<CreateForm>(emptyForm())

const rules: Record<string, Rule[]> = {
  title: [{ required: true, whitespace: true, message: '标题不能为空', trigger: 'blur' }],
}

const paramFields = computed(() =>
  props.kind === 'novel' ? NOVEL_PARAM_FIELDS : ARTICLE_PARAM_FIELDS,
)

// 模板表达式里放箭头函数 + 对象字面量会被 Vue 模板编译器解析失败，选项映射放 script。
function selectOptions(field: (typeof NOVEL_PARAM_FIELDS)[number]) {
  return (field.options ?? []).map((value) => ({ label: value, value }))
}

// 每次打开重置，别把上一本的参数带进来。
watch(
  () => props.open,
  (open) => {
    if (open) Object.assign(form, emptyForm())
  },
)

async function onSubmit() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    // 空串参数不发后端：白名单清洗虽然也会丢，请求体保持干净更好排查。
    const params = Object.fromEntries(
      Object.entries(form.params).filter(([, value]) => value !== '' && value != null),
    )
    const work = await writingApi.create({
      kind: props.kind,
      title: form.title.trim(),
      brief: form.brief.trim() || undefined,
      requirement: form.requirement.trim() || undefined,
      params,
    })
    emit('created', work)
    emit('update:open', false)
  } catch {
    // 信封错误与网络错误都由 http 拦截器统一弹提示，弹窗保持打开让用户改完重试。
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.param-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}

.param-item :deep(.ant-input-number) {
  width: 100%;
}

@media (max-width: 560px) {
  .param-grid {
    grid-template-columns: 1fr;
  }
}
</style>
