<template>
  <div class="page-card">
    <div class="page-title">智能体</div>
    <div class="subtitle">
      智能体决定客服回答问题的方式。内置智能体不可直接修改，可「复制」后调整为自己的版本。
    </div>

    <div class="toolbar">
      <a-button type="primary" @click="openCreate">
        <PlusOutlined /> 新建智能体
      </a-button>
    </div>

    <!-- 首屏用骨架屏占位，避免先闪「还没有智能体」再填充；已有内容时刷新直接留旧卡片。 -->
    <div v-if="loading && !agents.length" class="agent-grid">
      <a-card v-for="i in 3" :key="i" size="small" class="agent-card">
        <a-skeleton active :title="{ width: '40%' }" :paragraph="{ rows: 3 }" />
      </a-card>
    </div>
    <template v-else>
      <div class="agent-grid">
        <AgentCard
          v-for="agent in chatAgents"
          :key="agent.id"
          :agent="agent"
          @view="openView"
          @duplicate="onDuplicate"
          @edit="openEdit"
          @delete="(a) => onDelete(a.id)"
        />
      </div>

      <a-empty v-if="!agents.length" description="还没有智能体" />

      <!-- 功能内置人设：简历、运维等功能后台调用的人格，不进对话选择器。
           默认折叠，避免淹没用户真正要维护的对话智能体。 -->
      <template v-if="backgroundAgents.length">
        <div class="section-toggle" @click="showBackground = !showBackground">
          <DownOutlined class="section-toggle-icon" :rotate="showBackground ? 180 : 0" />
          功能内置人设（{{ backgroundAgents.length }}）
          <span class="muted">— 供简历、运维等功能后台调用，不出现在对话里</span>
        </div>
        <div v-show="showBackground" class="agent-grid">
          <AgentCard
            v-for="agent in backgroundAgents"
            :key="agent.id"
            :agent="agent"
            @view="openView"
            @duplicate="onDuplicate"
            @edit="openEdit"
            @delete="(a) => onDelete(a.id)"
          />
        </div>
      </template>
    </template>

    <a-modal
      v-model:open="modal.open"
      :title="modalTitle"
      :confirm-loading="modal.loading"
      :footer="modal.readonly ? null : undefined"
      ok-text="保存"
      cancel-text="取消"
      width="640px"
      @ok="submit"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="modal.form.name" :disabled="modal.readonly" />
        </a-form-item>

        <a-form-item v-if="!modal.editing && !modal.readonly" label="标识" required>
          <a-input v-model:value="modal.form.slug" placeholder="小写字母、数字与短横线，例如 sales-helper" />
          <div class="hint">创建后不可修改，用于在接口中稳定引用该智能体。</div>
        </a-form-item>

        <a-form-item label="描述">
          <a-input v-model:value="modal.form.description" :disabled="modal.readonly" />
        </a-form-item>

        <a-form-item label="系统提示词" required>
          <a-textarea
            v-model:value="modal.form.system_prompt"
            :rows="8"
            :disabled="modal.readonly"
          />
          <div class="hint">
            决定智能体的语气与回答方式。引用格式与「资料不足时不要编造」的约束由系统统一追加，此处无需重复。
          </div>
        </a-form-item>

        <a-form-item label="检索知识库">
          <a-switch v-model:checked="modal.form.use_knowledge" :disabled="modal.readonly" />
          <div class="hint">
            关闭后该智能体不会检索知识库，适合写作、头脑风暴等通用场景。
          </div>
        </a-form-item>

        <a-form-item label="运维只读工具">
          <a-switch v-model:checked="modal.form.use_ops" :disabled="modal.readonly" />
          <div class="hint">
            开启后，该智能体在主对话中可以只读排查你登记的服务器与数据库；
            写操作仍需到「运维」页面确认执行。
          </div>
        </a-form-item>

        <a-form-item label="长期记忆">
          <a-switch v-model:checked="modal.form.use_memory" :disabled="modal.readonly" />
          <div class="hint">
            开启后，该智能体回答时会带上对你的长期记忆（跨会话生效），
            对话中值得记住的事实也会自动沉淀。记忆内容可在「设置 → 长期记忆」查看与删除。
          </div>
        </a-form-item>

        <a-form-item label="在对话中可选">
          <a-switch v-model:checked="modal.form.chat_visible" :disabled="modal.readonly" />
          <div class="hint">
            关闭后该智能体不出现在主对话的选择器里，适合只被功能模块在后台调用的人设。
          </div>
        </a-form-item>

        <a-form-item :label="`发散度：${modal.form.temperature}`">
          <a-slider
            v-model:value="modal.form.temperature"
            :min="0"
            :max="100"
            :disabled="modal.readonly"
          />
          <div class="hint">数值越低回答越稳定保守；客服与技术支持建议保持在 30 以下。</div>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { DownOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { agentsApi, type Agent } from '@/api'
import AgentCard from '@/components/AgentCard.vue'

const agents = ref<Agent[]>([])
const loading = ref(false)

// 对话智能体（用户日常维护的）与功能内置人设（后台调用的）分开展示。
const chatAgents = computed(() => agents.value.filter((agent) => agent.chat_visible))
const backgroundAgents = computed(() => agents.value.filter((agent) => !agent.chat_visible))
const showBackground = ref(false)

const emptyForm = () => ({
  id: 0,
  slug: '',
  name: '',
  description: '',
  system_prompt: '',
  use_knowledge: true,
  use_ops: false,
  use_memory: false,
  chat_visible: true,
  temperature: 30,
})

const modal = reactive({
  open: false,
  editing: false,
  readonly: false,
  loading: false,
  form: emptyForm(),
})

const modalTitle = computed(() => {
  if (modal.readonly) return '查看智能体'
  return modal.editing ? '编辑智能体' : '新建智能体'
})

async function load() {
  loading.value = true
  try {
    const data = await agentsApi.list()
    agents.value = data.items
  } finally {
    loading.value = false
  }
}

function fillForm(agent: Agent) {
  modal.form = {
    id: agent.id,
    slug: agent.slug,
    name: agent.name,
    description: agent.description || '',
    system_prompt: agent.system_prompt,
    use_knowledge: agent.use_knowledge,
    use_ops: agent.use_ops,
    use_memory: agent.use_memory,
    chat_visible: agent.chat_visible,
    temperature: agent.temperature,
  }
}

function openCreate() {
  modal.editing = false
  modal.readonly = false
  modal.form = emptyForm()
  modal.open = true
}

function openEdit(agent: Agent) {
  modal.editing = true
  modal.readonly = false
  fillForm(agent)
  modal.open = true
}

function openView(agent: Agent) {
  modal.editing = false
  modal.readonly = true
  fillForm(agent)
  modal.open = true
}

async function submit() {
  if (modal.readonly) {
    modal.open = false
    return
  }
  if (!modal.form.name.trim() || !modal.form.system_prompt.trim()) {
    message.warning('请填写名称与系统提示词')
    return
  }
  if (!modal.editing && !/^[a-z0-9][a-z0-9-]*$/.test(modal.form.slug)) {
    message.warning('标识只能包含小写字母、数字与短横线')
    return
  }

  modal.loading = true
  try {
    const payload = {
      name: modal.form.name.trim(),
      description: modal.form.description.trim() || null,
      system_prompt: modal.form.system_prompt.trim(),
      use_knowledge: modal.form.use_knowledge,
      use_ops: modal.form.use_ops,
      use_memory: modal.form.use_memory,
      chat_visible: modal.form.chat_visible,
      temperature: modal.form.temperature,
    }
    if (modal.editing) {
      await agentsApi.update(modal.form.id, payload)
      message.success('已更新')
    } else {
      await agentsApi.create({ ...payload, slug: modal.form.slug.trim() })
      message.success('已创建')
    }
    modal.open = false
    await load()
  } finally {
    modal.loading = false
  }
}

async function onDuplicate(agent: Agent) {
  try {
    const copy = await agentsApi.duplicate(agent.id)
    message.success(`已复制为「${copy.name}」`)
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

async function onDelete(id: number) {
  try {
    await agentsApi.remove(id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

onMounted(load)
</script>

<style scoped>
.subtitle {
  color: var(--text-3);
  font-size: 13px;
  margin-bottom: 16px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.toolbar {
  margin-bottom: 16px;
  justify-content: flex-end;
}
.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}
.section-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 20px 0 12px;
  font-weight: 600;
  cursor: pointer;
  user-select: none;
}
.section-toggle .muted {
  font-weight: 400;
}
.section-toggle-icon {
  font-size: 12px;
  color: var(--text-3);
  transition: transform 0.2s;
}
/* 颜色/字号/行高用全局 .hint（style.css），这里只留本页差异。 */
.hint {
  margin-top: 4px;
}
</style>
