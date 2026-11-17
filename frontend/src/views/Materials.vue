<template>
  <div class="page-card">
    <div class="page-title">素材库</div>

    <div class="toolbar toolbar--between">
      <a-space :size="8" wrap>
        <a-radio-group v-model:value="kind" button-style="solid" size="small">
          <a-radio-button value="">全部</a-radio-button>
          <a-radio-button value="image">图片</a-radio-button>
          <a-radio-button value="video">视频</a-radio-button>
        </a-radio-group>
        <a-input-search
          v-model:value="keyword"
          placeholder="按名称或分组标签搜"
          allow-clear
          size="small"
          style="width: 240px"
        />
      </a-space>
      <a-space :size="10" wrap>
        <span class="muted">
          {{ filtered.length === items.length ? `共 ${total} 条` : `筛出 ${filtered.length} / ${total} 条` }}
          · 占用 {{ fmtSize(bytesTotal) }} · 一个账号最多 {{ maxPerOwner }} 条
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
      <section v-for="group in groups" :key="group.key" class="material-group">
        <div class="group-head">
          <a-tag
            v-if="group.tag"
            class="group-tag"
            title="点击改组名"
            @click="onRenameGroup(group.tag!)"
          >
            {{ group.title }} <EditOutlined />
          </a-tag>
          <span v-else class="group-title">{{ group.title }}</span>
          <span class="group-count">{{ group.items.length }} 条</span>
        </div>
        <ul class="material-grid">
          <li v-for="item in group.items" :key="item.id" class="material-cell">
            <div class="material-thumb">
              <!-- 缩略图没有时回退原图：缩略图是提速件，缺了不该让这格空白。 -->
              <img
                v-if="item.kind === 'image'"
                :src="item.thumb_url || item.url"
                :alt="item.name"
                loading="lazy"
              />
              <video v-else-if="item.url" :src="item.url" preload="metadata" muted playsinline />
              <span v-else class="thumb-missing">文件已丢失</span>
              <a-tag class="kind-tag" :color="item.kind === 'video' ? 'purple' : 'blue'">
                {{ item.kind === 'video' ? '视频' : '图片' }}
              </a-tag>
            </div>
            <div class="material-meta">
              <span class="material-name" :title="item.name">{{ item.name }}</span>
              <span class="hint">
                <a class="tag-chip" :class="{ 'tag-chip--empty': !item.tag }" @click="openTagEditor(item)">
                  <TagsOutlined v-if="!item.tag" /> {{ item.tag || '分组' }}
                </a>
                · {{ fmtSize(item.size) }} · {{ fmtShortDateTime(item.created_at) }}
              </span>
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
      </section>
      <a-empty v-if="!groups.length" :image="simpleEmpty" :description="emptyText" />
    </a-spin>

    <!-- 标签分组编辑：点卡片上的小标签进来改单条；点组标题的标签进来改整组。 -->
    <a-modal
      v-model:open="tagEditorOpen"
      :title="tagEditorTitle"
      ok-text="保存"
      cancel-text="取消"
      :confirm-loading="savingTag"
      @ok="saveTag"
    >
      <a-input
        v-model:value="tagDraft"
        :maxlength="128"
        placeholder="分组标签，如项目名；留空则按时间分组"
        @press-enter="saveTag"
      />
      <div v-if="tagChoices.length" class="hint tag-editor-hint">
        已有分组，点一个直接用：
        <a
          v-for="choice in tagChoices"
          :key="choice"
          class="tag-chip tag-chip--choice"
          @click="tagDraft = choice"
        >
          {{ choice }}
        </a>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 素材库：账号级的图片/视频素材，跨项目复用。
//
// 这一页只做三件事：拉全量（账号上限 200 条，一次拉满，筛选与分组全在本地做，
// 输入即生效）、按「打标分组 + 时间」分组展示（从短视频存进来的首帧自动打上
// 项目名，同一批聚成一组；没打标的按天归组）、列表图一律走后端生成的 320px
// WebP 缩略图（首帧是 2K PNG，一张好几 MB，几十张原图同时进网络就是卡的根因）。
//
// 视频类素材只存下来、也能被镜头引用，但那条链路要等参考视频有公网可达地址才通，
// 所以这里的文案只说「记录」，不说「用上」。
import { computed, onMounted, ref } from 'vue'
import { DeleteOutlined, EditOutlined, TagsOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import { materialsApi, type MaterialAsset, type MaterialKind } from '@/api'
import { errorText } from '@/utils/errors'
import { fmtShortDateTime, fmtSize } from '@/utils/format'

// 与后端 services/media/store.py 的魔数白名单一致：不在这里的后缀会被拒，
// 而拒的原因（「只支持 …」）比浏览器解不开一个改名的文件好懂得多。
const ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/bmp,video/mp4,video/webm'
// 全库一次拉满：账号上限就是 200 条（后端 MATERIAL_MAX_PER_OWNER），分组展示要
// 看到全量才能把同一标签聚齐；2xx 行的 JSON 比几 MB 的原图轻得多。
const FETCH_LIMIT = 200

const items = ref<MaterialAsset[]>([])
const total = ref(0)
const bytesTotal = ref(0)
const maxPerOwner = ref(0)
const loading = ref(false)
const uploading = ref(false)
const kind = ref<'' | MaterialKind>('')
const keyword = ref('')

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

// 一次多选会有几个回调还在飞：只有最后一个负责刷新列表。
let pendingUploads = 0

// 本地筛选：全量已在手上，类别与关键词（名字 + 分组标签）输入即生效，不发请求。
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter(
    (item) =>
      (!kind.value || item.kind === kind.value) &&
      (!kw ||
        item.name.toLowerCase().includes(kw) ||
        (item.tag || '').toLowerCase().includes(kw)),
  )
})

