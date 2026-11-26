<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, fetchMe } from './lib/api'
import { toasts } from './lib/toast'
import type { PluginInfo } from './lib/types'

const route = useRoute()
const router = useRouter()
const sidebarOpen = ref(false)

/** 已安装的插件会直接出现在左侧菜单里，点开就在本站渲染，不再跳独立页面 */
const pluginNav = ref<{ to: string; label: string; icon: string }[]>([])

const ICONS: Record<string, string> = { cloud189: '☁' }

async function loadPlugins() {
  try {
    const res = await api.get<{ plugins: PluginInfo[] }>('/plugins')
    pluginNav.value = (res.plugins || [])
      .filter((p) => p.installed)
      .map((p) => ({ to: `/plugin/${p.id}`, label: p.name, icon: ICONS[p.id] || '⧉' }))
  } catch {
    // 登录页 / 插件目录为空都不影响导航
    pluginNav.value = []
  }
}
onMounted(loadPlugins)
// 装 / 卸插件后左侧菜单要跟着变：每次换页顺手读一遍（本地接口，很轻）
watch(
  () => route.fullPath,
  () => {
    // 换页就把抽屉收起来，否则手机上跳过去还盖着半屏
    sidebarOpen.value = false
    if (route.name !== 'login') loadPlugins()
  },
)

// 抽屉打开时锁住页面滚动，不然手指一划背景跟着跑，抽屉像是浮在飘的东西上
watch(sidebarOpen, (open) => {
  document.body.style.overflow = open ? 'hidden' : ''
})
onUnmounted(() => {
  document.body.style.overflow = ''
})

const groups = computed(() => [
  {
    name: '概览',
    items: [{ to: '/', label: '仪表盘', icon: '◎' }],
  },
  {
    name: '115',
    items: [
      { to: '/accounts', label: '115 助手', icon: '☺' },
      { to: '/strm', label: '115 STRM', icon: '≡' },
      { to: '/share', label: '分享转存', icon: '↗' },
      { to: '/ed2k', label: 'ed2k STRM', icon: '⛓' },
      { to: '/instant', label: '115 秒传', icon: '⚡' },
    ],
  },
  {
    name: '媒体',
    items: [{ to: '/media', label: 'Emby 服务器', icon: '▷' }],
  },
  ...(pluginNav.value.length ? [{ name: '插件', items: pluginNav.value }] : []),
  {
    name: '系统',
    items: [
      { to: '/notify', label: '通知', icon: '✈' },
      { to: '/linker', label: '联动删除', icon: '⌫' },
      { to: '/plugins', label: '插件', icon: '⧉' },
      { to: '/settings', label: '服务设置', icon: '⚙' },
    ],
  },
])

const isLogin = computed(() => route.name === 'login')
/** bare：整页交给插件自己渲染（藏掉顶栏，iframe 铺满） */
const bare = computed(() => !!route.meta.bare)
const title = computed(() => (route.meta.title as string) || 'PanShare')
const desc = computed(() => (route.meta.desc as string) || '')

async function logout() {
  try {
    await api.post('/logout')
  } catch {
    /* 忽略 */
  }
  await fetchMe(true).catch(() => null)
  window.location.href = '/login'
}
</script>

<template>
  <router-view v-if="isLogin" />

  <div v-else class="shell">
    <!-- 插件页整屏铺 iframe、顶栏不渲染：手机上少了这个就退不出去 -->
    <button v-if="bare" class="bare-back" aria-label="返回" @click="router.push('/')">←</button>

    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="brand">
        <div class="logo">P</div>
        <div>
          <div class="name">PanShare</div>
          <div class="ver">115 · STRM · Emby</div>
        </div>
        <button class="drawer-x" aria-label="关闭菜单" @click="sidebarOpen = false">✕</button>
      </div>
      <nav class="nav">
        <template v-for="g in groups" :key="g.name">
          <div class="group">{{ g.name }}</div>
          <router-link
            v-for="item in g.items"
            :key="item.to"
            :to="item.to"
            active-class="active"
            @click="sidebarOpen = false"
          >
            <span class="ico">{{ item.icon }}</span>{{ item.label }}
          </router-link>
        </template>
      </nav>
      <div class="foot">
        <a href="https://115.com" target="_blank" rel="noreferrer">115 网盘</a>
        <span> · </span>
        <a href="/api/status" target="_blank">API</a>
      </div>
    </aside>

    <!-- 手机端点抽屉外面任意位置收起；桌面端这条永远不显示 -->
    <div v-if="sidebarOpen" class="scrim" @click="sidebarOpen = false"></div>

    <div class="main">
      <header class="topbar" v-if="!bare">
        <button
          class="btn ghost sm menu-btn"
          :aria-expanded="sidebarOpen"
          aria-label="打开菜单"
          @click.stop="sidebarOpen = !sidebarOpen"
        >
          ☰
        </button>
        <div>
          <div class="title">{{ title }}</div>
          <div class="sub" v-if="desc">{{ desc }}</div>
        </div>
        <div class="spacer"></div>
        <button class="btn ghost sm" @click="logout">退出</button>
      </header>
      <main class="page" :class="{ bare }">
        <router-view />
      </main>
    </div>
  </div>

  <div class="toasts">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="{ err: t.kind === 'err' }">{{ t.text }}</div>
  </div>
</template>
