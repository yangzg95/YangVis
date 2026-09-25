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
      chunkSizeWarningLimit: 1500,
      rollupOptions: {
        output: {
          manualChunks: {
            vue: ['vue', 'vue-router', 'pinia'],
            antd: ['ant-design-vue'],
          },
        },
      },
    },
  }
})
