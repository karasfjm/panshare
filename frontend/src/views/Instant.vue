<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, fmtTime } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import { loadSettings, patchSettings } from '../lib/settings'
import type { Account, InstantMapping, InstantTarget } from '../lib/types'
import DirPicker from '../components/DirPicker.vue'

const accounts = ref<Account[]>([])
const targets = ref<InstantTarget[]>([])
const mappings = ref<InstantMapping[]>([])
const users = ref<{ id: string; name: string }[]>([])
const loadingUsers = ref(false)
// 预秒传开关：true = 播放器请求播放信息时就在后台秒传好
const prewarm = ref(true)
const savingPrewarm = ref(false)

const targetForm = reactive({ account_id: '', dir_cid: '0', dir_path: '', enabled: true })
const mapForm = reactive({ emby_user_id: '', emby_user_name: '', account_id: '' })

async function loadPrewarm() {
  try {
    const st = await loadSettings()
    prewarm.value = !st.instant_prewarm_off
  } catch (e) {
    toastError(e)
  }
}

async function savePrewarm() {
  savingPrewarm.value = true
  try {
    await patchSettings({ instant_prewarm_off: !prewarm.value })
    toast(prewarm.value ? '已开启预秒传' : '已关闭预秒传：改为播放时秒传')
  } catch (e) {
    prewarm.value = !prewarm.value
    toastError(e)
  } finally {
    savingPrewarm.value = false
  }
}

async function load() {
  try {
    const [i, a] = await Promise.all([
      api.get<{ targets: InstantTarget[]; mappings: InstantMapping[] }>('/instant'),
      api.get<{ accounts: Account[] }>('/accounts'),
    ])
    targets.value = i.targets || []
    mappings.value = i.mappings || []
    accounts.value = a.accounts || []
    if (!mapForm.account_id) mapForm.account_id = accounts.value[0]?.id || ''
    if (!targetForm.account_id) targetForm.account_id = accounts.value[0]?.id || ''
  } catch (e) {
    toastError(e)
  }
}
onMounted(() => {
  load()
  loadPrewarm()
})

async function loadUsers() {
  loadingUsers.value = true
  try {
    const res = await api.get<{ users: { id: string; name: string }[] }>('/emby/users')
    users.value = res.users || []
    if (!users.value.length) toast('Emby 里没有用户（先检查服务器配置）')
  } catch (e) {
    toastError(e)
  } finally {
    loadingUsers.value = false
  }
}

function pickUser(id: string) {
  mapForm.emby_user_id = id
  const u = users.value.find((x) => x.id === id)
  mapForm.emby_user_name = u?.name || ''
}

