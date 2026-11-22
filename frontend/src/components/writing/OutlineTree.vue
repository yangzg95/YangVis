<template>
  <div class="outline-tree">
    <a-tree
      v-model:expanded-keys="expandedKeys"
      :selected-keys="selectedCid ? [selectedCid] : []"
      :tree-data="treeData"
      :field-names="{ title: 'title', key: 'cid', children: 'children' }"
      block-node
      class="tree-inner"
      @select="onSelect"
    >
      <template #title="{ dataRef }">
        <div class="node-row">
          <template v-if="dataRef.kind === 'volume'">
            <FolderOpenOutlined class="node-icon volume-icon" />
            <span class="node-label" :title="dataRef.title">{{ dataRef.title }}</span>
            <span class="node-count">{{ dataRef.children?.length ?? 0 }} 章</span>
            <span class="node-actions" @click.stop>
              <a-tooltip :title="locked ? '生成中，暂时不能编辑' : '在此卷末尾加章'">
                <a-button type="text" size="small" :disabled="locked" @click="emit('add', lastChapterCid(dataRef))">
                  <PlusOutlined />
                </a-button>
              </a-tooltip>
              <a-tooltip
                v-if="(dataRef.children?.length ?? 0) > 0"
                title="卷下还有章节，请先移空再删卷"
              >
                <a-button type="text" size="small" danger disabled>
                  <DeleteOutlined />
                </a-button>
              </a-tooltip>
              <a-popconfirm
                v-else
                title="删除这个空卷？"
                ok-text="删除"
                cancel-text="取消"
                @confirm="emit('removeVolume', dataRef)"
              >
                <a-button type="text" size="small" danger :disabled="locked">
                  <DeleteOutlined />
                </a-button>
              </a-popconfirm>
            </span>
          </template>

          <template v-else>
            <LoadingOutlined v-if="statusOf(dataRef.cid) === 'analyzing'" class="status-dot status-dot--running" />
            <a-tooltip v-else-if="statusOf(dataRef.cid) === 'error'" :title="errorOf(dataRef.cid) || '生成失败，点击查看运行记录'">
              <CloseCircleOutlined
                class="status-dot status-dot--error"
                @click.stop="emit('runs', dataRef.cid)"
              />
            </a-tooltip>
            <span class="node-label" :title="dataRef.title">{{ dataRef.title }}</span>
            <span class="node-words">{{ wordsOf(dataRef.cid) }} 字</span>
            <span class="node-actions" @click.stop>
              <a-tooltip :title="actionTip(dataRef.cid, work.kind === 'novel' ? '在后面加一章' : '在后面加一段')">
                <a-button type="text" size="small" :disabled="cidLocked(dataRef.cid)" @click="emit('add', dataRef.cid)">
                  <PlusOutlined />
                </a-button>
              </a-tooltip>
              <a-tooltip :title="actionTip(dataRef.cid, '上移')">
                <a-button type="text" size="small" :disabled="cidLocked(dataRef.cid)" @click="emit('move', dataRef.cid, -1)">
                  <ArrowUpOutlined />
                </a-button>
              </a-tooltip>
              <a-tooltip :title="actionTip(dataRef.cid, '下移')">
                <a-button type="text" size="small" :disabled="cidLocked(dataRef.cid)" @click="emit('move', dataRef.cid, 1)">
                  <ArrowDownOutlined />
                </a-button>
              </a-tooltip>
              <a-popconfirm
                :title="`确认删除「${dataRef.title}」？正文会一并删除。`"
                ok-text="删除"
                cancel-text="取消"
                @confirm="emit('remove', dataRef.cid)"
              >
                <a-button type="text" size="small" danger :disabled="cidLocked(dataRef.cid)">
                  <DeleteOutlined />
                </a-button>
              </a-popconfirm>
            </span>
          </template>
        </div>
      </template>
    </a-tree>

    <div class="tree-footer">
      <a-button block :disabled="locked" @click="emit('add', null)">
        <PlusOutlined /> {{ work.kind === 'novel' ? '加章节' : '加段落' }}
      </a-button>
      <a-button v-if="work.kind === 'novel'" block :disabled="locked" @click="emit('addVolume')">
        <FolderAddOutlined /> 加卷
      </a-button>
      <div v-if="locked" class="locked-hint">生成中，结构编辑已锁定</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  LoadingOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'
