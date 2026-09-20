import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

// 只引需要的字重；中文字形不发货，回落系统字体栈。
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-sans/700.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'

import App from './App.vue'
import router from './router'
import './style.css'
import './assets/markdown.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(Antd)
// 等首屏导航解析完再挂载：否则直接打开 bare 页（报告、终端）时，App 会先按
// 未解析的空路由渲染出带菜单的主布局，导航完成后才换成裸页，闪一下外壳。
void router.isReady().then(() => app.mount('#app'))
