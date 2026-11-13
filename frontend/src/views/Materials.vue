<template>
  <div class="page-card">
    <div class="page-title">素材库</div>

    <a-alert
      type="info"
      show-icon
      class="intro-alert"
      message="素材按账号存，不属于任何一条片子"
      description="短视频工作台里每一镜最多挑 4 条素材。人物形象、片头 logo 这类反复要用的东西放这儿，换一条选题不用重传；删一条短视频不会带走这里的素材，反过来删一条素材会把引用它的镜头一起去掉。"
    />

    <div class="toolbar toolbar--between">
      <a-space :size="8" wrap>
        <a-radio-group v-model:value="kind" button-style="solid" size="small" @change="reload">
          <a-radio-button value="">全部</a-radio-button>
          <a-radio-button value="image">图片</a-radio-button>
          <a-radio-button value="video">视频</a-radio-button>
        </a-radio-group>
        <a-input-search
          v-model:value="keyword"
          placeholder="按名称搜"
          allow-clear
          size="small"
          style="width: 220px"
          @search="reload"
        />
      </a-space>
      <a-space :size="10" wrap>
        <span class="muted">
          共 {{ total }} 条 · 占用 {{ fmtSize(bytesTotal) }} · 一个账号最多
          {{ maxPerOwner }} 条
        </span>
        <a-upload
          :before-upload="onUpload"
          :show-upload-list="false"
          :accept="ACCEPT"
          multiple
        >
          <a-button type="primary" :loading="uploading">
            <UploadOutlined /> 上传素材
          </a-button>
        </a-upload>
      </a-space>
    </div>

    <a-spin :spinning="loading">
      <ul v-if="items.length" class="material-grid">
        <li v-for="item in items" :key="item.id" class="material-cell">
          <div class="material-thumb">
            <img v-if="item.kind === 'image'" :src="item.url" :alt="item.name" loading="lazy" />
            <video v-else-if="item.url" :src="item.url" preload="metadata" muted playsinline />
            <span v-else class="thumb-missing">文件已丢失</span>
            <a-tag class="kind-tag" :color="item.kind === 'video' ? 'purple' : 'blue'">
              {{ item.kind === 'video' ? '视频' : '图片' }}
            </a-tag>
          </div>
          <div class="material-meta">
            <span class="material-name" :title="item.name">{{ item.name }}</span>
            <span class="hint">{{ fmtSize(item.size) }} · {{ fmtShortDateTime(item.created_at) }}</span>
          </div>
          <a-popconfirm
            title="删除这条素材？引用它的镜头会一起去掉这条。"
            ok-text="删除"
            cancel-text="取消"
            @confirm="onRemove(item)"
          >
            <a-button size="small" type="text" danger class="cell-remove">
              <DeleteOutlined />
            </a-button>
          </a-popconfirm>
        </li>
      </ul>
      <a-empty v-else :image="simpleEmpty" :description="emptyText" />
    </a-spin>

    <div v-if="total > pageSize" class="pager">
      <a-pagination
        v-model:current="page"
        :page-size="pageSize"
        :total="total"
        size="small"
        :show-size-changer="false"
        @change="load"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
// 素材库：账号级的图片/视频素材，跨项目复用。
// 这一期只有图片类素材真的进模型（首帧渲染时内联成 base64）；视频素材存下来、
// 也能被镜头引用，但那条链路要等参考视频有公网可达地址才通，所以这里的文案
// 只说「记录」，不说「用上」。
import { computed, onMounted, ref } from 'vue'
import { DeleteOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import { materialsApi, type MaterialAsset, type MaterialKind } from '@/api'
import { errorText } from '@/utils/errors'
import { fmtShortDateTime, fmtSize } from '@/utils/format'

// 与后端 services/media/store.py 的魔数白名单一致：不在这里的后缀会被拒，
// 而拒的原因（「只支持 …」）比浏览器解不开一个改名的文件好懂得多。
const ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/bmp,video/mp4,video/webm'
const PAGE_SIZE = 24

const items = ref<MaterialAsset[]>([])
const total = ref(0)
const bytesTotal = ref(0)
const maxPerOwner = ref(0)
const loading = ref(false)
const uploading = ref(false)
const kind = ref<'' | MaterialKind>('')
const keyword = ref('')
const page = ref(1)
const pageSize = PAGE_SIZE

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

// 一次多选会有几个回调还在飞：只有最后一个负责刷新列表。
let pendingUploads = 0

const emptyText = computed(() => {
  if (kind.value || keyword.value.trim()) return '没有符合筛选的素材'
  return '还没有素材，先传一张人物形象图或一段片头'
})

async function load() {
  loading.value = true
  try {
    const offset = (page.value - 1) * pageSize
    const data = await materialsApi.list({
      kind: kind.value || undefined,
      q: keyword.value.trim() || undefined,
      limit: pageSize,
      offset,
    })
    // 翻页翻空了（删掉最后一页的几条就会这样）：退回上一页再取一次，而不是留一屏空白。
    if (!data.items.length && offset > 0 && page.value > 1) {
      page.value -= 1
      await load()
      return
    }
    items.value = data.items
    total.value = data.total
    bytesTotal.value = data.bytes_total
    maxPerOwner.value = data.max_per_owner
  } finally {
    loading.value = false
  }
}

/** 筛选条件变了就回第一页：停在第 5 页看一个只剩 3 条的筛选结果是空白屏。 */
function reload() {
  page.value = 1
  void load()
}

async function onUpload(file: File) {
  uploading.value = true
  pendingUploads += 1
  try {
    const asset = await materialsApi.upload(file)
    message.success(`已存入素材库：${asset.name}`)
  } catch (err) {
    message.error(errorText(err, `上传 ${file.name} 失败`))
  } finally {
    pendingUploads -= 1
    // 多选时 antd 会连着回调几次：等最后一条落库再刷新，不然一屏会在几秒里重取好几遍。
    if (!pendingUploads) {
      uploading.value = false
      await load()
    }
  }
  // 返回 false 拦住 antd 的自动上传：文件由上面这次请求发出去了。
  return false
}

async function onRemove(item: MaterialAsset) {
  try {
    await materialsApi.remove(item.id)
    message.success('已删除')
    await load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  }
}

onMounted(() => void load())
</script>

<style scoped>
.intro-alert {
  margin-bottom: 12px;
}

.material-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
}

.material-cell {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--surface);
}

.material-thumb {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 150px;
  overflow: hidden;
  border-radius: 8px;
  background: var(--surface-2);
}

.material-thumb img,
.material-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.thumb-missing {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.kind-tag {
  position: absolute;
  top: 6px;
  left: 6px;
  margin: 0;
}

.material-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.material-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-weight: 600;
}

.cell-remove {
  position: absolute;
  top: 6px;
  right: 6px;
}

.pager {
  display: flex;
  justify-content: center;
  margin-top: 14px;
}
</style>
