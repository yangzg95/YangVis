<template>
  <div ref="hostRef" class="sql-editor" />
</template>

<script setup lang="ts">
// 查询窗口的 SQL 编辑器：CodeMirror 6 + lang-sql。
//
// 补全分三层，全部由 lang-sql 的 sql() 扩展完成：
//   1. 关键字——MySQL 方言自带（upperCaseKeywords 让补全插入大写形式）；
//   2. 表名——schema 配置的顶层键；
//   3. 字段——schema 配置里每张表的 children，输入「表名.」后触发。
// 表结构数据由父组件拉取后通过 setSchema 热更新（Compartment 重配），
// 不用重建编辑器。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorState, Compartment, type Extension } from '@codemirror/state'
import {
  EditorView,
  keymap,
  placeholder as cmPlaceholder,
  highlightActiveLine,
} from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, insertNewlineAndIndent } from '@codemirror/commands'
import { bracketMatching, defaultHighlightStyle, syntaxHighlighting } from '@codemirror/language'
import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
  type Completion,
} from '@codemirror/autocomplete'
import { sql, MySQL } from '@codemirror/lang-sql'

const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
    /** 编辑器行高（按行数折算最小高度）。 */
    rows?: number
    /**
     * 拖拽调出来的显式高度（px）。给了就改用固定高度 + 内部滚动，
     * 不再走「rows 最小高 / 240 上限」的自动伸缩。
     */
    height?: number | null
  }>(),
  { placeholder: '', rows: 4, height: null },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  /** Enter / Ctrl+Enter：执行。补全浮层打开时 Enter 归补全，不会误触发。 */
  run: []
  /** 选区有无变化：父组件据此把按钮切到「执行选中」。 */
  'selection-change': [hasSelection: boolean]
}>()

const hostRef = ref<HTMLElement | null>(null)
let view: EditorView | null = null

// SQL 方言配置（含补全 schema）做成 Compartment：父组件拿到表结构后热替换，
// 编辑器实例和用户输入都保留。
const sqlConf = new Compartment()

function buildExtensions(): Extension[] {
  return [
    // Enter 执行、Shift+Enter 换行——与原来 textarea 的行为保持一致。
    // 顺序有意安排：completionKeymap 在前，补全浮层打开时 Enter 先归「选中补全项」，
    // 浮层没开才落到我们的执行绑定。
    keymap.of([
      ...completionKeymap,
      ...closeBracketsKeymap,
      {
        key: 'Enter',
        run: () => {
          emit('run')
          return true
        },
      },
      { key: 'Mod-Enter', run: () => {
          emit('run')
          return true
        },
      },
      { key: 'Shift-Enter', run: insertNewlineAndIndent },
      ...historyKeymap,
      ...defaultKeymap,
    ]),
    history(),
    closeBrackets(),
    autocompletion(),
    bracketMatching(),
    highlightActiveLine(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    sqlConf.of(sql({ dialect: MySQL, upperCaseKeywords: true })),
    cmPlaceholder(props.placeholder),
    EditorView.lineWrapping,
    EditorView.updateListener.of((update) => {
      if (update.docChanged) emit('update:modelValue', update.state.doc.toString())
      if (update.docChanged || update.selectionSet) {
        emit('selection-change', !update.state.selection.main.empty)
      }
    }),
    EditorView.theme({
      '&': {
        fontSize: '13px',
        minHeight: `${props.rows * 22 + 8}px`,
        maxHeight: '240px',
        border: '1px solid #d9d9d9',
        borderRadius: '6px',
        backgroundColor: '#fff',
      },
      // 聚焦态描边跟 antd 输入框的 signal 色一致。
      '&.cm-focused': {
        outline: 'none',
        borderColor: 'var(--signal-border, #4096ff)',
        boxShadow: '0 0 0 2px rgba(5, 145, 255, 0.1)',
      },
      '.cm-scroller': {
        fontFamily: 'var(--font-mono)',
        lineHeight: '22px',
        overflow: 'auto',
      },
      '.cm-content': { padding: '4px 8px' },
      '.cm-line': { padding: '0' },
      '.cm-placeholder': { color: '#bfbfbf' },
      '.cm-activeLine': { backgroundColor: 'rgba(0, 0, 0, 0.03)' },
      // 提示浮层：关键字 / 表 / 字段的类型小图标列宽对齐。
      '.cm-tooltip.cm-tooltip-autocomplete': {
        border: '1px solid var(--hairline, #f0f0f0)',
        borderRadius: '6px',
        boxShadow: 'var(--shadow-overlay, 0 6px 16px rgba(0,0,0,0.12))',
      },
      '.cm-tooltip-autocomplete ul li[aria-selected]': {
        backgroundColor: 'var(--signal-bg, #e6f4ff)',
        color: 'inherit',
      },
    }),
  ]
}

onMounted(() => {
  view = new EditorView({
    state: EditorState.create({ doc: props.modelValue, extensions: buildExtensions() }),
    parent: hostRef.value!,
  })
  applyHeight(props.height)
})

onBeforeUnmount(() => {
  view?.destroy()
  view = null
})

// 外部赋值（收藏、示例、AI 带过来的 SQL）同步进编辑器；
// 编辑器自己产生的输入已在 updateListener 里回过父组件，值相同就不再回写。
watch(
  () => props.modelValue,
  (value) => {
    if (!view || value === view.state.doc.toString()) return
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } })
  },
)

