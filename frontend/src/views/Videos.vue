<template>
  <div class="page-card">
    <div class="page-title">短视频</div>

    <!-- 三个历史菜单叶子（短视频 / 人物库 / 素材库）合并成一个叶子，页内三页签。
         a-tab-pane 默认懒挂载：页签第一次打开才发请求，挂上后保留实例，切换不丢筛选与滚动。 -->
    <a-tabs v-model:activeKey="activeTab">
      <a-tab-pane key="projects" tab="片子">
        <VideoProjectsPanel />
      </a-tab-pane>
      <a-tab-pane key="characters" tab="人物库">
        <CharactersPanel />
      </a-tab-pane>
      <a-tab-pane key="materials" tab="素材库">
        <MaterialsPanel />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import VideoProjectsPanel from '@/components/videos/VideoProjectsPanel.vue'
import CharactersPanel from '@/components/videos/CharactersPanel.vue'
import MaterialsPanel from '@/components/videos/MaterialsPanel.vue'

// 页签态同步 query（?tab=characters|materials）：刷新、分享链接与浏览器前进后退都不丢，
// 与创作页（Writing.vue）的 tab query 同范式。旧路径 /office/characters、/office/materials
// 在路由表里重定向到这里，书签和外发链接不会 404。
const VIDEO_TABS = ['projects', 'characters', 'materials'] as const
type VideoTab = (typeof VIDEO_TABS)[number]

const route = useRoute()
const router = useRouter()

const activeTab = computed<VideoTab>({
  get() {
    const tab = route.query.tab
    // 不认的 query 值落回默认页签，不把空白页签暴露给用户。
    return VIDEO_TABS.includes(tab as VideoTab) ? (tab as VideoTab) : 'projects'
  },
  set(value) {
    // 默认页签不挂 query，保持 /office/videos 这个干净 URL。
    void router.replace({
      query: { ...route.query, tab: value === 'projects' ? undefined : value },
    })
  },
})
</script>
