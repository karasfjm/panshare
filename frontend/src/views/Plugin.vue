<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'
import type { PluginInfo } from '../lib/types'

// 插件页面用 iframe 内嵌：插件自己是独立进程、独立端口，核心只做反代。
// 这样插件升级不用动前端构建，插件崩了也只影响这一块。
const route = useRoute()
const id = computed(() => String(route.params.id || ''))
const src = computed(() => `/api/plugins/${id.value}/proxy/`)

const info = ref<PluginInfo | null>(null)
const err = ref('')
const loading = ref(true)
const frameKey = ref(0)

async function load() {
  loading.value = true
  err.value = ''
  try {
    const res = await api.get<{ plugins: PluginInfo[] }>('/plugins')
    info.value = (res.plugins || []).find((p) => p.id === id.value) || null
    if (!info.value) err.value = '找不到这个插件'
    else if (!info.value.installed) err.value = '插件还没安装'
    else if (!info.value.running) err.value = '插件没有在运行'
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function start() {
  try {
    await api.post(`/plugins/${id.value}/start`)
    await load()
    frameKey.value++
  } catch {
    /* 失败时 load 会把状态再读一遍 */
    await load()
  }
}

watch(id, load, { immediate: true })
</script>

<template>
  <div class="plugin-frame">
    <div v-if="loading" class="plugin-tip">正在打开…</div>
    <div v-else-if="err" class="plugin-tip">
      <div>{{ err }}</div>
      <div class="row">
        <button v-if="info" class="btn sm primary" @click="start">启动插件</button>
        <router-link class="btn sm" to="/plugins">去插件页</router-link>
      </div>
    </div>
    <iframe v-else :key="frameKey" :src="src" :title="info?.name || id" />
  </div>
</template>

<style scoped>
.plugin-frame {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
.plugin-frame iframe {
  flex: 1;
  width: 100%;
  border: 0;
  display: block;
  background: #f5f6f8;
}
.plugin-tip {
  margin: 40px auto;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: center;
}
.plugin-tip .row {
  display: flex;
  gap: 8px;
}
</style>
