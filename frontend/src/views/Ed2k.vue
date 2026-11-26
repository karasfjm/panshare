<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, ago, fmtSize } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { Account, Ed2kTask } from '../lib/types'
import DirPicker from '../components/DirPicker.vue'
import LocalDir from '../components/LocalDir.vue'

interface TaskDefaults {
  account_id?: string
  dir_cid?: string
  dir_name?: string
  local_dir?: string
}

const accounts = ref<Account[]>([])
const tasks = ref<Ed2kTask[]>([])
const loading = ref(false)
const submitting = ref(false)

const form = reactive({
  urls: '',
  account_id: '',
  dir_cid: '',
  dir_name: '',
  local_dir: '',
  transfer_on_add: false,
})

async function load() {
  loading.value = true
  try {
    const [t, a, d] = await Promise.all([
      api.get<{ tasks: Ed2kTask[] }>('/ed2k'),
      api.get<{ accounts: Account[] }>('/accounts'),
      api.get<{ ed2k: TaskDefaults }>('/tasks/defaults'),
    ])
    tasks.value = t.tasks || []
    accounts.value = a.accounts || []
    applyDefaults(d.ed2k || {})
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

// 上次选的账号 / 转存目录 / 本地目录固定沿用
function applyDefaults(d: TaskDefaults) {
  const ids = accounts.value.map((x) => x.id)
  form.account_id = d.account_id && ids.includes(d.account_id) ? d.account_id : accounts.value[0]?.id || ''
  form.dir_cid = d.dir_cid || ''
  form.dir_name = d.dir_name || ''
  form.local_dir = d.local_dir || ''
}

async function remember() {
  try {
    await api.post('/tasks/defaults', {
      kind: 'ed2k',
      account_id: form.account_id,
      dir_cid: form.dir_cid,
      dir_name: form.dir_name,
      local_dir: form.local_dir,
    })
  } catch {
    /* 记不住不影响主流程 */
  }
}
onMounted(load)

async function submit() {
  if (!form.urls.trim()) {
    toastError(new Error('请粘贴 ed2k 链接（一行一条）'))
    return
  }
  submitting.value = true
  try {
    await remember()
    const res = await api.post<{ count: number }>('/ed2k', { ...form })
    toast(`已添加 ${res.count} 条任务`)
    form.urls = ''
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    submitting.value = false
  }
}

async function remove(t: Ed2kTask, cloud: boolean) {
  if (!window.confirm(`删除「${t.name}」？${cloud ? '\n会同时删除 115 上的文件。' : ''}`)) return
  try {
    await api.del(`/ed2k/${t.id}?delete_cloud=${cloud ? '1' : '0'}&delete_local=1`)
    toast('已删除')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function onImport(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const res = await api.post<{ count: number }>('/ed2k/import', {
      data: text,
      account_id: form.account_id,
      dir_cid: form.dir_cid,
      local_dir: form.local_dir,
    })
    toast(`已导入 ${res.count} 条任务`)
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    input.value = ''
  }
}

function exportAll() {
  window.open('/api/ed2k/export', '_blank')
}

function stateClass(s: string) {
  return s === 'done' ? 'ok' : s === 'failed' ? 'err' : s === 'transferring' ? 'brand' : ''
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>添加 ed2k</h3>
        <div class="desc">
          添加时立刻生成 strm；默认到播放时才转存到 115，也可以改成添加时就转存。
        </div>
      </div>
      <div class="actions">
        <button class="btn" @click="exportAll">导出</button>
        <label class="btn" style="margin: 0">
          导入
          <input type="file" accept=".json,.txt" style="display: none" @change="onImport" />
        </label>
      </div>
    </div>

    <label class="field">
      <span>ed2k 链接</span>
      <textarea v-model="form.urls" placeholder="ed2k://|file|名字.mkv|123456789|ABCDEF...|/&#10;一行一条，支持一次贴多条" />
    </label>

    <div class="grid c2">
      <label class="field">
        <span>115 账号</span>
        <select v-model="form.account_id">
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
      <label class="field">
        <span>转存目录（115 上的接收目录）</span>
        <div class="inline">
          <input :value="form.dir_name || (form.dir_cid ? form.dir_cid : '默认（ed2k）')" readonly />
          <DirPicker
            :account-id="form.account_id"
            :model-value="form.dir_cid"
            @update:model-value="(v: string) => (form.dir_cid = v)"
            @pick="(d) => (form.dir_name = d.name)"
          />
        </div>
      </label>
      <label class="field">
        <span>本地 STRM 目录</span>
        <div class="inline">
          <input v-model="form.local_dir" class="mono" placeholder="留空则 ed2k/年月" />
          <LocalDir v-model="form.local_dir" sm />
        </div>
        <div class="tip">点「选」直接挑目录；选过一次就会记住</div>
      </label>
      <label class="field">
        <span>转存时机</span>
        <label class="switch" style="margin-top: 6px">
          <input type="checkbox" v-model="form.transfer_on_add" /><span class="track"></span>
          <span class="label">{{ form.transfer_on_add ? '添加时就转存' : '播放时才转存' }}</span>
        </label>
      </label>
    </div>

    <button class="btn primary" :disabled="submitting" @click="submit">{{ submitting ? '提交中…' : '添加' }}</button>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>任务</h3>
        <div class="desc">共 {{ tasks.length }} 条 · 导出的 JSON 可以在换设备后直接导入恢复</div>
      </div>
      <div class="actions"><button class="btn sm" @click="load">刷新</button></div>
    </div>

    <div v-if="loading && !tasks.length" class="empty">加载中…</div>
    <div v-else-if="!tasks.length" class="empty">还没有 ed2k 任务</div>
    <table v-else class="table">
      <thead>
        <tr><th>文件</th><th>大小</th><th>状态</th><th>账号 / 目录</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.id">
          <td>
            <div class="name wrap-any">{{ t.name }}</div>
            <div class="sub mono">{{ t.id }}</div>
          </td>
          <td class="num">{{ fmtSize(t.size) }}</td>
          <td>
            <span class="badge" :class="stateClass(t.state)">{{ t.state }}</span>
            <div class="sub">{{ t.message }}</div>
          </td>
          <td>
            <div>{{ t.account_name || t.account_id }}</div>
            <div class="sub">{{ t.local_dir }} · {{ ago(t.created_at) }}</div>
          </td>
          <td class="nowrap">
            <button class="btn sm" @click="remove(t, false)">删任务</button>
            <button class="btn sm danger" @click="remove(t, true)">连云端</button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
