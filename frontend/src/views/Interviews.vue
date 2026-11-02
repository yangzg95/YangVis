<template>
  <div class="page-card">
    <div class="page-title">面试</div>
    <!-- 求职过程集中在这一页：场次与问题复盘、求职助手的六类一次性生成、以及零散的
         求职笔记。三者同挂求职办公域，笔记与助手原先在「简历」页。 -->
    <a-tabs v-model:activeKey="activeTab" class="interview-tabs">
      <a-tab-pane key="records" tab="面试记录">
        <InterviewRecords />
      </a-tab-pane>
      <a-tab-pane key="toolkit" tab="求职助手">
        <JobToolkitPanel />
      </a-tab-pane>
      <a-tab-pane key="notes" tab="笔记">
        <NotesPanel />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import InterviewRecords from '@/components/InterviewRecords.vue'
import JobToolkitPanel from '@/components/JobToolkitPanel.vue'
import NotesPanel from '@/components/NotesPanel.vue'

const activeTab = ref('records')
</script>

<style scoped>
.interview-tabs {
  margin-top: 4px;
}
</style>
