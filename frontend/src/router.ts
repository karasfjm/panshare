import { createRouter, createWebHistory } from 'vue-router'
import { fetchMe } from './lib/api'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { public: true } },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('./views/Dashboard.vue'),
    meta: { title: '仪表盘', desc: '运行状态与最近的动静' },
  },
  {
    path: '/accounts',
    name: 'accounts',
    component: () => import('./views/Accounts.vue'),
    meta: { title: '115 助手', desc: '扫码或 Cookie 添加账号，所有工具都从这里取账号' },
  },
  {
    path: '/strm',
    name: 'strm',
    component: () => import('./views/Strm.vue'),
    meta: { title: '115 STRM', desc: '按「账号 + 网盘路径」生成 strm，增量 / 全量 / 全新' },
  },
  {
    path: '/share',
    name: 'share',
    component: () => import('./views/Share.vue'),
    meta: { title: '115 分享转存', desc: '转存 → 建自己的长期分享 → strm 走分享通道' },
  },
  {
    path: '/ed2k',
    name: 'ed2k',
    component: () => import('./views/Ed2k.vue'),
    meta: { title: 'ed2k STRM', desc: 'ed2k 链接接入 115 与 STRM，播放时再转存' },
  },
  {
    path: '/instant',
    name: 'instant',
    component: () => import('./views/Instant.vue'),
    meta: { title: '115 秒传', desc: '按 Emby 用户映射被秒传账号，播放前先秒传' },
  },
  {
    path: '/media',
    name: 'media',
    component: () => import('./views/Media.vue'),
    meta: { title: 'Emby 服务器', desc: '登记 Emby 地址与 API Key，供秒传映射、联动删除使用' },
  },
  {
    path: '/notify',
    name: 'notify',
    component: () => import('./views/Notify.vue'),
    meta: { title: '通知', desc: '任务开始 / 完成 / 失败推送到 Telegram 与微信，聊天窗口里发指令' },
  },
  {
    path: '/linker',
    name: 'linker',
    component: () => import('./views/Linker.vue'),
    meta: { title: '联动删除', desc: '本地 strm 与网盘双向对齐，带宽限期' },
  },
  {
    path: '/plugins',
    name: 'plugins',
    component: () => import('./views/Plugins.vue'),
    meta: { title: '插件', desc: '天翼云盘等插件按需安装' },
  },
  {
    // 插件页面整屏内嵌（左侧菜单里点插件名进这里，不跳独立页面）
    path: '/plugin/:id',
    name: 'plugin',
    component: () => import('./views/Plugin.vue'),
    meta: { title: '插件', bare: true },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('./views/Settings.vue'),
    meta: { title: '服务设置', desc: '映射根、请求头、默认扩展名与运行参数' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    // 先用缓存：每次跳页都重新问一遍 /api/me，日志会被刷满
    let me = await fetchMe()
    if (!me.logged_in) me = await fetchMe(true)
    if (!me.logged_in) return { name: 'login', query: { next: to.fullPath } }
    return true
  } catch {
    return { name: 'login' }
  }
})
