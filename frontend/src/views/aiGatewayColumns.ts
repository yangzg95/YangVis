/**
 * AI 网关各表格的列定义。
 *
 * 列宽、对齐与 key 名纯属展示数据，挪出页面脚本后，AiGateway.vue 只保留
 * 加载逻辑与表单状态；bodyCell 的渲染仍由模板按 key 分发。
 */

export const breakdownColumns = [
  { title: '名称', dataIndex: 'name', ellipsis: true },
  { title: '调用', dataIndex: 'calls', width: 70 },
  { title: '失败', key: 'failed', width: 70 },
  { title: 'Token', key: 'total_tokens', width: 100 },
  { title: '均耗时', dataIndex: 'avg_latency_ms', width: 90 },
]

export const channelColumns = [
  { title: '名称', dataIndex: 'name', width: 160 },
  { title: 'base_url', dataIndex: 'base_url', ellipsis: true },
  { title: 'api_key', key: 'api_key', width: 140 },
  { title: '模型', key: 'models', width: 260 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '连通性', key: 'last_test', width: 200 },
  { title: '操作', key: 'action', width: 200 },
]

export const routeColumns = [
  { title: '对外模型名', key: 'model_name', width: 220 },
  { title: '通道', key: 'channel_name', width: 160 },
  { title: '上游模型名', key: 'upstream_model', width: 220 },
  { title: '优先级', dataIndex: 'priority', width: 90 },
  { title: '有效期 / 额度', key: 'limit', width: 230 },
  { title: 'Auto 定义', key: 'auto', width: 240 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
  { title: '操作', key: 'action', width: 150 },
]

export const keyColumns = [
  { title: '名称', dataIndex: 'name', width: 180 },
  { title: '密钥', key: 'key_prefix', width: 200 },
  { title: '状态', key: 'enabled', width: 90 },
  { title: '调用次数', dataIndex: 'call_count', width: 100 },
  { title: '最近调用', key: 'last_used_at', width: 180 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
  { title: '操作', key: 'action', width: 200 },
]

export const logColumns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '密钥', dataIndex: 'key_name', width: 130, ellipsis: true },
  { title: '模型', key: 'model', width: 200 },
  { title: '通道', dataIndex: 'channel_name', width: 140, ellipsis: true },
  { title: '结果', key: 'success', width: 110 },
  { title: 'Token', key: 'tokens', width: 160 },
  { title: '缓存命中率', key: 'cache_hit', width: 110 },
  { title: '耗时', key: 'latency', width: 170 },
  { title: '来源', dataIndex: 'client_ip', width: 130 },
]

export const attemptColumns = [
  { title: '通道', dataIndex: 'channel_name' },
  { title: '状态码', dataIndex: 'status_code', width: 90 },
  { title: '耗时(ms)', dataIndex: 'latency_ms', width: 100 },
  { title: '错误', dataIndex: 'error', ellipsis: true },
]
