/**
 * 前端 API 层：一个业务域一个文件，这里只汇总转发，消费方照旧 `from '@/api'`。
 *
 * 切分沿的是原文件自带的 `// ---- 分节 ----` 横幅，注释与实现一起搬，行为一字未改。
 * 公共件只有两个：http.ts 的 request（所有发请求的域都用它）与 sse.ts 的拉流实现
 * （对话与工作流共用，故不给它做全量转发，只把公开过的 StreamHandlers 传出去）。
 * 域文件之间的交叉只剩一处：ops.ts 借 settings.ts 的 TestResult 表达「测一下通不通」
 * 的返回。sse.ts 对两个域的回边是 import type，编译后擦除，不构成运行时环。
 */
export * from './http'
export * from './auth'
export * from './knowledge'
export * from './settings'
export * from './agents'
export * from './workflows'
export * from './mcp'
export * from './memory'
export * from './chat'
export type { StreamHandlers } from './sse'
export * from './ops'
export * from './ops-browser'
export * from './resume'
export * from './interview'
export * from './netdisk'
export * from './ai-gateway'
export * from './slides'
export * from './dashboard'
