<template>
  <div class="page-card">
    <div class="page-title">面试</div>
    <!-- 场次列表本体：与简历页解耦后独占一页；问题明细仍走 bare 整页详情。 -->
    <InterviewRecords />
  </div>
</template>

<script setup lang="ts">
import InterviewRecords from '@/components/InterviewRecords.vue'
</script>
