import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'node:path'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    base: '/',
    plugins: [vue()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': {
          target: env.VITE_API_TARGET || 'http://localhost:18099',
          changeOrigin: true,
          // 运维终端和运维问答都走 WebSocket，开发代理必须一并转发 upgrade 请求。
          ws: true,
        },
        // AI 网关对外的 OpenAI 兼容端点挂在站点根的 /v1（不带 /api 前缀），
        // 所以开发环境也要单独转发，否则 5173 上访问会被 SPA 回退吃掉。
        '/v1': {
          target: env.VITE_API_TARGET || 'http://localhost:18099',
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      // 800KB：路由块与功能域 vendor 块一旦超过就是新增回归。
      // 已知会超限的只有 antd（全量注册遗留，改成按文件引入后才会降下来）；
      // elk 只由工作流画布的动态 import 引入，mermaid 内部复用同一份模块。
      chunkSizeWarningLimit: 800,
      rollupOptions: {
        output: {
          // 按功能域切稳定 vendor 块：业务代码改动不会让这些大依赖的 hash 变化，
          // 浏览器强缓存可以一直命中；其余依赖交给 Rollup 按动态 import 自动切。
          manualChunks(id) {
            const normalized = id.replace(/\\/g, '/')
            if (!normalized.includes('/node_modules/')) return
            if (/\/node_modules\/(vue|vue-router|pinia|@vue)\//.test(normalized)) return 'vue'
            if (/\/node_modules\/ant-design-vue\//.test(normalized)) return 'antd'
            if (/\/node_modules\/(@codemirror|@lezer|codemirror)\//.test(normalized)) return 'codemirror'
            if (/\/node_modules\/(@xterm|xterm)\//.test(normalized)) return 'xterm'
          },
        },
      },
    },
  }
})