/** 热更新补全用的表结构：表名 → 列名（带类型，显示在补全项右侧）。 */
function setSchema(tables: { name: string; columns: { name: string; column_type: string }[] }[]) {
  if (!view) return
  // SQLNamespace 的「表」形态是 {self, children}：self 是表名补全项本身，
  // children 是输入「表名.」后弹出的字段列表。
  const schema: Record<string, { self: Completion; children: Completion[] }> = {}
  for (const table of tables) {
    schema[table.name] = {
      self: { label: table.name, type: 'table' },
      children: table.columns.map<Completion>((col) => ({
        label: col.name,
        type: 'property',
        detail: col.column_type || undefined,
      })),
    }
  }
  view.dispatch({
    effects: sqlConf.reconfigure(
      sql({ dialect: MySQL, upperCaseKeywords: true, schema }),
    ),
  })
}

function focus() {
  view?.focus()
}

/** 当前选中的文本（执行选中用）；没选区时返回空串。 */
function getSelection(): string {
  if (!view) return ''
  const { from, to } = view.state.selection.main
  return view.state.doc.sliceString(from, to)
}

// 显式高度走宿主元素的内联样式 + sized class（关掉主题里的 min/max 高度），
// 拖拽时只改样式，不动编辑器状态。
function applyHeight(height: number | null | undefined) {
  const host = hostRef.value
  if (!host) return
  if (height && height > 0) {
    host.classList.add('sized')
    host.style.height = `${Math.round(height)}px`
  } else {
    host.classList.remove('sized')
    host.style.height = ''
  }
}

watch(() => props.height, applyHeight)

defineExpose({ setSchema, focus, getSelection })
</script>

<style scoped>
.sql-editor {
  flex: 1;
  min-width: 0;
}

/* 拖拽给定显式高度时：编辑器填满宿主、内部滚动。
   三层类名压住 CodeMirror 主题里的 min/max-height（单层类选择器）。
   注意宿主不能动 flex：宽度靠在 .editor 行容器里的 flex:1 撑满，
   高度走宿主内联样式（容器 align-items:flex-start，不会纵向拉伸），
   曾经在这里写 flex:none 想把高度定死，结果把宽度也塌成了内容宽。 */
.sql-editor.sized :deep(.cm-editor) {
  height: 100%;
  min-height: 0;
  max-height: none;
}
</style>
