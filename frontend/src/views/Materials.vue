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

    <div v-if="uploadTasks.length" class="upload-tasks">
      <div class="upload-head">
        <span class="hint">
          上传中 {{ uploadingCount }} 条{{ uploadTasks.length > uploadingCount ? `，共 ${uploadTasks.length} 条` : '' }}
        </span>
      </div>
      <ul class="upload-list">
        <li v-for="task in uploadTasks" :key="task.id" class="upload-item">
          <span class="upload-name" :title="task.error || task.name">{{ task.name }}</span>
          <a-progress
            class="upload-progress"
            size="small"
            :percent="task.percent"
            :status="task.status === 'error' ? 'exception' : task.status === 'done' ? 'success' : 'active'"
          />
          <a
            v-if="task.status === 'error'"
            class="upload-dismiss"
            title="从列表里移除，不影响已上传的其他文件"
            @click="dismissUpload(task.id)"
          >
            移除
          </a>
        </li>
      </ul>
    </div>

    <div v-if="selectedIds.length" class="batch-bar">
      <span class="batch-count">已选 {{ selectedIds.length }} 条素材</span>
      <a-space :size="8">
        <a-button size="small" @click="openBatchTag">批量分组</a-button>
        <a-button size="small" danger @click="openDelete(selectedIds)">批量删除</a-button>
        <a-button size="small" type="text" @click="selectedIds = []">取消选择</a-button>
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
          <li
            v-for="item in group.items"
            :key="item.id"
            class="material-cell"
            :class="{ 'material-cell--picked': selectedIds.includes(item.id) }"
          >
            <div class="material-thumb" @click="openPreview(item)">
              <!-- 缩略图没有时回退原图：缩略图是提速件，缺了不该让这格空白。 -->
              <img
                v-if="item.kind === 'image'"
                :src="item.thumb_url || item.url"
                :alt="item.name"
                loading="lazy"
              />
              <video v-else-if="item.url" :src="item.url" preload="metadata" muted playsinline />
              <span v-else class="thumb-missing">文件已丢失</span>
              <span v-if="item.url" class="thumb-mask"><EyeOutlined /> 预览</span>
              <a-checkbox
                class="cell-check"
                :checked="selectedIds.includes(item.id)"
                @click.stop
                @change="toggleSelect(item)"
              />
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
            <div class="cell-actions">
              <a-tooltip title="重命名">
                <a-button size="small" type="text" @click="openNameEditor(item)">
                  <EditOutlined />
                </a-button>
              </a-tooltip>
              <a-tooltip title="删除">
                <a-button size="small" type="text" danger @click="openDelete([item.id])">
                  <DeleteOutlined />
                </a-button>
              </a-tooltip>
            </div>
          </li>
        </ul>
      </section>
      <a-empty v-if="!groups.length" :image="simpleEmpty" :description="emptyText" />
    </a-spin>

    <!-- 标签分组编辑：点卡片上的小标签改单条、点组标题改整组、批量工具栏改一批。 -->
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

    <a-modal
      v-model:open="previewOpen"
      :title="previewItem?.name"
      width="820px"
      :footer="null"
      destroy-on-close
    >
      <div v-if="previewItem" class="preview-body">
        <img
          v-if="previewItem.kind === 'image'"
          :src="previewItem.url"
          :alt="previewItem.name"
          class="preview-media"
        />
        <video v-else :src="previewItem.url" controls autoplay class="preview-media" />
        <div class="hint preview-meta">
          {{ previewItem.kind === 'video' ? '视频' : '图片' }} ·
          {{ fmtSize(previewItem.size) }} · {{ fmtShortDateTime(previewItem.created_at) }}
          <template v-if="previewItem.tag"> · 分组：{{ previewItem.tag }}</template>
        </div>
      </div>
    </a-modal>

    <a-modal
      v-model:open="nameEditorOpen"
      title="重命名素材"
      ok-text="保存"
      cancel-text="取消"
      :confirm-loading="savingName"
      @ok="saveName"
    >
      <a-input
        v-model:value="nameDraft"
        :maxlength="128"
        placeholder="素材名称"
        @press-enter="saveName"
      />
      <div class="hint name-editor-hint">名字只影响素材库里的显示，不改文件名。</div>
    </a-modal>

    <a-modal
      v-model:open="deleteOpen"
      :title="`删除 ${deleteIds.length} 条素材`"
      :ok-text="`删除 ${deleteIds.length} 条`"
      cancel-text="取消"
      :confirm-loading="deleting"
      :ok-button-props="{ danger: true, disabled: loadingUsage }"
      @ok="confirmDelete"
    >
      <a-alert
        type="warning"
        show-icon
        :message="`文件会先进回收期，${undoWindowLabel}内可撤销。撤销期内镜头引用保持不变；回收期结束后文件才真正删除，引用它的镜头会去掉这条素材。`"
      />
      <a-spin :spinning="loadingUsage">
        <div class="usage-block">
          <template v-if="usageSummary.length">
            <div class="hint">以下 {{ usageShotCount }} 个镜头引用了它，回收期结束后会去掉这条素材：</div>
            <ul class="usage-list">
              <li v-for="line in usageSummary" :key="line.projectId">
                《{{ line.title }}》 第 {{ line.shots.join('、') }} 镜
              </li>
            </ul>
          </template>
          <div v-else-if="usageFailed && !loadingUsage" class="hint">
            引用情况没读出来；如果这几条正被镜头用着，删除后那些镜头会失去引用。
          </div>
          <div v-else-if="!loadingUsage" class="hint">没有被任何镜头引用，可以放心删除。</div>
        </div>
      </a-spin>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 素材库：账号级的图片/视频素材，跨项目复用。
