import {createRouter, createWebHistory, type RouteRecordRaw} from 'vue-router'
import {isAuthenticated} from '@/utils/auth'

const routes: RouteRecordRaw[] = [
    {
        path: '/login',
        name: 'Login',
        component: () => import('@/views/Login.vue'),
        meta: {title: '登录', public: true, bare: true},
    },
    {
        path: '/',
        redirect: '/customer-service',
    },
    {
        path: '/service',
        redirect: '/customer-service',
    },
    {
        path: '/customer-service',
        name: 'CustomerService',
        component: () => import('@/views/CustomerService.vue'),
        meta: {title: '对话'},
    },
    {
        path: '/office',
        redirect: '/office/resume',
    },
    {
        path: '/office/resume',
        name: 'Resume',
        component: () => import('@/views/Resume.vue'),
        meta: {title: '简历'},
    },
    {
        path: '/office/slides',
        name: 'Slides',
        component: () => import('@/views/Slides.vue'),
        meta: {title: '幻灯片'},
    },
    {
        // 幻灯片工作台：整页编辑（页面列表 + 表单 + 源码 + 实时预览）。
        // 与数据库问答页同一模式——bare 跳过主布局外壳，从列表页新开标签页进来。
        path: '/office/slides/:id(\\d+)/workbench',
        name: 'SlideWorkbench',
        component: () => import('@/views/SlideWorkbench.vue'),
        meta: {title: '幻灯片工作台', bare: true},
    },
    {
        // 放映页：满屏播放 + 键盘翻页 + 全屏 + 就地改内容。
        path: '/office/slides/:id(\\d+)/show',
        name: 'SlideShow',
        component: () => import('@/views/SlideShow.vue'),
        meta: {title: '幻灯片放映', bare: true},
    },
    {
        // 报告整页展示：列表页「查看报告」新开标签页进这里，bare 跳过主布局外壳，
        // 与全屏终端页同一模式。不加 public，未登录照样被守卫拦下。
        path: '/office/report/:kind(resume|comparison|toolkit)/:id(\\d+)',
        name: 'ReportView',
        component: () => import('@/views/ReportView.vue'),
        meta: {title: '报告', bare: true},
    },
    {
        // 面试详情整页展示：列表「详情」新开标签页进这里，与报告页同一模式。
        path: '/office/interview/:id(\\d+)',
        name: 'InterviewDetail',
        component: () => import('@/views/InterviewDetailView.vue'),
        meta: {title: '面试详情', bare: true},
    },
    {
        path: '/ops',
        redirect: '/ops/server',
    },
    {
        path: '/ops/server',
        name: 'OpsServer',
        component: () => import('@/views/OpsServer.vue'),
        meta: {title: '服务器运维'},
    },
    {
        // 全屏终端页：bare 跳过主布局外壳，但不加 public，未登录照样被守卫拦下。
        // \d+ 约束让非数字 id 直接落到 catch-all，页面里不用处理垃圾参数。
        path: '/ops/server/:id(\\d+)',
        name: 'OpsServerTerminal',
        component: () => import('@/views/OpsServerTerminal.vue'),
        meta: {title: '服务器终端', bare: true},
    },
    {
        path: '/ops/database',
        name: 'OpsDatabase',
        component: () => import('@/views/OpsDatabase.vue'),
        meta: {title: '数据库运维'},
    },
    {
        // 全屏智能问答页：bare 跳过主布局外壳，与服务器终端页同一模式。
        path: '/ops/database/:id(\\d+)',
        name: 'OpsDatabaseChat',
        component: () => import('@/views/OpsDatabaseChat.vue'),
        meta: {title: '数据库问答', bare: true},
    },
    {
        path: '/agents',
        name: 'Agents',
        component: () => import('@/views/Agents.vue'),
        meta: {title: '智能体'},
    },
    {
        path: '/system',
        redirect: '/settings',
    },
    {
        path: '/settings',
        name: 'Settings',
        component: () => import('@/views/Settings.vue'),
        meta: {title: '通用设置'},
    },
    {
        path: '/system/users',
        name: 'Users',
        component: () => import('@/views/Users.vue'),
        meta: {title: '用户管理'},
    },
    {
        path: '/system/ai-gateway',
        name: 'AiGateway',
        component: () => import('@/views/AiGateway.vue'),
        meta: {title: 'AI 网关'},
    },
    {
        path: '/knowledge',
        name: 'Knowledge',
        component: () => import('@/views/Knowledge.vue'),
        meta: {title: '知识库'},
    },
    {
        path: '/:pathMatch(.*)*',
        redirect: '/customer-service',
    },
]

const router = createRouter({
    history: createWebHistory('/'),
    routes,
})

router.beforeEach((to) => {
    if (to.meta?.public) {
        return true
    }
    if (isAuthenticated()) {
        return true
    }
    return {path: '/login', query: {redirect: to.fullPath}, replace: true}
})

router.afterEach((to) => {
    const title = (to.meta?.title as string | undefined) || '杨维斯'
    document.title = `${title} · 杨维斯`
})

export default router
