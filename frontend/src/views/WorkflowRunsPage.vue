<template>
  <div class="runs-page">
    <header class="page-head">
      <div class="head-main">
        <span class="head-title">运行记录</span>
        <span v-if="workflow" class="head-name">{{ workflow.name }}</span>
        <span v-if="workflow" class="muted">{{ workflow.slug }}</span>
      </div>
      <div class="head-meta">
        <!-- 配置入口：看记录的人多半也想顺手改流程，省一趟回列表。与列表整行
             点击同一套路——把「打开谁」写进 store 再切过去。 -->
        <a-button v-if="workflow" size="small" @click="openEditor">编辑配置</a-button>
        <a-button size="small" @click="router.push({ name: 'Workflows' })">工作流页</a-button>
      </div>
    </header>

    <a-spin :spinning="loading && !workflow" class="page-spin">
      <a-result v-if="errorText" status="warning" :title="errorText">
        <template #extra>
          <a-button size="small" @click="loadWorkflow">重试</a-button>
        </template>
      </a-result>
      <WorkflowRunsPanel v-else-if="workflow" ref="panel" :workflow="workflow" :active="active" />
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { onActivated, onDeactivated, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { workflowsApi, type Workflow } from '@/api'
import { requestEditor } from '@/stores/workflowEditor'
import WorkflowRunsPanel from '@/components/workflow/WorkflowRunsPanel.vue'

const route = useRoute()
const router = useRouter()

// 顶栏多标签下本组件是 keep-alive 复用的单实例：切到另一条流程的标签不会重跑
// setup，流程 id 得跟着路由参数换。
const workflowId = ref(Number(route.params.id))
const workflow = ref<Workflow | null>(null)
const loading = ref(false)
const errorText = ref('')
/**
 * 面板的重置开关：换流程、重试走 false→true 整页重置；同一条流程切走再切回
 * 不碰它，改走 suspend/resume，筛选与展开的现场留着。
 */
const active = ref(false)
const panel = ref<InstanceType<typeof WorkflowRunsPanel> | null>(null)

async function loadWorkflow() {
  // 单实例在标签间复用：在途的响应回来时可能已经切去另一条流程，按发起时的 id 认账。
  const at = workflowId.value
  loading.value = true
  try {
    const data = await workflowsApi.get(at)
    if (workflowId.value !== at) return
    workflow.value = data
    errorText.value = ''
    active.value = true
  } catch (err) {
    if (workflowId.value !== at) return
    workflow.value = null
    errorText.value = (err as { message?: string })?.message || '工作流加载失败'
  } finally {
    if (workflowId.value === at) loading.value = false
  }
}

watch(
  () => route.params.id,
  (id) => {
    if (route.name !== 'WorkflowRunsPage' || id == null || String(id) === String(workflowId.value)) return
    panel.value?.suspend()
    workflowId.value = Number(id)
    workflow.value = null
    errorText.value = ''
    active.value = false
    void loadWorkflow()
  },
)

// 切走断流不占连接；切回来只重取列表——最新那次还在跑面板会自己接上。
onDeactivated(() => panel.value?.suspend())
onActivated(() => {
  if (workflow.value && route.name === 'WorkflowRunsPage') panel.value?.resume()
})

function openEditor() {
  if (!workflow.value) return
  requestEditor(workflow.value.id, workflow.value.is_builtin)
  router.push('/workflows/editor')
}

void loadWorkflow()
</script>

<style scoped>
.runs-page {
  padding: 20px 24px 48px;
}
.page-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--hairline);
  margin-bottom: 12px;
}
.head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.head-title {
  font-weight: 600;
}
.head-name {
  font-weight: 600;
}
.head-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
</style>
