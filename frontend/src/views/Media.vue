<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { EmbyProxyInfo, EmbyServer } from '../lib/types'
import AppModal from '../components/AppModal.vue'

const servers = ref<EmbyServer[]>([])
const editorOpen = ref(false)
const editing = ref<EmbyServer | null>(null)
const form = reactive({ name: '', url: '', api_key: '', enabled: true })
const testing = ref(false)
const testResult = ref('')
const proxy = ref<EmbyProxyInfo>({
  enabled: false, on: true, port: 0, listen_port: 0, port_default: 0, port_custom: 0,
  running: false, target: '', last_error: '', uptime: '', requests: 0, redirects: 0,
})
/** 端口输入框：留空表示跟随 compose 里的默认端口 */
const portInput = ref<number | string>('')
const savingPort = ref(false)
const toggling = ref(false)

/** 反代地址：用当前页面访问的主机名 + 反代端口，用户直接抄给 Emby 客户端 */
const proxyAddr = computed(() => `http://${window.location.hostname}:${proxy.value.port}`)
/** 输入框里没填时展示的默认值 */
const defaultPort = computed(() => proxy.value.port_default || 7259)

async function load() {
  try {
    const e = await api.get<{ servers: EmbyServer[] }>('/emby/servers')
    servers.value = e.servers || []
  } catch (err) {
    toastError(err)
  }
}

async function loadProxy() {
  try {
    proxy.value = await api.get<EmbyProxyInfo>('/emby/proxy')
    portInput.value = proxy.value.port_custom > 0 ? proxy.value.port_custom : ''
  } catch {
    // 反代没启用时不影响服务器列表
  }
}

async function savePort() {
  const raw = String(portInput.value ?? '').trim()
  const port = raw === '' ? 0 : Number(raw)
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    toastError(new Error('端口要填 1-65535，留空表示用默认端口'))
    return
  }
  savingPort.value = true
  try {
    const res = await api.post<{ stats: EmbyProxyInfo }>('/emby/proxy', { port })
    proxy.value = res.stats
    portInput.value = res.stats.port_custom > 0 ? res.stats.port_custom : ''
    toast(port === 0 ? `已改用默认端口 ${res.stats.port}` : `反代已切到 ${port} 端口`)
  } catch (e) {
    toastError(e)
  } finally {
    savingPort.value = false
  }
}

/** 清空输入框 = 回到 compose 里的默认端口 */
function resetPort() {
  portInput.value = ''
  savePort()
}

async function copyProxy() {
  try {
    await navigator.clipboard.writeText(proxyAddr.value)
    toast('地址已复制')
  } catch {
    toastError(new Error('复制失败，请手动选中复制'))
  }
}

/** 反代总开关：关掉就停监听（端口留着，下次打开还是这个端口） */
async function toggleOn(ev: Event) {
  const el = ev.target as HTMLInputElement
  const on = el.checked
  toggling.value = true
  try {
    const res = await api.post<{ stats: EmbyProxyInfo }>('/emby/proxy', { enabled: on })
    proxy.value = res.stats
    portInput.value = res.stats.port_custom > 0 ? res.stats.port_custom : ''
    if (!on) {
      toast('反代已关闭')
    } else if (res.stats.running) {
      toast(`反代已开启（端口 ${res.stats.listen_port || res.stats.port}）`)
    } else {
      toast('已打开，但端口还是 0：填个端口才会开始监听')
    }
  } catch (e) {
    el.checked = !on
    toastError(e)
  } finally {
    toggling.value = false
  }
}

onMounted(() => {
  load()
  loadProxy()
})

function create() {
  editing.value = null
  Object.assign(form, { name: '', url: '', api_key: '', enabled: true })
  testResult.value = ''
  editorOpen.value = true
}

function edit(s: EmbyServer) {
  editing.value = s
  Object.assign(form, { name: s.name, url: s.url, api_key: '', enabled: s.enabled })
  testResult.value = ''
  editorOpen.value = true
}

async function test() {
  testing.value = true
  testResult.value = ''
  try {
    const info = await api.post<{ info: any }>('/emby/servers/test', { ...form, id: editing.value?.id })
    testResult.value = `连接成功：${info.info?.ServerName || info.info?.ProductName || 'Emby'} ${info.info?.Version || ''}`
  } catch (e) {
    testResult.value = e instanceof Error ? e.message : String(e)
  } finally {
    testing.value = false
  }
}

