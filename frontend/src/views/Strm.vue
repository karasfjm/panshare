<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, ago, fmtSize } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { Account, CleanupConfirmation, StrmConfig } from '../lib/types'
import AppModal from '../components/AppModal.vue'
import DirPicker from '../components/DirPicker.vue'
import LocalDir from '../components/LocalDir.vue'

interface PathRow {
  remote_id: string
  remote_path: string
  local_dir: string
}

const configs = ref<StrmConfig[]>([])
const accounts = ref<Account[]>([])
const strmRoot = ref('/strm')
const baseURL = ref('')
const defaultExts = ref('')
const defaultMetaExts = ref('')
const cleanups = ref<CleanupConfirmation[]>([])
const loading = ref(false)
let timer: number | undefined

const editorOpen = ref(false)
const editing = ref<StrmConfig | null>(null)
const form = reactive({
  name: '',
  account_id: '',
  paths: [] as PathRow[],
  include_exts: '',
  meta_exts: '',
  strm_base: '',
  all_files: false,
  overwrite_mode: 'always' as 'always' | 'never',
  exclude_keywords: '',
  min_file_size_mb: 0,
  skip_pending: false,
  pending_dirs: '待整理,待归类',
  concurrency: 8,
  cron_full: '',
  watch_enabled: false,
  watch_delay_sec: 30,
  watch_interval: 60,
  sched_enabled: false,
  sched_minutes: 60,
  download_meta: false,
})
const saving = ref(false)

// 预览用的样例文件：编辑时从已有索引里取一条，新建时用占位符
const sample = ref<{ pickcode: string; name: string } | null>(null)

const filesOpen = ref(false)
const filesTitle = ref('')
const files = ref<any[]>([])

const hasAccounts = computed(() => accounts.value.length > 0)
const uidOf = computed(() => accounts.value.find((a) => a.id === form.account_id)?.uid || '')

const localRootAbs = computed(() => strmRoot.value.replace(/\/+$/, ''))

// 表单里本地目录一律显示成「映射根 + 相对路径」，跟页面上其他地方看到的绝对路径一致
function absLocal(v: string): string {
  const root = localRootAbs.value
  let s = (v || '').replace(/\\/g, '/').trim()
  if (!s) return ''
  if (s === root || s.startsWith(root + '/')) return s
  return root + '/' + s.replace(/^\/+/, '')
}

function relLocal(v: string): string {
  const root = localRootAbs.value
  let s = (v || '').replace(/\\/g, '/').trim()
  if (root && s === root) return ''
  if (root && s.startsWith(root + '/')) s = s.slice(root.length + 1)
  return s.replace(/^\/+/, '')
}

// STRM 内容预览：完整展示一条 strm 里到底写了什么
const previewURL = computed(() => {
  const base = (form.strm_base || baseURL.value || '').replace(/\/+$/, '')
  const pick = sample.value?.pickcode || 'pickcode'
  const name = sample.value?.name || '示例影片.mkv'
  const uid = uidOf.value || '115用户ID'
  return `${base}/p115/play/${pick}/${encodeURIComponent(name)}/${uid}`
})

async function load() {
  loading.value = true
  try {
    const [c, a, s, q] = await Promise.all([
      api.get<{ configs: StrmConfig[] }>('/strm/configs'),
      api.get<{ accounts: Account[] }>('/accounts'),
      api.get<{
        settings: { default_exts?: string; default_meta_exts?: string; public_base_url?: string }
        derived: { strm_root: string; base_url: string }
      }>('/settings'),
      api.get<{ cleanups: CleanupConfirmation[] }>('/strm/cleanups'),
    ])
    cleanups.value = q.cleanups || []
    configs.value = c.configs || []
    accounts.value = a.accounts || []
    strmRoot.value = s.derived.strm_root || '/strm'
    baseURL.value = s.derived.base_url || ''
    defaultExts.value = s.settings.default_exts || ''
    defaultMetaExts.value = s.settings.default_meta_exts || ''
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(() => load(), 10000)
})
onUnmounted(() => timer && window.clearInterval(timer))

