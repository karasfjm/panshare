<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, ago } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { Account, ShareTask } from '../lib/types'
import DirPicker from '../components/DirPicker.vue'
import LocalDir from '../components/LocalDir.vue'

interface TaskDefaults {
  account_id?: string
  dir_cid?: string
  dir_name?: string
  local_dir?: string
}

const accounts = ref<Account[]>([])
const shares = ref<ShareTask[]>([])
const loading = ref(false)
const submitting = ref(false)

const form = reactive({
  url: '',
  account_id: '',
  dir_cid: '',
  dir_name: '',
  local_dir: '',
  name: '',
  no_transfer: false,
})

async function load() {
  loading.value = true
  try {
    const [s, a, d] = await Promise.all([
      api.get<{ shares: ShareTask[] }>('/shares'),
      api.get<{ accounts: Account[] }>('/accounts'),
      api.get<{ share: TaskDefaults }>('/tasks/defaults'),
    ])
    shares.value = s.shares || []
    accounts.value = a.accounts || []
    applyDefaults(d.share || {})
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

// 上次选的账号 / 接收目录 / 本地目录一直沿用，不用每次重选
function applyDefaults(d: TaskDefaults) {
  const ids = accounts.value.map((x) => x.id)
  form.account_id = d.account_id && ids.includes(d.account_id) ? d.account_id : accounts.value[0]?.id || ''
  form.dir_cid = d.dir_cid || ''
  form.dir_name = d.dir_name || ''
  form.local_dir = d.local_dir || ''
}

// 提交成功后把这三项记到后端，下次打开还记得
async function remember() {
  try {
    await api.post('/tasks/defaults', {
      kind: 'share',
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
  if (!form.url.trim()) {
    toastError(new Error('请粘贴 115 分享链接'))
    return
  }
  submitting.value = true
  try {
    await remember()
    await api.post('/shares', { ...form })
    toast('已开始处理，进度看下面的任务')
    form.url = ''
    form.name = ''
    setTimeout(load, 800)
  } catch (e) {
    toastError(e)
  } finally {
    submitting.value = false
  }
}

async function remove(t: ShareTask, mode: 'local' | 'cloud' | 'all') {
  const msg =
    mode === 'all'
      ? `删除任务「${t.title}」？\n会同时取消自己的分享、删除本地 strm。`
      : `删除任务「${t.title}」？`
  if (!window.confirm(msg)) return
  try {
    await api.del(
      `/shares/${t.id}?cancel_share=${mode === 'all' ? '1' : '0'}&delete_cloud=${mode === 'all' ? '1' : '0'}&delete_local=1`,
    )
    toast('已删除')
    await load()
  } catch (e) {
    toastError(e)
  }
}

function statusClass(s: string) {
  if (s === 'done') return 'ok'
  if (s === 'error') return 'err'
  if (s === 'running') return 'brand'
  return ''
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>新的转存</h3>
        <div class="desc">
          粘贴分享链接 → 转存到接收目录 → 用自己的文件建长期分享 → strm 指向自己的分享。
        </div>
      </div>
    </div>

    <label class="field">
      <span>115 分享链接</span>
      <input v-model="form.url" placeholder="https://115.com/s/swXXXX?password=abcd 或 swXXXX-abcd" />
    </label>

    <div class="grid c2">
      <label class="field">
        <span>接收账号</span>
        <select v-model="form.account_id">
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
      <label class="field">
        <span>接收目录</span>
        <div class="inline">
          <input :value="form.dir_name || (form.dir_cid ? form.dir_cid : '默认（分享接收）')" readonly />
          <DirPicker
            :account-id="form.account_id"
            :model-value="form.dir_cid"
            @update:model-value="(v: string) => (form.dir_cid = v)"
            @pick="(d) => (form.dir_name = d.name)"
          />
        </div>
        <div class="tip">留空则用服务设置里的接收目录，没有就自动建「分享接收」</div>
      </label>
      <label class="field">
        <span>本地 STRM 目录</span>
        <div class="inline">
          <input v-model="form.local_dir" class="mono" placeholder="留空则 分享/<分享标题>" />
          <LocalDir v-model="form.local_dir" sm />
        </div>
        <div class="tip">点「选」直接挑目录；选过一次就会记住，不用每次填</div>
      </label>
      <label class="field">
        <span>自定义标题（可空）</span>
        <input v-model="form.name" placeholder="默认取分享标题" />
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">不转存，直接生成</div>
        <div class="d">用于自己已有的分享：不占用网盘空间，strm 直接指向源分享，播放时从分享通道解析直链</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="form.no_transfer" /><span class="track"></span>
      </label>
    </div>

    <div class="row mt16">
      <button class="btn primary" :disabled="submitting" @click="submit">{{ submitting ? '提交中…' : '开始转存' }}</button>
      <span class="muted small">大分享遍历会比较久，页面可以关掉，任务在后台跑</span>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>任务</h3>
        <div class="desc">共 {{ shares.length }} 条</div>
      </div>
      <div class="actions">
        <button class="btn sm" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="loading && !shares.length" class="empty">加载中…</div>
    <div v-else-if="!shares.length" class="empty">还没有转存任务</div>
    <div v-else class="list">
      <div v-for="t in shares" :key="t.id" class="row-item">
        <div class="body">
          <div class="title">
            {{ t.title || t.share_code }}
            <span class="badge" :class="statusClass(t.status)">{{ t.status }}</span>
            <span class="badge" v-if="t.no_transfer">未转存</span>
            <span class="badge">{{ t.files }} 个文件</span>
          </div>
          <div class="meta">
            {{ t.account_name || t.account_id }} · 本地 <span class="mono">{{ t.local_dir }}</span> · {{ ago(t.created_at) }}
          </div>
          <div class="meta" v-if="t.my_share_code">
            自己的分享：
            <a :href="`https://115.com/s/${t.my_share_code}`" target="_blank" rel="noreferrer">
              {{ t.my_share_code }}
            </a>
            <span v-if="t.my_receive_code"> · 提取码 {{ t.my_receive_code }}</span>
          </div>
          <div class="meta" v-if="t.message" :style="t.status === 'error' ? 'color:var(--err)' : ''">{{ t.message }}</div>
        </div>
        <div class="actions">
          <button class="btn sm danger" @click="remove(t, 'local')">删任务</button>
          <button class="btn sm danger" @click="remove(t, 'all')">全删</button>
        </div>
      </div>
    </div>
  </section>
</template>