//
// 这一页做五件事：拉全量（账号上限 200 条，一次拉满，筛选与分组全在本地做，
// 输入即生效）、按「打标分组 + 时间」分组展示（从短视频存进来的首帧自动打上
// 项目名，同一批聚成一组；没打标的按天归组）、列表图一律走后端生成的 320px
// WebP 缩略图（首帧是 2K PNG，一张好几 MB，几十张原图同时进网络就是卡的根因）、
// 点缩略图看大图/播放、以及改名/批量分组/批量删除（删前先列出引用它的项目与镜号；
// 删除先进回收期，提示上直接给「撤销」，过期后后端才删文件、解引用）。
//
// 视频类素材只存下来、也能被镜头引用，但那条链路要等参考视频有公网可达地址才通，
// 所以这里的文案只说「记录」，不说「用上」。
import { computed, h, onMounted, ref } from 'vue'
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  TagsOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'
import {
  materialsApi,
  type MaterialAsset,
  type MaterialKind,
  type MaterialUsage,
} from '@/api'
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
// 删除后的撤销窗口：由列表接口带回来，文案与提示时长都照它，不在这里写死 10 分钟。
const trashTtlSeconds = ref(0)
const kind = ref<'' | MaterialKind>('')
const keyword = ref('')
// 勾选跨分组保留：批量分组/删除按 ids 走，不要求先筛到同一组。
const selectedIds = ref<number[]>([])

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

interface UploadTask {
  id: number
  name: string
  percent: number
  status: 'uploading' | 'done' | 'error'
  error?: string
}

// 每个文件一行进度：多选时 antd 会连着回调几次，逐条看得见比一个转圈更可信。
// 成功的两秒后自动消失，失败的留着让人看清原因。
const uploadTasks = ref<UploadTask[]>([])
let uploadSeq = 0

const uploadingCount = computed(
  () => uploadTasks.value.filter((task) => task.status === 'uploading').length,
)
const uploading = computed(() => uploadingCount.value > 0)