// 扩展名统一显示成「不带点、逗号分隔」，跟表单里的写法一致
function extList(raw: string): string {
  return (raw || '')
    .split(/[,;\s]+/)
    .map((x) => x.trim().replace(/^\./, ''))
    .filter(Boolean)
    .join(',')
}

function newRow(): PathRow {
  return { remote_id: '0', remote_path: '', local_dir: '' }
}

function create() {
  if (!hasAccounts.value) {
    toastError(new Error('先去「115 助手」添加账号'))
    return
  }
  editing.value = null
  sample.value = null
  Object.assign(form, {
    name: '',
    account_id: accounts.value[0]?.id || '',
    paths: [newRow()],
    include_exts: extList(defaultExts.value),
    meta_exts: extList(defaultMetaExts.value),
    strm_base: '',
    all_files: false,
    overwrite_mode: 'always',
    exclude_keywords: '',
    min_file_size_mb: 0,
    skip_pending: false,
    pending_dirs: '待整理,待归类',
    concurrency: 8,
    cron_full: '',
    watch_enabled: true,
    watch_delay_sec: 30,
    watch_interval: 60,
    sched_enabled: false,
    sched_minutes: 60,
    download_meta: true,
  })
  editorOpen.value = true
}

async function edit(c: StrmConfig) {
  editing.value = c
  const rows = (c.paths && c.paths.length ? c.paths : [{ remote_id: c.remote_id, remote_path: c.remote_path, local_dir: c.local_dir }]).map(
    (p) => ({
      remote_id: p.remote_id || '0',
      remote_path: p.remote_path || '',
      local_dir: absLocal(p.local_dir),
    }),
  )
  Object.assign(form, {
    name: c.name,
    account_id: c.account_id,
    paths: rows.length ? rows : [newRow()],
    include_exts: c.include_exts || extList(defaultExts.value),
    meta_exts: (c as any).meta_exts || extList(defaultMetaExts.value),
    strm_base: (c as any).strm_base || '',
    all_files: c.all_files,
    overwrite_mode: c.overwrite_mode || 'always',
    exclude_keywords: c.exclude_keywords || '',
    min_file_size_mb: c.min_file_size_mb || 0,
    skip_pending: !!c.skip_pending,
    pending_dirs: c.pending_dirs || '待整理,待归类',
    concurrency: c.concurrency || 8,
    cron_full: c.cron_full || '',
    watch_enabled: c.watch_enabled,
    watch_delay_sec: c.watch_delay_sec || 30,
    watch_interval: c.watch_interval || 60,
    sched_enabled: c.sched_enabled,
    sched_minutes: c.sched_minutes || 60,
    download_meta: c.download_meta,
  })
  sample.value = null
  editorOpen.value = true
  // 拿一条已有文件当预览样例，让用户看到真实的 strm 内容
  try {
    const res = await api.get<{ files: any[] }>(`/strm/configs/${c.id}/files?limit=1`)
    const f = res.files?.[0]
    if (f?.pickcode) sample.value = { pickcode: f.pickcode, name: f.name }
  } catch {
    /* 没有文件就继续用占位符 */
  }
}

function addRow() {
  form.paths.push(newRow())
}

function removeRow(i: number) {
  if (form.paths.length <= 1) {
    toastError(new Error('至少要留一条网盘路径'))
    return
  }
  form.paths.splice(i, 1)
}

function onPickDir(i: number, d: { id: string; name: string; path: string }) {
  const row = form.paths[i]
  row.remote_id = d.id
  row.remote_path = d.path || '/' + d.name
  // 本地目录按「映射根 + 网盘目录名」预填，用户还可以自己改或点「选」
  if (!row.local_dir) row.local_dir = absLocal(d.name)
}