async function save() {
  try {
    await api.post('/emby/servers', { ...form, id: editing.value?.id || '' })
    toast('已保存')
    editorOpen.value = false
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function remove(s: EmbyServer) {
  if (!window.confirm(`删除服务器「${s.name}」？`)) return
  try {
    await api.del(`/emby/servers/${s.id}`)
    await load()
    toast('已删除')
  } catch (e) {
    toastError(e)
  }
}

async function toggle(s: EmbyServer) {
  try {
    await api.post('/emby/servers', { id: s.id, name: s.name, url: s.url, enabled: !s.enabled })
    await load()
  } catch (e) {
    toastError(e)
  }
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>Emby 反代</h3>
        <div class="desc">把 Emby 客户端的服务器地址改成「本机 IP + 反代端口」（点右上角复制）；播放时本服务只做 302，视频流量直连 115。</div>
      </div>
      <div class="actions">
        <label class="switch">
          <input type="checkbox" :checked="proxy.on" :disabled="toggling" @change="toggleOn" />
          <span class="track"></span>
          <span class="label">{{ proxy.on ? '已开启' : '已关闭' }}</span>
        </label>
        <button class="btn" :disabled="!proxy.running" @click="copyProxy">复制地址</button>
      </div>
    </div>

    <div class="port-row">
      <label class="field port-field">
        <span>反代端口</span>
        <input
          v-model="portInput"
          type="number"
          min="0"
          max="65535"
          :placeholder="`留空 = 默认 ${defaultPort}`"
          @keyup.enter="savePort"
        />
      </label>
      <button class="btn primary" :disabled="savingPort" @click="savePort">
        {{ savingPort ? '保存中…' : '保存端口' }}
      </button>
      <button v-if="proxy.port_custom > 0" class="btn" :disabled="savingPort" @click="resetPort">
        恢复默认
      </button>
      <span class="badge" :class="proxy.running ? 'ok' : ''">{{ proxy.running ? '运行中' : '未运行' }}</span>
      <span v-if="proxy.running && proxy.listen_port !== proxy.port" class="badge warn">
        正在切到 {{ proxy.port }}
      </span>
    </div>
    <div class="desc">
      留空就用 compose 里的 <span class="mono">PS_EMBY_PROXY_PORT</span>（当前 {{ defaultPort }}）。
      改完立即重绑，不用重启容器；但新端口要在容器上映射出来（compose 的 ports），否则局域网访问不到。
    </div>

    <div v-if="!proxy.on" class="empty">
      反代已关闭（开关打开即恢复），Emby 客户端会连不上
    </div>
    <div v-else-if="!proxy.enabled" class="empty">
      端口是 0，还没开始监听：上面填个端口，或把 <span class="mono">PS_EMBY_PROXY_PORT</span> 设成非 0
    </div>
    <template v-else>
      <div class="desc">
        上游：<span class="mono">{{ proxy.server_name || '—' }}</span>
        <span class="mono muted"> {{ proxy.target || '还没有启用的 Emby 服务器' }}</span>
      </div>
      <div class="desc">
        已 302 直链 {{ proxy.redirects }} 次 · 累计请求 {{ proxy.requests }} 次
        <template v-if="proxy.uptime"> · 已运行 {{ proxy.uptime }}</template>
      </div>
      <div v-if="proxy.last_error" class="notice mt12">{{ proxy.last_error }}</div>
      <div class="desc">
        提示：Emby 里 .strm 内容指向本服务的 /p115/play 即可，反代会自动改写成直链。
      </div>
    </template>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>Emby 服务器</h3>
        <div class="desc">可以加多台；反代用第一台启用的，秒传用户映射、联动删除也用这里的服务器。</div>
      </div>
      <div class="actions"><button class="btn primary" @click="create">添加服务器</button></div>
    </div>

    <div v-if="!servers.length" class="empty">还没有 Emby 服务器</div>
    <table v-else class="table">
      <thead>
        <tr><th>名称</th><th>地址</th><th>状态</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="s in servers" :key="s.id">
          <td>
            <div class="name">{{ s.name }}</div>
            <div class="sub" v-if="s.is_default">默认服务器</div>
          </td>
          <td class="mono small wrap-any">{{ s.url }}</td>
          <td>
            <span class="badge" :class="s.enabled ? 'ok' : ''">{{ s.enabled ? '启用' : '停用' }}</span>
            <span class="badge" v-if="!s.has_key">无 API Key</span>
          </td>
          <td class="nowrap">
            <button class="btn sm" @click="toggle(s)">{{ s.enabled ? '停用' : '启用' }}</button>
            <button class="btn sm" @click="edit(s)">编辑</button>
            <button class="btn sm danger" @click="remove(s)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>

  <AppModal v-if="editorOpen" :title="editing ? '编辑 Emby 服务器' : '添加 Emby 服务器'" @close="editorOpen = false">
    <label class="field">
      <span>名称</span>
      <input v-model="form.name" placeholder="例如：客厅 Emby" />
    </label>
    <label class="field">
      <span>地址</span>
      <input v-model="form.url" placeholder="http://192.168.1.10:8096" />
      <div class="tip">容器里访问宿主机 Emby 一般用 http://172.17.0.1:8096</div>
    </label>
    <label class="field">
      <span>API Key</span>
      <input v-model="form.api_key" placeholder="Emby → 设置 → API 密钥" />
      <div class="tip">编辑时留空表示不修改</div>
    </label>
    <label class="switch">
      <input type="checkbox" v-model="form.enabled" /><span class="track"></span>
      <span class="label">启用</span>
    </label>
    <div v-if="testResult" class="notice mt12">{{ testResult }}</div>
    <template #foot>
      <button class="btn" :disabled="testing" @click="test">{{ testing ? '测试中…' : '测试连接' }}</button>
      <button class="btn primary" @click="save">保存</button>
    </template>
  </AppModal>
</template>

<style scoped>
.port-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  margin-top: 14px;
  flex-wrap: wrap;
}
.port-field {
  width: 180px;
  margin: 0;
}
.desc {
  color: var(--muted);
  font-size: 12.5px;
  margin-top: 8px;
  line-height: 1.6;
}
.mt10 { margin-top: 10px; }
.mt12 { margin-top: 12px; }
.muted { color: var(--muted); }
</style>