const undoWindowLabel = computed(() => {
  const seconds = trashTtlSeconds.value
  if (!seconds) return '短时间内'
  return seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`
})

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
    trashTtlSeconds.value = data.trash_ttl_seconds
  } finally {
    loading.value = false
  }
}

// -- 选择与预览 -----------------------------------------------------------

function toggleSelect(item: MaterialAsset) {
  selectedIds.value = selectedIds.value.includes(item.id)
    ? selectedIds.value.filter((id) => id !== item.id)
    : [...selectedIds.value, item.id]
}

const previewOpen = ref(false)
const previewItem = ref<MaterialAsset | null>(null)

function openPreview(item: MaterialAsset) {
  if (!item.url) {
    message.warning('这条素材的文件已丢失，没有可预览的内容')
    return
  }
  previewItem.value = item
  previewOpen.value = true
}

// -- 改名 -----------------------------------------------------------------

const nameEditorOpen = ref(false)
const nameEditing = ref<MaterialAsset | null>(null)
const nameDraft = ref('')
const savingName = ref(false)

function openNameEditor(item: MaterialAsset) {
  nameEditing.value = item
  nameDraft.value = item.name
  nameEditorOpen.value = true
}

async function saveName() {
  if (!nameEditorOpen.value || !nameEditing.value) return
  const next = nameDraft.value.trim()
  if (!next) {
    message.warning('名字不能为空')
    return
  }
  savingName.value = true
  try {
    replaceItem(await materialsApi.rename(nameEditing.value.id, next))
    nameEditorOpen.value = false
    message.success('已重命名')
  } catch (err) {
    message.error(errorText(err, '重命名失败'))
  } finally {
    savingName.value = false
  }
}

// -- 打标分组 -------------------------------------------------------------

const tagEditorOpen = ref(false)
// 三种编辑走同一个框：改单条时指着那条素材；改组名时 renameTarget 有值；
// 批量分组时 batchTagIds 有值。
const tagEditing = ref<MaterialAsset | null>(null)
const renameTarget = ref<string | null>(null)
const batchTagIds = ref<number[] | null>(null)
const tagDraft = ref('')
const savingTag = ref(false)

const tagEditorTitle = computed(() => {
  if (batchTagIds.value) return `给选中的 ${batchTagIds.value.length} 条素材选分组`
  if (renameTarget.value !== null) return `改「${renameTarget.value}」这一组的名字`
  return tagEditing.value ? `给「${tagEditing.value.name}」选分组` : '选分组'
})

function openTagEditor(item: MaterialAsset) {
  tagEditing.value = item
  renameTarget.value = null
  batchTagIds.value = null
  tagDraft.value = item.tag || ''
  tagEditorOpen.value = true
}

/** 批量工具栏进来：草稿从空开始，也允许直接点已有分组。 */
function openBatchTag() {
  if (!selectedIds.value.length) return
  tagEditing.value = null
  renameTarget.value = null
  batchTagIds.value = [...selectedIds.value]
  tagDraft.value = ''
  tagEditorOpen.value = true
}

/** 点组标题的标签进来：保存时把这一组的每一条都改成草稿值。 */
function onRenameGroup(tag: string) {
  tagEditing.value = null
  renameTarget.value = tag
  batchTagIds.value = null
  tagDraft.value = tag
  tagEditorOpen.value = true
}

/** 原位替换一条素材（后端回来的就是整行新值，含缩略图 URL）。 */
function replaceItem(updated: MaterialAsset) {
  const index = items.value.findIndex((row) => row.id === updated.id)
  if (index >= 0) items.value[index] = updated
}

/** 一批素材改成同一分组：一次请求落库，再按回执原地改本地行。 */
async function assignTag(ids: number[], tag: string) {
  const data = await materialsApi.batchSetTag(ids, tag)
  const tags = new Map(data.items.map((item) => [item.id, item.tag]))
  items.value = items.value.map((row) =>
    tags.has(row.id) ? { ...row, tag: tags.get(row.id) ?? null } : row,
  )
}

async function saveTag() {
  if (!tagEditorOpen.value) return
  const next = tagDraft.value.trim()
  savingTag.value = true
  try {
    if (batchTagIds.value) {
      await assignTag(batchTagIds.value, next)
      message.success(
        `已把 ${batchTagIds.value.length} 条改成「${next || '未分组'}」`,
      )
      selectedIds.value = []
    } else if (renameTarget.value !== null) {
      const members = items.value.filter((row) => row.tag === renameTarget.value)
      await assignTag(
        members.map((row) => row.id),
        next,
      )
      message.success(
        members.length ? `已把 ${members.length} 条改成「${next || '未分组'}」` : '已更新分组',
      )
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

// -- 删除 -----------------------------------------------------------------

interface UsageLine {
  projectId: number
  title: string
  /** 同一项目里会失去引用的镜号，按小到大排。 */
  shots: number[]
}

const deleteOpen = ref(false)
const deleteIds = ref<number[]>([])
const deleteUsages = ref<MaterialUsage[]>([])
const loadingUsage = ref(false)
const usageFailed = ref(false)
const deleting = ref(false)

/** 一批素材的影响面按项目合并：同一镜头引用了两条待删素材也只报一次。 */
const usageSummary = computed<UsageLine[]>(() => {
  const byProject = new Map<number, { title: string; shots: Set<number> }>()
  for (const usage of deleteUsages.value) {
    for (const project of usage.projects) {
      let entry = byProject.get(project.project_id)
      if (!entry) {
        entry = { title: project.title, shots: new Set<number>() }
        byProject.set(project.project_id, entry)
      }
      for (const shot of project.shots) entry.shots.add(shot.sort_order)
    }
  }
  return [...byProject.entries()].map(([projectId, entry]) => ({
    projectId,
    title: entry.title,
    shots: [...entry.shots].sort((a, b) => a - b),
  }))
})

const usageShotCount = computed(() =>
  usageSummary.value.reduce((sum, line) => sum + line.shots.length, 0),
)

async function openDelete(ids: number[]) {
  const unique = [...new Set(ids)]
  if (!unique.length) return
  deleteIds.value = unique
  deleteUsages.value = []
  usageFailed.value = false
  deleteOpen.value = true
  loadingUsage.value = true
  try {
    deleteUsages.value = await materialsApi.usage(unique)
  } catch (err) {
    usageFailed.value = true
    message.error(errorText(err, '读取引用情况失败'))
  } finally {
    loadingUsage.value = false
  }
}

async function confirmDelete() {
  if (!deleteIds.value.length) return
  // 快照 ids：提示里的「撤销」要对准这一次删的，不能跟着后面打开的另一批走。
  const ids = [...deleteIds.value]
  deleting.value = true
  try {
    const result = await materialsApi.batchRemove(ids)
    const removed = new Set(ids)
    deleteOpen.value = false
    selectedIds.value = selectedIds.value.filter((id) => !removed.has(id))
    if (previewItem.value && removed.has(previewItem.value.id)) {
      previewOpen.value = false
      previewItem.value = null
    }
    if (nameEditing.value && removed.has(nameEditing.value.id)) {
      nameEditorOpen.value = false
    }
    // 撤销入口就挂在提示上：删除后去别处翻「回收站」是另一套心智，窗口只有几分钟。
    message.success({
      content: h('span', [
        `已删除 ${result.deleted} 条素材` +
          (result.references ? `，${result.references} 个镜头还在引用` : ''),
        h(
          'a',
          {
            style: 'margin-left: 10px; font-weight: 600',
            onClick: () => void undoDelete(ids),
          },
          '撤销',
        ),
      ]),
      duration: Math.max(6, Math.min(result.undo_seconds || 10, 15)),
    })
    await load()
  } catch (err) {
    message.error(errorText(err, '删除失败'))
  } finally {
    deleting.value = false
  }
}

async function undoDelete(ids: number[]) {
  try {
    const { restored } = await materialsApi.restore(ids)
    if (!restored) {
      message.warning('没有可恢复的素材，可能已过撤销期限')
      return
    }
    message.success(`已恢复 ${restored} 条素材`)
    await load()
  } catch (err) {
    message.error(errorText(err, '撤销失败'))
  }
}

// -- 上传 ------------------------------------------------------------------

function patchUpload(id: number, next: Partial<UploadTask>) {
  const index = uploadTasks.value.findIndex((task) => task.id === id)
  if (index >= 0) uploadTasks.value[index] = { ...uploadTasks.value[index], ...next }
}

function dismissUpload(id: number) {
  uploadTasks.value = uploadTasks.value.filter((task) => task.id !== id)
}

async function onUpload(file: File) {
  const task: UploadTask = {
    id: ++uploadSeq,
    name: file.name,
    percent: 0,
    status: 'uploading',
  }
  uploadTasks.value.push(task)
  pendingUploads += 1
  try {
    const asset = await materialsApi.upload(file, (percent) => {
      // 到 100% 只代表浏览器把字节发完了，落库还要一会儿；这一格等回执再变绿。
      patchUpload(task.id, { percent: Math.min(99, percent) })
    })
    patchUpload(task.id, { percent: 100, status: 'done' })
    message.success(`已存入素材库：${asset.name}`)
    window.setTimeout(() => dismissUpload(task.id), 2000)
  } catch (err) {
    patchUpload(task.id, { status: 'error', error: errorText(err, `上传 ${file.name} 失败`) })
    message.error(errorText(err, `上传 ${file.name} 失败`))
  } finally {
    pendingUploads -= 1
    // 多选时 antd 会连着回调几次：等最后一条落库再刷新，不然一屏会在几秒里重取好几遍。
    if (!pendingUploads) await load()
  }
  // 返回 false 拦住 antd 的自动上传：文件由上面这次请求发出去了。
  return false
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
  bottom: 6px;
  left: 6px;
  margin: 0;
}

.batch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface-2);
}

.upload-tasks {
  margin-top: 12px;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface-2);
}

.upload-list {
  display: grid;
  gap: 6px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

.upload-item {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) 2fr auto;
  align-items: center;
  gap: 10px;
}

.upload-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: var(--fs-xs);
}

.upload-progress {
  margin: 0;
}

.upload-dismiss {
  font-size: var(--fs-xs);
}

.batch-count {
  font-weight: 600;
}

.material-cell--picked {
  border-color: var(--primary, #1677ff);
  box-shadow: inset 0 0 0 1px var(--primary, #1677ff);
}

.material-thumb {
  cursor: zoom-in;
}

/* 悬停才浮出来：默认让缩略图自己说话，不叠一层常驻按钮。 */
.thumb-mask {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: #fff;
  font-size: var(--fs-xs);
  background: rgba(0, 0, 0, 0.34);
  opacity: 0;
  transition: opacity 0.15s;
  pointer-events: none;
}

.material-thumb:hover .thumb-mask {
  opacity: 1;
}

/* 勾选常驻：批量操作要能被看见，不能只藏在悬停里。 */
.cell-check {
  position: absolute;
  top: 6px;
  left: 6px;
  padding: 2px 5px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.9);
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

.cell-actions {
  position: absolute;
  top: 6px;
  right: 6px;
  display: flex;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.92);
  opacity: 0;
  transition: opacity 0.15s;
}

.material-cell:hover .cell-actions,
.material-cell:focus-within .cell-actions {
  opacity: 1;
}

.preview-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.preview-media {
  max-width: 100%;
  max-height: 60vh;
  border-radius: 8px;
  background: #000;
}

.preview-meta {
  text-align: center;
}

.name-editor-hint {
  margin-top: 8px;
}

.usage-block {
  margin-top: 12px;
}

.usage-list {
  margin: 6px 0 0;
  padding-left: 18px;
  color: var(--text-2);
}

.tag-editor-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 0;
  margin-top: 10px;
}
</style>