interface MaterialGroup {
  key: string
  title: string
  /** 有标签的组才有：标题就是标签本身，改组名要按它找回去。 */
  tag?: string
  items: MaterialAsset[]
}

// 分组展示：打了标的按标签聚一组（组间按各自最新一条的时间倒序，因为全量本身
// 就是 created_at 倒序，插入序天然满足）；没打标的按天归组（今天 / 昨天 / 具体日期）。
const groups = computed<MaterialGroup[]>(() => {
  const byTag = new Map<string, MaterialAsset[]>()
  const byDay = new Map<string, MaterialAsset[]>()
  for (const item of filtered.value) {
    if (item.tag) {
      const list = byTag.get(item.tag) ?? []
      list.push(item)
      byTag.set(item.tag, list)
    } else {
      const key = dayKey(item.created_at)
      const list = byDay.get(key) ?? []
      list.push(item)
      byDay.set(key, list)
    }
  }
  const tagged = [...byTag.entries()].map(([tag, list]) => ({
    key: `tag:${tag}`,
    title: tag,
    tag,
    items: list,
  }))
  const daily = [...byDay.entries()].map(([key, list]) => ({
    key: `day:${key}`,
    title: dayTitle(key),
    items: list,
  }))
  // 标签组在前：打标是用户（或存帧那一侧）明确的意志，比「恰好同一天传的」优先。
  return [...tagged, ...daily]
})

// 已有分组标签：给编辑框当快捷选项，也是改组名的候选。
const tagChoices = computed(() => [...new Set(items.value.map((item) => item.tag).filter(Boolean))] as string[])

function dayKey(value: string | null): string {
  if (!value) return '未知时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知时间'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function dayTitle(key: string): string {
  const now = new Date()
  const todayKey = dayKey(now.toString())
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (key === todayKey) return '今天'
  if (key === dayKey(yesterday.toString())) return '昨天'
  const [y, m, d] = key.split('-').map(Number)
  if (!y || !m || !d) return key
  return y === now.getFullYear() ? `${m}月${d}日` : `${y}年${m}月${d}日`
}

const emptyText = computed(() => {
  if (kind.value || keyword.value.trim()) return '没有符合筛选的素材'
  return '还没有素材，先传一张人物形象图或一段片头'
})

async function load() {
  loading.value = true
  try {
    const data = await materialsApi.list({ limit: FETCH_LIMIT })
    items.value = data.items
    total.value = data.total
    bytesTotal.value = data.bytes_total
    maxPerOwner.value = data.max_per_owner
  } finally {
    loading.value = false
  }
}

// -- 打标分组 -------------------------------------------------------------

const tagEditorOpen = ref(false)
// 两种编辑走同一个框：改单条时指着那条素材；改组名时 null、renameTarget 有值。
const tagEditing = ref<MaterialAsset | null>(null)
const renameTarget = ref<string | null>(null)
const tagDraft = ref('')
const savingTag = ref(false)

const tagEditorTitle = computed(() => {
  if (renameTarget.value !== null) return `改「${renameTarget.value}」这一组的名字`
  return tagEditing.value ? `给「${tagEditing.value.name}」选分组` : '选分组'
})

function openTagEditor(item: MaterialAsset) {
  tagEditing.value = item
  renameTarget.value = null
  tagDraft.value = item.tag || ''
  tagEditorOpen.value = true
}

/** 点组标题的标签进来：保存时把这一组的每一条都改成草稿值。 */
function onRenameGroup(tag: string) {
  tagEditing.value = null
  renameTarget.value = tag
  tagDraft.value = tag
  tagEditorOpen.value = true
}

/** 原位替换一条素材（后端回来的就是整行新值，含缩略图 URL）。 */
function replaceItem(updated: MaterialAsset) {
  const index = items.value.findIndex((row) => row.id === updated.id)
  if (index >= 0) items.value[index] = updated
}

async function saveTag() {
  if (!tagEditorOpen.value) return
  const next = tagDraft.value.trim()
  savingTag.value = true
  try {
    if (renameTarget.value !== null) {
      const members = items.value.filter((row) => row.tag === renameTarget.value)
      for (const row of members) {
        replaceItem(await materialsApi.setTag(row.id, next))
      }
      message.success(members.length ? `已把 ${members.length} 条改成「${next || '未分组'}」` : '已更新分组')
    } else if (tagEditing.value) {
      replaceItem(await materialsApi.setTag(tagEditing.value.id, next))
      message.success('已更新分组')
    }
    tagEditorOpen.value = false
  } catch (err) {
    message.error(errorText(err, '更新分组失败'))
  } finally {
    savingTag.value = false
  }
}

// -- 上传 / 删除 ----------------------------------------------------------

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
.material-group {
  margin-top: 16px;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.group-title {
  font-weight: 600;
}

.group-tag {
  margin: 0;
  font-weight: 600;
}

.group-count {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.material-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  margin: 0;
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

.tag-chip {
  margin-right: 2px;
  color: var(--primary, #1677ff);
  font-size: var(--fs-xs);
}

.tag-chip--empty {
  color: var(--text-3);
}

.tag-chip--choice {
  margin-right: 8px;
}

.cell-remove {
  position: absolute;
  top: 6px;
  right: 6px;
}

.tag-editor-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 0;
  margin-top: 10px;
}
</style>