async function saveTarget() {
  if (!targetForm.account_id) {
    toastError(new Error('请选择账号'))
    return
  }
  try {
    await api.post('/instant/targets', { ...targetForm })
    toast('已保存被秒传账号')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function saveMapping() {
  if (!mapForm.emby_user_id || !mapForm.account_id) {
    toastError(new Error('请选择 Emby 用户与账号'))
    return
  }
  try {
    await api.post('/instant/mappings', { ...mapForm })
    toast('已保存映射')
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function delTarget(accountId: string) {
  try {
    await api.del(`/instant/targets/${accountId}`)
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function delMapping(userId: string) {
  try {
    await api.del(`/instant/mappings/${userId}`)
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
        <h3>预秒传</h3>
        <div class="desc">
          播放器来取播放信息时，就在后台把文件秒传到下面的账号里，等它真正拉流时目标账号已经就绪，
          首播几乎不用等。关掉后改为「播放到 /p115/instant 时再秒传」，功能不变，只是第一次播放多等两三秒。
        </div>
      </div>
    </div>
    <div class="switch-row">
      <div class="txt">
        <div class="t">提前秒传（推荐开着）</div>
        <div class="d">关掉后只有真正播放时才秒传，避免误点也占目标账号目录</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="prewarm" :disabled="savingPrewarm" @change="savePrewarm" /><span class="track"></span>
      </label>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>被秒传账号</h3>
        <div class="desc">
          选 115 账号与落盘目录。可多选：一个 Emby 用户可以对应一个账号，播放前先把文件秒传到这个目录。
        </div>
      </div>
    </div>

    <div class="grid c3">
      <label class="field">
        <span>115 账号</span>
        <select v-model="targetForm.account_id">
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
      <label class="field">
        <span>秒传落盘目录</span>
        <div class="inline">
          <input :value="targetForm.dir_path || (targetForm.dir_cid === '0' ? '根目录' : targetForm.dir_cid)" readonly />
          <DirPicker
            :account-id="targetForm.account_id"
            :model-value="targetForm.dir_cid"
            @update:model-value="(v: string) => (targetForm.dir_cid = v)"
            @pick="(d) => (targetForm.dir_path = d.name)"
          />
        </div>
      </label>
      <label class="field">
        <span>状态</span>
        <label class="switch" style="margin-top: 6px">
          <input type="checkbox" v-model="targetForm.enabled" /><span class="track"></span>
          <span class="label">{{ targetForm.enabled ? '启用' : '停用' }}</span>
        </label>
      </label>
    </div>
    <button class="btn primary" @click="saveTarget">保存</button>

    <div class="list mt16">
      <div v-for="t in targets" :key="t.account_id" class="row-item">
        <div class="body">
          <div class="title">
            {{ t.account_name || t.account_id }}
            <span class="badge" :class="t.enabled ? 'ok' : ''">{{ t.enabled ? '启用' : '停用' }}</span>
          </div>
          <div class="meta">落盘目录 {{ t.dir_path || t.dir_cid }}</div>
        </div>
        <button class="btn sm danger" @click="delTarget(t.account_id)">移除</button>
      </div>
      <div v-if="!targets.length" class="empty">还没有配置被秒传账号</div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>Emby 用户映射</h3>
        <div class="desc">
          该用户播放被秒传账号的资源时，先秒传到映射账号的目录下，再通过该目录里的资源播放。
        </div>
      </div>
      <div class="actions">
        <button class="btn sm" :disabled="loadingUsers" @click="loadUsers">
          {{ loadingUsers ? '读取中…' : '读取 Emby 用户' }}
        </button>
      </div>
    </div>

    <div class="grid c3">
      <label class="field">
        <span>Emby 用户</span>
        <select :value="mapForm.emby_user_id" @change="pickUser(($event.target as HTMLSelectElement).value)">
          <option value="">选择用户</option>
          <option v-for="u in users" :key="u.id" :value="u.id">{{ u.name }}</option>
        </select>
        <div class="tip">点右上「读取 Emby 用户」；也可以手动填下面的 ID</div>
      </label>
      <label class="field">
        <span>用户 ID（可手填）</span>
        <input v-model="mapForm.emby_user_id" placeholder="Emby 的 UserId" />
      </label>
      <label class="field">
        <span>秒传到哪个账号</span>
        <select v-model="mapForm.account_id">
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
    </div>
    <button class="btn primary" @click="saveMapping">保存映射</button>

    <table class="table mt16" v-if="mappings.length">
      <thead>
        <tr><th>Emby 用户</th><th>映射账号</th><th>更新时间</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="m in mappings" :key="m.emby_user_id">
          <td>
            <div class="name">{{ m.emby_user_name || '（未命名）' }}</div>
            <div class="sub mono">{{ m.emby_user_id }}</div>
          </td>
          <td>{{ m.account_name || m.account_id }}</td>
          <td class="small muted">{{ fmtTime(m.updated_at) }}</td>
          <td class="nowrap"><button class="btn sm danger" @click="delMapping(m.emby_user_id)">删除</button></td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty mt16">还没有用户映射</div>
  </section>
</template>
