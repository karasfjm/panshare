<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, fmtTime } from '../lib/api'
import { toastError } from '../lib/toast'
import type { Stats } from '../lib/types'

const stats = ref<Stats | null>(null)
let timer: number | undefined

const levelClass = computed(() => (level: string) => {
  if (level === 'error') return 'err'
  if (level === 'warn') return 'warn'
  if (level === 'success') return 'ok'
  return ''
})

async function load() {
  try {
    stats.value = await api.get<Stats>('/stats')
  } catch (e) {
    toastError(e)
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 8000)
})
onUnmounted(() => timer && window.clearInterval(timer))
</script>

<template>
  <div v-if="stats">
    <div class="grid c4 mb16">
      <div class="stat">
        <div class="k">115 账号</div>
        <div class="v">{{ stats.accounts_ok }}<small>/ {{ stats.accounts }}</small></div>
      </div>
      <div class="stat">
        <div class="k">生成配置</div>
        <div class="v">{{ stats.configs }}<small v-if="stats.configs_running">· {{ stats.configs_running }} 运行中</small></div>
      </div>
      <div class="stat">
        <div class="k">索引文件</div>
        <div class="v">{{ stats.files }}<small>个 strm</small></div>
      </div>
      <div class="stat">
        <div class="k">运行时长</div>
        <div class="v" style="font-size: 19px">{{ stats.uptime }}</div>
      </div>
    </div>

    <div class="grid c2">
      <section class="card">
        <div class="card-head">
          <div class="h">
            <h3>运行参数</h3>
            <div class="desc">改这些去「服务设置」</div>
          </div>
        </div>
        <div class="kv">
          <div class="k">映射根</div>
          <div class="mono wrap-any">{{ stats.strm_root }}</div>
          <div class="k">请求头</div>
          <div class="mono wrap-any">{{ stats.base_url }}</div>
          <div class="k">Emby 服务器</div>
          <div>
            <span class="badge" :class="stats.emby_servers ? 'ok' : ''">{{ stats.emby_servers || 0 }} 台</span>
            <span class="muted small" v-if="!stats.emby_servers"> 未添加</span>
          </div>
          <div class="k">Telegram</div>
          <div>
            <span class="badge" :class="stats.settings.tg_enabled ? 'ok' : ''">{{ stats.settings.tg_enabled ? '已开启' : '未开启' }}</span>
            <span class="muted small" v-if="stats.settings.tg_commands"> · 指令已开</span>
          </div>
          <div class="k">联动删除</div>
          <div>
            <span class="badge" :class="stats.settings.delete_enabled ? 'ok' : ''">{{ stats.settings.delete_enabled ? '已开启' : '未开启' }}</span>
            <span class="muted small" v-if="stats.settings.delete_enabled"> · 宽限 {{ stats.settings.delete_grace_minutes }} 分钟</span>
          </div>
          <div class="k">版本</div>
          <div class="mono">{{ stats.version }}</div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <div class="h">
            <h3>最近的动静</h3>
            <div class="desc">任务开始 / 完成 / 失败都在这里</div>
          </div>
          <div class="actions">
            <router-link class="btn sm" to="/notify">推送设置</router-link>
          </div>
        </div>
        <div v-if="!stats.events.length" class="empty">还没有事件</div>
        <div class="list" v-else>
          <div v-for="e in stats.events" :key="e.id" class="row-item">
            <span class="badge" :class="levelClass(e.level)">{{ e.kind }}</span>
            <div class="body">
              <div class="title" style="font-weight: 500">{{ e.message }}</div>
              <div class="meta">{{ fmtTime(e.created_at) }}</div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
  <div v-else class="empty">加载中…</div>
</template>
