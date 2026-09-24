<template>
  <div class="desktop-notice">
    <a-result :title="title" :sub-title="subTitle">
      <template #icon><DesktopOutlined /></template>
      <template v-if="$slots.default" #extra>
        <slot />
      </template>
    </a-result>
  </div>
</template>

<script setup lang="ts">
import { DesktopOutlined } from '@ant-design/icons-vue'

/** 「请用桌面端访问」整页提示：运维重交互页（数据库工作台、服务器终端）
 *  在手机上用 v-if 把主体换成它。深色页（终端页）自行用 :deep 覆盖配色。 */
withDefaults(
  defineProps<{
    title?: string
    subTitle?: string
  }>(),
  {
    title: '请使用桌面端访问',
    subTitle: '这个页面包含桌面端专用的复杂交互，手机屏幕难以操作。',
  },
)
</script>

<style scoped>
/* 撑满父级并居中：父级需是 flex column 且自身有高度（两个接入页都满足）。 */
.desktop-notice {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
}
</style>
