<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, fetchMe } from '../lib/api'

const router = useRouter()
const route = useRoute()
const username = ref('admin')
const password = ref('')
const siteName = ref('PanShare')
const busy = ref(false)
const error = ref('')
const bgUrl = ref('')
const bgLoading = ref(false)
let objectUrl = ''

// 背景图走后端公开接口：TMDB Key 和代理都不暴露给浏览器。
async function loadBackground() {
  bgLoading.value = true
  try {
    const res = await fetch(`/api/login/background?v=${Date.now()}`, { credentials: 'same-origin' })
    if (!res.ok || res.status === 204) {
      bgUrl.value = ''
      return
    }
    const blob = await res.blob()
    if (!blob.type.startsWith('image/')) {
      bgUrl.value = ''
      return
    }
    const next = URL.createObjectURL(blob)
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    objectUrl = next
    bgUrl.value = next
  } catch {
    bgUrl.value = ''
  } finally {
    bgLoading.value = false
  }
}

onMounted(async () => {
  loadBackground()
  try {
    const me = await fetchMe(true)
    siteName.value = me.site_name || 'PanShare'
    if (me.username) username.value = me.username
    if (me.logged_in) router.replace((route.query.next as string) || '/')
  } catch {
    /* 未登录就留在登录页 */
  }
})

onUnmounted(() => {
  if (objectUrl) URL.revokeObjectURL(objectUrl)
})

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await api.post('/login', { username: username.value, password: password.value })
    await fetchMe(true)
    router.replace((route.query.next as string) || '/')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div v-if="bgUrl" class="login-bg" :style="{ backgroundImage: `url(${bgUrl})` }"></div>
    <div class="login-veil"></div>

    <form class="login-card" @submit.prevent="submit">
      <h1>{{ siteName }}</h1>
      <div class="sub">115 STRM · 分享转存 · 秒传 · Emby 302</div>

      <label class="field">
        <span>账号</span>
        <input v-model="username" autocomplete="username" placeholder="admin" />
      </label>
      <label class="field">
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password" placeholder="compose 里设置的密码" />
      </label>

      <div v-if="error" class="notice err mb12">{{ error }}</div>
      <button class="btn primary block" :disabled="busy">{{ busy ? '登录中…' : '登录' }}</button>

      <div class="login-foot mt12">
        <span class="small muted">账号密码由 compose 的 PS_USERNAME / PS_PASSWORD 决定（默认 admin / password）</span>
        <button type="button" class="btn ghost sm" :disabled="bgLoading" @click="loadBackground">
          {{ bgLoading ? '换图中…' : '换一张背景' }}
        </button>
      </div>
      <div class="small muted mt8">背景随机取自 TMDB 热门影视，走 Telegram 同一个代理。</div>
    </form>
  </div>
</template>
