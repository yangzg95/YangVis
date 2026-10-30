import { createApp, type Plugin } from 'vue'
import { createPinia } from 'pinia'
import {
  Alert,
  AutoComplete,
  Avatar,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  ConfigProvider,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Layout,
  List,
  Menu,
  Modal,
  Pagination,
  Popconfirm,
  Popover,
  Radio,
  Rate,
  Result,
  Row,
  Segmented,
  Select,
  Skeleton,
  Slider,
  Space,
  Spin,
  Statistic,
  Steps,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Tree,
  Upload,
} from 'ant-design-vue'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import 'ant-design-vue/dist/reset.css'

// 只引需要的字重；中文字形不发货，回落系统字体栈。
// 700 全站只有简历工作台一处小节标题在用，缺字重时浏览器会回落到 600，
// 不值得为它多下一份字体文件。
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'

// 主题 store 的模块副作用会在挂载前把 data-theme 写到 <html>，必须早于 App 引入。
import './stores/theme'
import App from './App.vue'
import router from './router'
import './style.css'
import './assets/markdown.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// AntD 4 的每个组件自带 install，安装父组件会连同子组件一起注册
// （Button→ButtonGroup、Input→Group/Search/Password/TextArea、Tabs→TabPane 等）。
// 这里只列全站模板实际用到的组件，替代 `app.use(Antd)` 的全量注册——
// 全量注册会把 Calendar/Carousel/Cascader/TreeSelect 等未使用的组件也打进入口包。
const antdComponents: Plugin[] = [
  Alert,
  AutoComplete,
  Avatar,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  ConfigProvider,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Layout,
  List,
  Menu,
  Modal,
  Pagination,
  Popconfirm,
  Popover,
  Radio,
  Rate,
  Result,
  Row,
  Segmented,
  Select,
  Skeleton,
  Slider,
  Space,
  Spin,
  Statistic,
  Steps,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Tree,
  Upload,
]
antdComponents.forEach((component) => app.use(component))
// 日期面板的月份/星期名由 dayjs 的语言数据出，App.vue 的 ConfigProvider zhCN 只管
// 组件文案（占位符、确定、今天）。不注册 zh-cn，dayjs 会静默按英文渲染成 Jan/Feb。
dayjs.locale('zh-cn')
// 等首屏导航解析完再挂载：否则直接打开 bare 页（报告、终端）时，App 会先按
// 未解析的空路由渲染出带菜单的主布局，导航完成后才换成裸页，闪一下外壳。
void router.isReady().then(() => app.mount('#app'))