async function save() {
  const name = form.name.trim()
  if (!name) {
    toastError(new Error('任务名称要填'))
    return
  }
  const paths = form.paths
    .map((p) => ({
      remote_id: p.remote_id || '0',
      remote_path: p.remote_path || '/',
      local_dir: relLocal(p.local_dir),
    }))
    .filter((p) => p.local_dir)
  if (!paths.length) {
    toastError(new Error('每条网盘路径都要选一个本地目录'))
    return
  }
  saving.value = true
  try {
    const payload = {
      name,
      account_id: form.account_id,
      paths,
      include_exts: form.include_exts,
      meta_exts: form.meta_exts,
      strm_base: form.strm_base,
      all_files: form.all_files,
      overwrite_mode: form.overwrite_mode,
      exclude_keywords: form.exclude_keywords,
      min_file_size_mb: form.min_file_size_mb,
      skip_pending: form.skip_pending,
      pending_dirs: form.pending_dirs,
      concurrency: form.concurrency,
      cron_full: form.cron_full,
      watch_enabled: form.watch_enabled,
      watch_delay_sec: form.watch_delay_sec,
      watch_interval: form.watch_interval,
      sched_enabled: form.sched_enabled,
      sched_minutes: form.sched_minutes,
      download_meta: form.download_meta,
    }
    if (editing.value) {
      await api.patch(`/strm/configs/${editing.value.id}`, payload)
      toast('已保存')
    } else {
      await api.post('/strm/configs', payload)
      toast('已添加，可以点「增量」开始生成')
    }
    editorOpen.value = false
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function run(c: StrmConfig, mode: 'inc' | 'full' | 'rebuild') {
  try {
    await api.post(`/strm/configs/${c.id}/run?mode=${mode}`)
    toast(mode === 'inc' ? '已开始增量生成' : mode === 'full' ? '已开始全量生成' : '已开始全新生成')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function remove(c: StrmConfig) {
  if (!window.confirm(`删除配置「${c.name}」？\n这条配置生成过的 strm 会被删掉（不碰别人的）。`)) return
  try {
    await api.del(`/strm/configs/${c.id}`)
    toast('已删除')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function approveCleanup(x: CleanupConfirmation) {
  if (!window.confirm(`确认删除「${x.config_name}」的 ${x.total} 个残留 strm？`)) return
  try {
    const res = await api.post<{ removed: number }>(`/strm/cleanups/${x.id}/approve`)
    toast(`已移除 ${res.removed} 个残留`)
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function cancelCleanup(x: CleanupConfirmation) {
  try {
    await api.del(`/strm/cleanups/${x.id}`)
    toast('已取消清理计划')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function showFiles(c: StrmConfig) {
  filesTitle.value = c.name
  files.value = []
  filesOpen.value = true
  try {
    const res = await api.get<{ files: any[] }>(`/strm/configs/${c.id}/files?limit=300`)
    files.value = res.files || []
  } catch (e) {
    toastError(e)
  }
}

function copyUrl(f: any) {
  const acc = accounts.value.find((a) => a.id === f.account_id)
  if (!f.pickcode) {
    toastError(new Error('这条索引没有 pickcode'))
    return
  }
  const u = `${location.origin}/p115/play/${f.pickcode}/${encodeURIComponent(f.name)}/${encodeURIComponent(acc?.uid || '')}`
  navigator.clipboard?.writeText(u)
  toast('已复制播放地址')
}

function pathsOf(c: StrmConfig): PathRow[] {
  if (c.paths && c.paths.length) return c.paths as unknown as PathRow[]
  return [{ remote_id: c.remote_id, remote_path: c.remote_path, local_dir: c.local_dir }]
}
</script>

<template>
  <section class="card" v-if="cleanups.length">
    <div class="card-head">
      <div class="h">
        <h3>待确认清理</h3>
        <div class="desc">全量生成发现云端已删的残留，已通过稳定性与比例校验，等待人工确认</div>
      </div>
    </div>
    <div class="list">
      <div v-for="x in cleanups" :key="x.id" class="row-item">
        <div class="body">
          <div class="title">
            {{ x.config_name }}
            <span class="badge">{{ x.total }} 个残留</span>
            <span class="badge" v-if="x.ratio">阈值 {{ x.ratio }}%</span>
          </div>
          <div class="meta">过期时间 {{ ago(x.expires_at) }}</div>
          <div class="meta">{{ x.items.slice(0, 5).map((i) => i.name).join('、') }}{{ x.total > 5 ? ' …' : '' }}</div>
        </div>
        <div class="actions">
          <button class="btn sm primary" @click="approveCleanup(x)">确认删除</button>
          <button class="btn sm" @click="cancelCleanup(x)">取消计划</button>
        </div>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>生成配置</h3>
        <div class="desc">
          每条配置可以挂多条「网盘路径 → 本地目录」，strm 内容 = 请求头 + pickcode + 名字 + 115 用户 ID。
          映射根 <code>{{ strmRoot }}</code>
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" @click="create">添加配置</button>
      </div>
    </div>

    <div v-if="loading && !configs.length" class="empty">加载中…</div>
    <div v-else-if="!configs.length" class="empty">还没有配置。添加一条：选账号 → 选网盘目录 → 选本地目录</div>
    <div v-else class="list">
      <div v-for="c in configs" :key="c.id" class="row-item">
        <div class="body">
          <div class="title">
            {{ c.name }}
            <span class="badge" :class="c.running ? 'brand' : c.last_status === 'error' ? 'err' : 'ok'">
              {{ c.running ? '生成中' : c.last_status === 'error' ? '失败' : c.last_status === 'warn' ? '有告警' : '空闲' }}
            </span>
            <span class="badge">{{ c.files }} 个文件</span>
            <span v-if="c.watch_enabled" class="badge ok" :title="c.last_check_at ? '上次探测：' + ago(c.last_check_at) : '尚未探测'">
              事件监控{{ c.last_check_at ? ' · ' + ago(c.last_check_at) : '' }}
            </span>
            <span v-if="c.sched_enabled" class="badge">定时 {{ c.sched_minutes }} 分钟</span>
            <span v-if="c.cron_full" class="badge">Cron {{ c.cron_full }}</span>
            <span v-if="c.overwrite_mode === 'never'" class="badge">不覆盖</span>
            <span v-if="c.cleanup_stale_count" class="badge">残留稳定 {{ c.cleanup_stale_count }}</span>
            <span v-if="c.download_meta" class="badge">下载元数据</span>
          </div>
          <div class="meta">
            {{ c.account_name || c.account_id }} ·
            <template v-for="(p, i) in pathsOf(c)" :key="i">
              <span v-if="i" class="muted"> · </span>
              网盘 <span class="mono">{{ p.remote_path || '根目录' }}</span>
              → <span class="mono">{{ absLocal(p.local_dir) }}</span>
            </template>
          </div>
          <div class="meta">上次 {{ ago(c.last_run_at) }}</div>
          <div class="meta" v-if="c.tg_alias">
            TG 指令 <span class="mono">/inc_{{ c.tg_alias }}</span> 增量 ·
            <span class="mono">/full_{{ c.tg_alias }}</span> 全量
          </div>
          <div class="meta" v-if="c.last_message">{{ c.last_message }}</div>
        </div>
        <div class="actions">
          <button class="btn sm primary" @click="run(c, 'inc')">增量</button>
          <button class="btn sm" @click="run(c, 'full')">全量</button>
          <button class="btn sm" @click="run(c, 'rebuild')">全新</button>
          <button class="btn sm" @click="showFiles(c)">文件</button>
          <button class="btn sm" @click="edit(c)">编辑</button>
          <button class="btn sm danger" @click="remove(c)">删除</button>
        </div>
      </div>
    </div>

    <div class="notice mt16">
      <div>
        <strong>增量</strong>只补新增；<strong>全量</strong>重扫并重写内容有变化的 strm，清理残留前要做稳定性、比例和人工确认校验；
        <strong>全新</strong>先删掉这条配置生成过的旧 strm（只删自己的）再从头生成。
        事件监控监听 115 的目录变化，来了新文件就立刻生成，不用整棵树反复扫。
      </div>
    </div>
  </section>

  <AppModal
    v-if="editorOpen"
    :title="editing ? `配置 · ${editing.name}` : '添加配置'"
    wide
    @close="editorOpen = false"
  >
    <div class="grid c2">
      <label class="field">
        <span>账号（来自 115 助手）</span>
        <select v-model="form.account_id">
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
      <label class="field">
        <span>任务名称</span>
        <input v-model="form.name" placeholder="例如：国漫 / 电影" />
      </label>
    </div>

    <div class="field">
      <span>网盘路径 → 本地 STRM 目录</span>
      <div v-for="(p, i) in form.paths" :key="i" class="path-row">
        <DirPicker
          :account-id="form.account_id"
          :model-value="p.remote_id"
          label="📁"
          sm
          @pick="(d: any) => onPickDir(i, d)"
        />
        <input class="mono path-remote" :value="p.remote_path || '根目录'" readonly title="点左边的文件夹选网盘目录" />
        <span class="arrow">→</span>
        <input v-model="p.local_dir" class="mono path-local" placeholder="本地 STRM 目录" />
        <LocalDir v-model="p.local_dir" sm />
        <button class="btn ghost sm" type="button" title="删掉这条" @click="removeRow(i)">✕</button>
      </div>
      <div class="row mt8">
        <button class="btn sm" type="button" @click="addRow">＋ 选择网盘目录</button>
      </div>
      <div class="tip">
        本地目录默认按「映射根 + 网盘目录名」预填（网盘 /影音/国漫 → {{ strmRoot }}/国漫）；点行里的「选」可以改成映射根下的任意目录，也可以直接改。
      </div>
    </div>

    <label class="field">
      <span>STRM 请求头</span>
      <input v-model="form.strm_base" class="mono" :placeholder="baseURL || 'http://172.17.0.1:9527'" />
      <div class="tip">strm 内容的基地址，客户端必须能访问到；留空则用全局设置（{{ baseURL || '未设置' }}）</div>
    </label>

    <div class="field">
      <span>STRM 内容预览</span>
      <div class="strm-preview mono">{{ previewURL }}</div>
      <div class="tip">规则：请求头 + pickcode + name（URL 编码）+ 115 用户 ID（标识该 strm 属于哪个账号）</div>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>媒体文件扩展名</span>
        <input v-model="form.include_exts" class="mono" placeholder="mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts" />
        <div class="tip">命中这些后缀才生成 strm，英文逗号分隔</div>
      </label>
      <label class="field">
        <span>元数据文件扩展名</span>
        <input v-model="form.meta_exts" class="mono" placeholder="nfo,jpg,jpeg,png,srt,ass,sub,idx" />
        <div class="tip">nfo / 图片 / 字幕等，可下载到本地</div>
      </label>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>覆盖模式</span>
        <select v-model="form.overwrite_mode">
          <option value="always">始终重算（内容相同不写盘）</option>
          <option value="never">已有 strm 直接跳过</option>
        </select>
        <div class="tip">参考 QBP115StrmHelper：never 时目标文件存在就不动，也不更新索引</div>
      </label>
      <label class="field">
        <span>并发写线程</span>
        <input v-model.number="form.concurrency" type="number" min="1" max="32" />
        <div class="tip">列目录仍串行（115 限速），这里只控制写 strm / 下元数据的并发，0 用全局默认</div>
      </label>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>黑名单关键词</span>
        <input v-model="form.exclude_keywords" placeholder="花絮,预告,sample" />
        <div class="tip">文件名包含任一关键词就跳过，英文逗号分隔</div>
      </label>
      <label class="field">
        <span>最小文件大小（MB）</span>
        <input v-model.number="form.min_file_size_mb" type="number" min="0" />
        <div class="tip">小于这个体积的文件不生成 strm，0 表示不限制</div>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">跳过待整理目录</div>
        <div class="d">网盘目录名命中下面的关键词时整棵子树不扫</div>
      </div>
      <div class="row">
        <input v-model="form.pending_dirs" class="mono" style="width: 180px" placeholder="待整理,待归类" />
        <label class="switch">
          <input type="checkbox" v-model="form.skip_pending" /><span class="track"></span>
        </label>
      </div>
    </div>

    <label class="field">
      <span>定时全量 Cron</span>
      <input v-model="form.cron_full" class="mono" placeholder="0 3 * * *" />
      <div class="tip">5 段：分 时 日 月 周；例如 <code>0 3 * * *</code> 是每天 03:00 跑全量，留空关闭</div>
    </label>

    <div class="switch-row">
      <div class="txt">
        <div class="t">下载元数据文件</div>
        <div class="d">元数据后缀的文件直接下载到本地 strm 所在目录</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="form.download_meta" /><span class="track"></span>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">事件监控</div>
        <div class="d">
          监听 115 目录变化，新增文件实时生成 strm（每 {{ form.watch_interval || 60 }} 秒拉一次事件）
        </div>
      </div>
      <div class="row">
        <input v-model.number="form.watch_interval" type="number" min="15" style="width: 92px" title="拉事件的间隔（秒）" />
        <span class="muted small">秒</span>
        <label class="switch">
          <input type="checkbox" v-model="form.watch_enabled" /><span class="track"></span>
        </label>
      </div>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">定时巡检</div>
        <div class="d">按下面的间隔自动跑一次增量生成；只补新增 / 改名的文件，不会重写已有 strm（全量、全新仍需手动点）</div>
      </div>
      <div class="row">
        <input v-model.number="form.sched_minutes" type="number" min="5" style="width: 92px" />
        <span class="muted small">分钟</span>
        <label class="switch">
          <input type="checkbox" v-model="form.sched_enabled" /><span class="track"></span>
        </label>
      </div>
    </div>

    <template #foot>
      <button class="btn" @click="editorOpen = false">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存配置' }}</button>
    </template>
  </AppModal>

  <AppModal v-if="filesOpen" :title="`文件 · ${filesTitle}`" wide @close="filesOpen = false">
    <div v-if="!files.length" class="empty">还没有生成文件</div>
    <table v-else class="table">
      <thead>
        <tr><th>路径</th><th>大小</th><th>状态</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="f in files" :key="f.id">
          <td>
            <div class="name wrap-any">{{ f.name }}</div>
            <div class="sub">{{ f.rel_path }}</div>
          </td>
          <td class="num">{{ fmtSize(f.size) }}</td>
          <td><span class="badge" :class="f.deleted ? 'err' : 'ok'">{{ f.deleted ? '已失效' : '正常' }}</span></td>
          <td><button class="btn sm" @click="copyUrl(f)">复制链接</button></td>
        </tr>
      </tbody>
    </table>
  </AppModal>
</template>

<style scoped>
.path-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.path-remote,
.path-local {
  flex: 1;
  min-width: 0;
}
.path-remote {
  color: var(--text-2);
  cursor: default;
}
.arrow {
  color: var(--muted);
}
.strm-preview {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-2, rgba(127, 127, 127, 0.06));
  padding: 9px 11px;
  font-size: 12.5px;
  color: var(--text-2);
  white-space: nowrap;
  overflow-x: auto;
}
</style>
