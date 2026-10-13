<!--
  运行抽屉：业务页（简历/幻灯片/面试详情）回看「某个对象跑过的哪几次」的上下文
  弹层。内容全在 WorkflowRunsPanel，这里只套 a-drawer 外壳；工作流页的「运行」
  不走抽屉，走应用内标签整页（WorkflowRunsPage）。
-->
<template>
  <a-drawer
    :open="open"
    :title="drawerTitle"
    placement="right"
    width="min(1240px, 94vw)"
    :mask-closable="false"
    @update:open="(value: boolean) => emit('update:open', value)"
  >
    <WorkflowRunsPanel :workflow="workflow" :subject="subject" :active="open" @changed="emit('changed')" />
  </a-drawer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import WorkflowRunsPanel from '@/components/workflow/WorkflowRunsPanel.vue'
import type { Workflow, WorkflowRunSubject } from '@/api'

const props = defineProps<{
  open: boolean
  workflow: Workflow | null
  /** 业务对象模式：列「这个对象跑过的哪几次」，不提供发起入口。 */
  subject?: WorkflowRunSubject | null
}>()

const emit = defineEmits<{ 'update:open': [value: boolean]; changed: [] }>()

const drawerTitle = computed(() =>
  props.subject
    ? `运行记录 · ${props.subject.label ?? props.subject.ref}`
    : `运行 · ${props.workflow?.name ?? ''}`,
)
</script>
