/**
 * 简历管理三张表格的列定义。
 *
 * 列宽与 key 纯属展示数据，挪出页面后 Resume.vue 只保留加载与轮询逻辑；
 * 操作列的按钮仍由模板的 bodyCell 按 key 分发。
 */

export const docColumns = [
  { key: 'title', title: '简历', dataIndex: 'title', ellipsis: true },
  { key: 'updated_at', title: '更新时间', dataIndex: 'updated_at', width: 220 },
  { key: 'actions', title: '操作', width: 300 },
]

export const resumeColumns = [
  { key: 'title', title: '简历', dataIndex: 'title', ellipsis: true },
  { key: 'size', title: '大小', dataIndex: 'size', width: 90 },
  { key: 'description', title: '描述', dataIndex: 'description', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '上传时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 300 },
]

export const comparisonColumns = [
  { key: 'title', title: '对比', dataIndex: 'title', ellipsis: true },
  { key: 'status', title: '状态', dataIndex: 'status', width: 110 },
  { key: 'created_at', title: '发起时间', dataIndex: 'created_at', width: 180 },
  { key: 'actions', title: '操作', width: 180 },
]
