<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { PluginInfo } from '../lib/types'

const plugins = ref<PluginInfo[]>([])
const busy = ref('')
const router = useRouter()

async function load() {
  try {
    const res = await api.get<{ plugins: PluginInfo[] }>('/plugins')
    plugins.value = res.plugins || []
  } catch (e) {
    toastError(e)
  }
}
onMounted(load)

async function act(p: PluginInfo, action: 'install' | 'start' | 'stop') {
  busy.value = p.id + action
  try {
    await api.post(`/plugins/${p.id}/${action}`)
    toast(action === 'install' ? '已安装并启动' : action === 'start' ? '已启动' : '已停止')
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    busy.value = ''
  }
}

async function uninstall(p: PluginInfo) {
  if (!window.confirm(`卸载插件「${p.name}」？\n插件自己的账号与配置会一起删除。`)) return
  busy.value = p.id + 'uninstall'
  try {
    await api.del(`/plugins/${p.id}`)
    toast('已卸载')
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    busy.value = ''
  }
}

function open(p: PluginInfo) {
  // 装好就进站内页面（左侧菜单也会出现它），不再另开一个独立页面
  router.push(`/plugin/${p.id}`)
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>已内置的插件</h3>
        <div class="desc">
          插件按需安装：不装完全不占资源，装坏了也不影响主服务。安装后在独立进程里跑一段独立端口，
          核心只做反代。
        </div>
      </div>
      <div class="actions"><button class="btn sm" @click="load">刷新</button></div>
    </div>

    <div v-if="!plugins.length" class="empty">没有找到插件（镜像里 plugins 目录为空）</div>
    <div class="list">
      <div v-for="p in plugins" :key="p.id" class="row-item">
        <div style="font-size: 22px; width: 34px; text-align: center">⧉</div>
        <div class="body">
          <div class="title">
            {{ p.name }}
            <span class="badge">v{{ p.version }}</span>
            <span class="badge" :class="p.running ? 'ok' : p.installed ? 'warn' : ''">
              {{ p.running ? '运行中' : p.installed ? '已安装 · 未运行' : '未安装' }}
            </span>
            <span class="badge err" v-if="!p.binary">缺少可执行文件</span>
          </div>
          <div class="meta">{{ p.description }}</div>
          <div class="meta" v-if="p.running">端口 {{ p.port }} · 入口 /api/plugins/{{ p.id }}/proxy/</div>
          <div class="meta" v-if="p.error" style="color: var(--err)">{{ p.error }}</div>
        </div>
        <div class="actions">
          <template v-if="!p.installed">
            <button class="btn sm primary" :disabled="busy === p.id + 'install'" @click="act(p, 'install')">
              安装
            </button>
          </template>
          <template v-else>
            <button class="btn sm primary" v-if="!p.running" :disabled="busy === p.id + 'start'" @click="act(p, 'start')">启动</button>
            <button class="btn sm" v-else :disabled="busy === p.id + 'stop'" @click="act(p, 'stop')">停止</button>
            <button class="btn sm" :disabled="!p.running" @click="open(p)">打开</button>
            <button class="btn sm danger" :disabled="busy === p.id + 'uninstall'" @click="uninstall(p)">卸载</button>
          </template>
        </div>
      </div>
    </div>

    <div class="notice mt16">
      <div>
        <strong>天翼云盘</strong>是第一个插件：安装后左侧菜单会多出「天翼云盘」，点开就能扫码 / Cookie 登录，
        选个人云或家庭云目录生成 strm，播放时由插件现取直链再 302。卸载只会删插件自己的数据，不碰 115 的任何东西。
      </div>
    </div>
  </section>
</template>