import type { OutlineNode, WritingPart, WritingPartStatus, WritingWorkDetail } from '@/api'

const props = defineProps<{
  work: WritingWorkDetail
  selectedCid: string | null
  /** 作品级运行或任一块生成期间冻结一切结构编辑：产物落地会整树覆盖，手改只会白写。 */
  locked?: boolean
}>()
const emit = defineEmits<{
  select: [cid: string]
  add: [afterCid: string | null]
  remove: [cid: string]
  removeVolume: [node: OutlineNode]
  move: [cid: string, dir: -1 | 1]
  addVolume: []
  /** 失败状态点被点击：打开该块的运行记录抽屉。 */
  runs: [cid: string]
}>()

// 新卷默认展开，否则加完卷看不到它的空 children 槽位。
const expandedKeys = ref<string[]>([])
watch(
  () => props.work.outline,
  (outline) => {
    const volumeIds = outline.filter((node) => node.kind === 'volume').map((node) => node.cid)
    expandedKeys.value = Array.from(new Set([...expandedKeys.value, ...volumeIds])).filter((cid) =>
      volumeIds.includes(cid),
    )
  },
  { immediate: true, deep: false },
)

// 卷节点不可选中（点击只做展开/折叠），在喂给 a-tree 的数据上打 selectable 标记。
const treeData = computed(() =>
  props.work.outline.map((node) => ({
    ...node,
    selectable: node.kind !== 'volume',
  })),
)

const partWords = computed(() => {
  const map = new Map<string, number>()
  for (const part of props.work.parts) map.set(part.cid, part.word_count)
  return map
})

const partState = computed(() => {
  const map = new Map<string, WritingPart>()
  for (const part of props.work.parts) map.set(part.cid, part)
  return map
})

function wordsOf(cid: string): number {
  return partWords.value.get(cid) ?? 0
}

function statusOf(cid: string): WritingPartStatus | undefined {
  return partState.value.get(cid)?.status
}

function errorOf(cid: string): string | null {
  return partState.value.get(cid)?.error_msg ?? null
}

/** 单块生成只锁自己那一行；作品级运行或别的块在跑时，其余行照常可操作。 */
function cidLocked(cid: string): boolean {
  return props.locked || statusOf(cid) === 'analyzing'
}

function actionTip(cid: string, base: string): string {
  if (statusOf(cid) === 'analyzing') return '该章节/段落正在生成，暂时不能编辑'
  if (props.locked) return '生成中，暂时不能编辑'
  return base
}

function lastChapterCid(volume: OutlineNode): string | null {
  const children = volume.children ?? []
  return children.length ? children[children.length - 1].cid : null
}

function onSelect(keys: (string | number)[]) {
  // 卷节点已在 treeData 上标 selectable:false，这里拿到的只会是叶子 cid。
  const cid = String(keys[0] ?? '')
  if (cid) emit('select', cid)
}
</script>

<style scoped>
.outline-tree {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
}

.tree-inner {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.node-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.node-icon {
  flex: none;
  color: var(--text-3);
}

.volume-icon {
  color: var(--signal-text);
}

.node-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-count,
.node-words {
  flex: none;
  color: var(--text-3);
  font-size: 12px;
}

.status-dot {
  flex: none;
  font-size: 13px;
}

.status-dot--running {
  color: var(--signal-text);
}

.status-dot--error {
  color: var(--error, #cf1322);
  cursor: pointer;
}

.node-actions {
  display: none;
  flex: none;
  margin-left: auto;
  align-items: center;
}

.node-row:hover .node-actions {
  display: inline-flex;
}

.node-actions :deep(.ant-btn) {
  width: 22px;
  height: 22px;
  padding: 0;
}

.tree-footer {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
}

.locked-hint {
  flex-basis: 100%;
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
}
</style>
