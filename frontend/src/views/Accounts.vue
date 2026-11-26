<script setup lang="ts">
import { nextTick, onUnmounted, ref } from 'vue'
import QRCode from 'qrcode'
import { api, fmtTime } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import type { Account } from '../lib/types'
import AppModal from '../components/AppModal.vue'

const accounts = ref<Account[]>([])
const defaultId = ref('')
const loading = ref(false)

const cookieOpen = ref(false)
const cookieText = ref('')
const cookieName = ref('')
const saving = ref(false)

const qrOpen = ref(false)
const qrCanvas = ref<HTMLCanvasElement | null>(null)
const qrStatus = ref('正在生成二维码…')
const qrUid = ref('')
const qrName = ref('')
const qrAccount = ref('')
let poll: number | undefined

async function load() {
  loading.value = true
  try {
    const res = await api.get<{ accounts: Account[]; default: string }>('/accounts')
    accounts.value = res.accounts || []
    defaultId.value = res.default
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}
load()

async function saveCookie() {
  if (!cookieText.value.trim()) {
    toastError(new Error('请粘贴 115 的 Cookie'))
    return
  }
  saving.value = true
  try {
    await api.post('/accounts/cookie', { cookies: cookieText.value.trim(), name: cookieName.value.trim() })
    toast('账号已保存')
    cookieOpen.value = false
    cookieText.value = ''
    cookieName.value = ''
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function startQR(accountId = '') {
  qrOpen.value = true
  qrStatus.value = '正在生成二维码…'
  qrAccount.value = accountId
  stopPoll()
  try {
    const res = await api.post<{ uid: string; content: string }>('/accounts/qr', { account_id: accountId })
    qrUid.value = res.uid
    qrStatus.value = '请用 115 App 扫码'
    await nextTick()
    if (qrCanvas.value) {
      await QRCode.toCanvas(qrCanvas.value, res.content, { width: 220, margin: 1, color: { dark: '#1f2329', light: '#ffffff' } })
    }
    poll = window.setInterval(checkQR, 2200)
  } catch (e) {
    qrStatus.value = e instanceof Error ? e.message : String(e)
  }
}

async function checkQR() {
  if (!qrUid.value) return
  try {
    const res = await api.get<{ status: string; account?: Account }>(
      `/accounts/qr/${qrUid.value}?name=${encodeURIComponent(qrName.value)}`,
    )
    if (res.status === 'scanned') qrStatus.value = '已扫码，请在手机上确认'
    if (res.status === 'expired') {
      qrStatus.value = '二维码已过期，重新生成'
      stopPoll()
    }
    if (res.status === 'ok') {
      stopPoll()
      qrOpen.value = false
      toast(`账号已添加：${res.account?.name || ''}`)
      await load()
    }
  } catch (e) {
    stopPoll()
    qrStatus.value = e instanceof Error ? e.message : String(e)
  }
}

function stopPoll() {
  if (poll) window.clearInterval(poll)
  poll = undefined
}

function closeQR() {
  stopPoll()
  qrOpen.value = false
  qrName.value = ''
}

onUnmounted(stopPoll)

async function toggleCopy(a: Account) {
  try {
    await api.patch(`/accounts/${a.id}`, { copy_play: !a.copy_play })
    a.copy_play = !a.copy_play
    toast(a.copy_play ? '已开启同播复制' : '已关闭同播复制')
  } catch (e) {
    toastError(e)
  }
}

async function rename(a: Account) {
  const name = window.prompt('备注名', a.name)
  if (name === null) return
  try {
    await api.patch(`/accounts/${a.id}`, { name: name.trim() })
    await load()
    toast('已保存')
  } catch (e) {
    toastError(e)
  }
}

async function setDefault(a: Account) {
  try {
    await api.post(`/accounts/${a.id}/default`)
    await load()
    toast('已设为默认账号')
  } catch (e) {
    toastError(e)
  }
}

async function refresh(a: Account) {
  try {
    await api.post(`/accounts/${a.id}/refresh`)
    await load()
    toast('登录态正常')
  } catch (e) {
    toastError(e)
  }
}

async function remove(a: Account) {
  if (!window.confirm(`删除账号「${a.name}」？\n名下的生成配置会一起删掉，已生成的 strm 保留。`)) return
  try {
    const res = await api.del<{ removed_configs: number }>(`/accounts/${a.id}`)
    await load()
    toast(`已删除（同时移除 ${res.removed_configs} 条配置）`)
  } catch (e) {
    toastError(e)
  }
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="h">
        <h3>115 账号</h3>
        <div class="desc">分享转存、ed2k、秒传、STRM 都从这里取账号；strm 末尾写的是 115 用户 ID。</div>
      </div>
      <div class="actions">
        <button class="btn primary" @click="cookieOpen = true">粘贴 Cookie</button>
        <button class="btn" @click="startQR()">扫码添加</button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!accounts.length" class="empty">还没有账号，先扫码或粘贴 Cookie</div>
    <div v-else class="list">
      <div v-for="a in accounts" :key="a.id" class="row-item">
        <div class="body">
          <div class="title">
            {{ a.name || a.user_name || a.uid }}
            <span v-if="a.is_default" class="badge brand">默认</span>
            <span class="badge" :class="a.status === 'ok' ? 'ok' : 'err'">
              {{ a.status === 'ok' ? '正常' : '异常' }}
            </span>
            <span class="badge" :class="a.copy_play ? 'ok' : ''">
              同播复制{{ a.copy_play ? '开' : '关' }}
            </span>
          </div>
          <div class="meta">
            uid {{ a.uid || '未知' }} · 来源 {{ a.source }} · {{ a.configs }} 条配置 · 添加于 {{ fmtTime(a.created_at) }}
            <span v-if="a.last_error" style="color: var(--err)"> · {{ a.last_error }}</span>
          </div>
        </div>
        <div class="actions">
          <button class="btn sm" @click="toggleCopy(a)">{{ a.copy_play ? '关同播复制' : '开同播复制' }}</button>
          <button class="btn sm" @click="startQR(a.id)">重新登录</button>
          <button class="btn sm" @click="rename(a)">备注</button>
          <button class="btn sm" v-if="!a.is_default" @click="setDefault(a)">设为默认</button>
          <button class="btn sm" @click="refresh(a)">校验</button>
          <button class="btn sm danger" @click="remove(a)">删除</button>
        </div>
      </div>
    </div>

    <div class="notice mt16">
      <div>
        <strong>同播复制</strong>：两台以上设备同时播放同一个文件时，在 115 里给后来者复制一份，
        各走各的直链、不抢带宽。对这个账号的所有播放生效（strm / ed2k 都算）。
      </div>
    </div>
  </section>

  <AppModal v-if="cookieOpen" title="用 Cookie 添加 115 账号" @close="cookieOpen = false">
    <div class="notice mb12">
      手机端「115 App → 我的 → 设置」或电脑浏览器登录 115 后，从开发者工具复制整条 Cookie
      （必须包含 <code>UID=</code>），粘贴到下面。
    </div>
    <label class="field">
      <span>Cookie</span>
      <textarea v-model="cookieText" placeholder="UID=...; CID=...; SEID=...; KID=..." />
    </label>
    <label class="field">
      <span>备注名（可空）</span>
      <input v-model="cookieName" placeholder="例如：主号" />
    </label>
    <template #foot>
      <button class="btn" @click="cookieOpen = false">取消</button>
      <button class="btn primary" :disabled="saving" @click="saveCookie">{{ saving ? '保存中…' : '保存账号' }}</button>
    </template>
  </AppModal>

  <AppModal v-if="qrOpen" title="扫码登录 115" @close="closeQR">
    <div style="text-align: center">
      <canvas ref="qrCanvas" style="border: 1px solid var(--line); border-radius: 12px"></canvas>
      <p class="mt12">{{ qrStatus }}</p>
      <label class="field mt12" style="text-align: left">
        <span>备注名（确认前可填）</span>
        <input v-model="qrName" placeholder="例如：主号" />
      </label>
    </div>
    <template #foot>
      <button class="btn" @click="closeQR">关闭</button>
      <button class="btn primary" @click="startQR(qrAccount)">重新生成</button>
    </template>
  </AppModal>
</template>
