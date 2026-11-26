<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import { loadSettings, patchSettings } from '../lib/settings'
import type { Deletion, Settings } from '../lib/types'

const s = ref<Settings | null>(null)
const deletions = ref<Deletion[]>([])
const saving = ref(false)
const scanning = ref(false)

async function loadDeletions() {
  try {
    const res = await api.get<{ deletions: Deletion[] }>('/link/deletions')
    deletions.value = res.deletions || []
  } catch (e) {
    toastError(e)
  }
}

onMounted(async () => {
  try {
    s.value = await loadSettings()
  } catch (e) {
    toastError(e)
  }
  await loadDeletions()
})

async function save() {
  if (!s.value) return
  saving.value = true
  try {
    s.value = await patchSettings({
      delete_enabled: s.value.delete_enabled,
      delete_grace_minutes: s.value.delete_grace_minutes,
      delete_cloud_when_local_missing: s.value.delete_cloud_when_local_missing,
      delete_local_when_cloud_deleted: s.value.delete_local_when_cloud_deleted,
      delete_meta_with_strm: s.value.delete_meta_with_strm,
    })
    toast('已保存')
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function scan() {
  scanning.value = true
  try {
    await api.post('/link/scan')
    toast('已开始对账，稍后刷新看结果')
    setTimeout(loadDeletions, 2000)
  } catch (e) {
    toastError(e)
  } finally {
    scanning.value = false
  }
}

async function confirm(d: Deletion) {
  if (!window.confirm(`立即删除云端文件？\n${d.path}`)) return
  try {
    await api.post(`/link/deletions/${d.id}/confirm`)
    toast('已删除云端文件')
    await loadDeletions()
  } catch (e) {
    toastError(e)
  }
}

async function cancel(d: Deletion) {
  try {
    await api.del(`/link/deletions/${d.id}`)
    toast('已撤销')
    await loadDeletions()
  } catch (e) {
    toastError(e)
  }
}
</script>

<template>
  <section class="card" v-if="s">
    <div class="card-head">
      <div class="h">
        <h3>双向联动</h3>
        <div class="desc">本地 strm 消失（超过宽限期）→ 删 115 文件；115 上删除 → 清本地 strm 与同名元数据。</div>
      </div>
      <div class="actions">
        <button class="btn" :disabled="scanning" @click="scan">{{ scanning ? '对账中…' : '立即对账' }}</button>
      </div>
    </div>

    <div class="switch-row">
      <div class="txt"><div class="t">联动删除总开关</div><div class="d">关掉后任何方向都不会自动删</div></div>
      <label class="switch">
        <input type="checkbox" v-model="s.delete_enabled" /><span class="track"></span>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">本地 strm 消失 → 删云端文件</div>
        <div class="d">宽限期内把 strm 放回去就不会删</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.delete_cloud_when_local_missing" :disabled="!s.delete_enabled" /><span class="track"></span>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">云端删除 → 清本地 strm</div>
        <div class="d">顺带清掉同名 nfo / 海报 / 字幕、影片-poster.jpg</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.delete_local_when_cloud_deleted" :disabled="!s.delete_enabled" /><span class="track"></span>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">删 strm 时一并删元数据</div>
        <div class="d">nfo / srt / ass / poster 等，按「同名 + 元数据扩展名」匹配</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.delete_meta_with_strm" :disabled="!s.delete_enabled" /><span class="track"></span>
      </label>
    </div>

    <label class="field mt12" style="max-width: 260px">
      <span>宽限期（分钟）</span>
      <input v-model.number="s.delete_grace_minutes" type="number" min="0" :disabled="!s.delete_enabled" />
      <div class="tip">0 = 立即删。磁盘没挂载时建议设 30 分钟以上</div>
    </label>

    <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>待办 / 记录</h3>
        <div class="desc">宽限期内可以撤销；已完成的记录留在这里备查</div>
      </div>
      <div class="actions"><button class="btn sm" @click="loadDeletions">刷新</button></div>
    </div>

    <div v-if="!deletions.length" class="empty">还没有联动删除记录</div>
    <table v-else class="table">
      <thead>
        <tr><th>方向</th><th>路径</th><th>状态</th><th>时间</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="d in deletions" :key="d.id">
          <td class="nowrap">{{ d.kind }}</td>
          <td class="wrap-any">{{ d.path }}</td>
          <td>
            <span class="badge" :class="d.state === 'done' ? 'ok' : d.state === 'error' ? 'err' : 'warn'">{{ d.state }}</span>
            <div class="sub" v-if="d.message">{{ d.message }}</div>
          </td>
          <td class="small muted nowrap">{{ fmtTime(d.created_at) }}</td>
          <td class="nowrap">
            <template v-if="d.state === 'pending'">
              <button class="btn sm" @click="confirm(d)">立即删</button>
              <button class="btn sm" @click="cancel(d)">撤销</button>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
