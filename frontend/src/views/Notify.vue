<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import { loadSettings, patchSettings } from '../lib/settings'
import type { Settings } from '../lib/types'

interface TGStatus {
  enabled: boolean
  commands: boolean
  polling: boolean
  proxy: string
  proxy_error?: string
  last_error?: string
  last_error_at?: string
  last_ok_at?: string
}

interface WeChatStatus {
  enabled: boolean
  app_configured: boolean
  to_user: string
  last_error?: string
  last_error_at?: string
  last_ok_at?: string
  // 企业微信「接收消息」回调（地址里带 token=API）
  app_callback_ready: boolean
  // 是否允许在聊天窗口发指令 / 发链接
  commands: boolean
  // 通知类型过滤（原始字符串）与菜单同步时间
  notify_kinds?: string
  menu_at?: string
}

const s = ref<Settings | null>(null)
const saving = ref(false)
const testing = ref(false)
const status = ref<TGStatus | null>(null)

const wxSaving = ref(false)
const wxTesting = ref(false)
const menuPushing = ref(false)
const wxStatus = ref<WeChatStatus | null>(null)

// 事件类型：和 Event(level, kind, …) 里的 kind 一一对应
const ALL_KINDS = [
  { key: 'strm', label: '生成任务' },
  { key: 'share', label: '分享转存' },
  { key: 'ed2k', label: 'ed2k 任务' },
  { key: 'instant', label: '秒传' },
  { key: 'account', label: '账号' },
  { key: 'link', label: '联动删除' },
  { key: 'plugin', label: '插件' },
  { key: 'settings', label: '服务设置' },
  { key: 'copy', label: '同播复制' },
  { key: 'emby', label: 'Emby 反代' },
]

// 库里存的是逗号串：空 = 全部，「-」= 一条都不推。
// 认不出来的 key（历史遗留，比如已下线的 telegram）直接丢掉，不显示也不提交。
const pickedKinds = computed(() => {
  const raw = (s.value?.wechat_notify_kinds || '').trim()
  if (!raw) return ALL_KINDS.map((k) => k.key)
  if (raw === '-') return [] as string[]
  const known = new Set(ALL_KINDS.map((k) => k.key))
  return raw
    .split(',')
    .map((x) => x.trim())
    .filter((x) => known.has(x))
})

const kindOpen = ref(false)
const kindBox = ref<HTMLElement | null>(null)

// 下拉框外面点一下就收起来
function onDocClick(e: MouseEvent) {
  if (!kindOpen.value) return
  if (kindBox.value && !kindBox.value.contains(e.target as Node)) kindOpen.value = false
}

const kindSummary = computed(() => {
  const picked = pickedKinds.value
  if (picked.length === ALL_KINDS.length) return '全部类型'
  if (picked.length === 0) return '一条都不推'
  const labels = ALL_KINDS.filter((k) => picked.includes(k.key)).map((k) => k.label)
  return '已选 ' + picked.length + ' 项 · ' + labels.join('、')
})

function setKinds(keys: string[]) {
  if (!s.value) return
  const next = ALL_KINDS.map((k) => k.key).filter((k) => keys.includes(k))
  s.value.wechat_notify_kinds = next.length === 0 ? '-' : next.join(',')
}

function toggleKind(key: string) {
  const cur = new Set(pickedKinds.value)
  if (cur.has(key)) cur.delete(key)
  else cur.add(key)
  setKinds([...cur])
}

function pickAllKinds() {
  setKinds(ALL_KINDS.map((k) => k.key))
}

function clearKinds() {
  setKinds([])
}

// 存库的写法：全选存空串（以后新增类型自动包含），清空存「-」，否则存逗号列表。
// 这样历史遗留的 telegram 之类不会又被写回去。
function kindsForSave(): string {
  const picked = pickedKinds.value
  if (picked.length === ALL_KINDS.length) return ''
  if (picked.length === 0) return '-'
  return picked.join(',')
}

// 企业微信「接收消息 → 设置API接收」要填的地址：外网地址 + 回调路径 + 地址里的 API
const appCallbackURL = computed(() => {
  const base = (s.value?.wechat_mp_base || window.location.origin || '').replace(/\/+$/, '')
  const api = (s.value?.wechat_app_api || '').trim()
  return base + '/api/wechat/app/callback' + (api ? '?token=' + encodeURIComponent(api) : '')
})

async function loadStatus() {
  try {
    status.value = await api.get<TGStatus>('/tg/status')
  } catch {
    status.value = null
  }
}

async function loadWeChatStatus() {
  try {
    wxStatus.value = await api.get<WeChatStatus>('/wechat/status')
  } catch {
    wxStatus.value = null
  }
}

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  try {
    s.value = await loadSettings()
    await Promise.all([loadStatus(), loadWeChatStatus()])
  } catch (e) {
    toastError(e)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
})

async function save() {
  if (!s.value) return
  saving.value = true
  try {
    s.value = await patchSettings({
      tg_enabled: s.value.tg_enabled,
      tg_bot_token: s.value.tg_bot_token,
      tg_chat_id: s.value.tg_chat_id,
      tg_commands: s.value.tg_commands,
      tg_proxy: s.value.tg_proxy,
    })
    toast('已保存')
    await loadStatus()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    await api.post('/tg/test')
    toast('测试消息已发出，去 Telegram 看看')
  } catch (e) {
    toastError(e)
  } finally {
    testing.value = false
    await loadStatus()
  }
}

// 回调地址里的凭据，需要够长才猜不到，手填不现实，这里一键生成
function genAppAPI() {
  if (!s.value) return
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  const buf = new Uint8Array(32)
  crypto.getRandomValues(buf)
  s.value.wechat_app_api = Array.from(buf, (b) => chars[b % chars.length]).join('')
  toast('已生成，记得点下面的保存')
}

async function saveWeChat() {
  if (!s.value) return
  wxSaving.value = true
  try {
    await patchSettings({
      wechat_enabled: s.value.wechat_enabled,
      wechat_corp_id: s.value.wechat_corp_id,
      wechat_agent_id: s.value.wechat_agent_id,
      wechat_secret: s.value.wechat_secret,
      wechat_to_user: s.value.wechat_to_user,
      wechat_commands: s.value.wechat_commands,
      wechat_notify_kinds: kindsForSave(),
      wechat_app_api: s.value.wechat_app_api,
      wechat_app_token: s.value.wechat_app_token,
      wechat_app_aeskey: s.value.wechat_app_aeskey,
    })
    toast('微信通知设置已保存')
    await loadWeChatStatus()
  } catch (e) {
    toastError(e)
  } finally {
    wxSaving.value = false
  }
}

async function pushMenu() {
  menuPushing.value = true
  try {
    await api.post('/wechat/app/menu')
    toast('自定义菜单已同步，去企业微信里看看')
    await loadWeChatStatus()
  } catch (e) {
    toastError(e)
  } finally {
    menuPushing.value = false
  }
}

async function testWeChat() {
  wxTesting.value = true
  try {
    await api.post('/wechat/test')
    toast('测试消息已发出，去微信看看')
  } catch (e) {
    toastError(e)
  } finally {
    wxTesting.value = false
    await loadWeChatStatus()
  }
}

</script>

<template>
  <section class="card" v-if="s">
    <div class="card-head">
      <div class="h">
        <h3>Telegram</h3>
        <div class="desc">任务开始 / 完成 / 失败都会推送；开启指令后，可以直接在聊天窗口里操作本服务。</div>
      </div>
      <div class="actions">
        <button class="btn" :disabled="testing" @click="test">{{ testing ? '发送中…' : '发测试消息' }}</button>
      </div>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">启用推送</div>
        <div class="d">Bot Token 与 Chat ID 都填了才会真正发消息</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.tg_enabled" /><span class="track"></span>
      </label>
    </div>

    <div class="grid c2 mt12">
      <label class="field">
        <span>Bot Token</span>
        <input v-model="s.tg_bot_token" placeholder="123456:ABC-DEF…" />
        <div class="tip">找 @BotFather 新建机器人拿 token</div>
      </label>
      <label class="field">
        <span>Chat ID</span>
        <input v-model="s.tg_chat_id" placeholder="例如 123456789 或 -100…" />
        <div class="tip">给机器人发条消息后，用 getUpdates 能看到 chat.id</div>
      </label>
    </div>

    <label class="field mt12">
      <span>共享代理（Telegram / TMDB，国内必填）</span>
      <input v-model="s.tg_proxy" placeholder="http://192.168.1.34:7890 或 socks5://192.168.1.34:7891" />
      <div class="tip">
        Telegram 通知、TG 指令和登录页的 TMDB 热门背景共用这一个代理，只需填一次。
        留空则依次回落到 compose 的 <span class="mono">PS_PROXY</span>、环境变量 <span class="mono">HTTPS_PROXY</span>。
        mihomo / clash 的「混合端口」就是 HTTP 代理。
      </div>
    </label>

    <div class="switch-row">
      <div class="txt">
        <div class="t">允许聊天窗口发指令 / 发链接</div>
        <div class="d">只响应上面这个 Chat ID，别人拉群也指挥不动；发普通聊天不会回复</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.tg_commands" /><span class="track"></span>
      </label>
    </div>

    <button class="btn primary mt12" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>

    <div v-if="status" class="notice mt16">
      <div v-if="status.proxy_error"><strong>代理配置有问题：</strong>{{ status.proxy_error }}</div>
      <div v-else-if="status.last_error">
        <strong>最近一次失败：</strong>{{ status.last_error }}
        <span class="muted">（{{ status.last_error_at }}）</span>
      </div>
      <div v-else>
        <strong>链路正常</strong>
        <span v-if="status.last_ok_at" class="muted">（最近成功 {{ status.last_ok_at }}）</span>
        <span v-else class="muted">（还没成功过一次）</span>
      </div>
      <div class="muted mt8">
        指令轮询：{{ status.polling ? '运行中（每 5 秒拉一次）' : '未开启' }}
        · 代理：{{ status.proxy || '跟随环境变量' }}
      </div>
    </div>

    <div class="notice mt16">
      <div>
        <strong>可用指令</strong>（在 TG 里发 <code>/help</code> 随时看，含每条配置的专属指令）<br />
        <strong>查看</strong>：<code>/status</code> 运行状态 · <code>/accounts</code> 账号列表 ·
        <code>/strm</code> 配置、文件数与专属指令<br />
        <strong>生成</strong>：<code>/inc 配置名|all</code> 增量 ·
        <code>/full 配置名|all</code> 全量<br />
        <span class="muted">
          每条配置还会自动生成一对专属指令，形如 <code>/inc_movie</code> / <code>/full_movie</code>，
          代号在「115 STRM」配置列表里、<code>/strm</code> 与 <code>/help</code> 里都能看到。
        </span><br />
        <strong>发链接</strong>：直接把 <code>ed2k://</code> 或 115 分享链接发给机器人（可多行），
        会按「ed2k」「分享转存」页面里的账号 / 目录设置建任务并生成 strm；
        <code>/ed2k 链接</code>、<code>/share 链接</code> 可以显式指定类型<br />
        <strong>其它</strong>：<code>/find 关键词</code> 搜索索引 · <code>/link</code> 跑一次联动对账
      </div>
    </div>
  </section>

  <section class="card mt16" v-if="s">
    <div class="card-head">
      <div class="h">
        <h3>微信通知</h3>
        <div class="desc">
          任务开始 / 完成 / 失败都会推送到企业微信；配好下面的接收消息地址，企业微信后台才允许填「企业可信 IP」。
        </div>
      </div>
      <div class="actions">
        <button class="btn" :disabled="wxTesting" @click="testWeChat">
          {{ wxTesting ? '发送中…' : '发测试消息' }}
        </button>
      </div>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">启用微信推送</div>
        <div class="d">CorpID / AgentID / Secret 填齐才会真正发消息</div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.wechat_enabled" /><span class="track"></span>
      </label>
    </div>

    <div class="switch-row">
      <div class="txt">
        <div class="t">允许聊天窗口发指令 / 发链接</div>
        <div class="d">
          和 Telegram 一套指令：<span class="mono">/help</span> 看全部，
          <span class="mono">/status</span>、<span class="mono">/inc</span>、<span class="mono">/full</span> 管生成，
          直接发 <span class="mono">ed2k://</span> 或 115 分享链接也能建任务；普通聊天不回复
        </div>
      </div>
      <label class="switch">
        <input type="checkbox" v-model="s.wechat_commands" /><span class="track"></span>
      </label>
    </div>

    <div class="mt12" style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
      <button class="btn sm" :disabled="menuPushing" @click="pushMenu">
        {{ menuPushing ? '同步中…' : '同步指令到自定义菜单' }}
      </button>
      <span class="muted" v-if="wxStatus?.menu_at">上次同步：{{ wxStatus.menu_at }}</span>
    </div>
    <div class="tip" style="margin-top: 6px">
      提交后应用的聊天窗口底部会多出「运行 / 全量 / 增量」三个菜单：运行里有状态、账号、配置、帮助；
      全量和增量各带一条「all」以及每条生成配置的专属项。点一下就当指令执行，走的还是被动回复。
      这个接口同样要求出口 IP 在可信 IP 白名单里，没配会报 60020。
    </div>

    <div class="field mt16">
      <span>通知类型（只推勾选的）</span>
      <div ref="kindBox" class="kinds">
        <button type="button" class="kinds-btn" :class="{ open: kindOpen }" @click="kindOpen = !kindOpen">
          <span class="kinds-txt">{{ kindSummary }}</span>
          <span class="kinds-arrow">▾</span>
        </button>
        <div v-if="kindOpen" class="kinds-pop">
          <div class="kinds-list">
            <label v-for="k in ALL_KINDS" :key="k.key" class="kinds-item">
              <input type="checkbox" :checked="pickedKinds.includes(k.key)" @change="toggleKind(k.key)" />
              <span>{{ k.label }}</span>
            </label>
          </div>
          <div class="kinds-acts">
            <button type="button" class="btn sm" @click="pickAllKinds">全选</button>
            <button type="button" class="btn sm" @click="clearKinds">清空</button>
          </div>
        </div>
      </div>
      <div class="tip">
        按事件来源过滤，和成功 / 失败无关：只勾「生成任务」就只推 strm 相关事件。
        全勾 = 全部推送，全部取消 = 一条都不推。
      </div>
    </div>

    <div class="grid c2 mt12">
      <label class="field">
        <span>CorpID（企业ID）</span>
        <input v-model="s.wechat_corp_id" placeholder="ww1234567890abcdef" />
        <div class="tip">企业微信后台 →「我的企业」→ 企业ID</div>
      </label>
      <label class="field">
        <span>AgentID（应用ID）</span>
        <input v-model="s.wechat_agent_id" placeholder="1000002" />
        <div class="tip">「应用管理」→ 自建应用 → AgentId（纯数字）</div>
      </label>
    </div>

    <div class="grid c2 mt12">
      <label class="field">
        <span>Secret（应用密钥）</span>
        <input
          v-model="s.wechat_secret"
          type="password"
          placeholder="应用详情页的 Secret"
          autocomplete="new-password"
        />
        <div class="tip">不是通讯录的 Secret</div>
      </label>
      <label class="field">
        <span>接收成员</span>
        <input v-model="s.wechat_to_user" placeholder="@all" />
        <div class="tip">UserID，多个用 <span class="mono">|</span> 分隔；留空发全部可见成员</div>
      </label>
    </div>

    <h4 class="mt16" style="font-size: 13.5px; color: var(--text-2)">
      接收消息地址（配置企业可信 IP 的前置条件）
    </h4>

    <div class="field mt12">
      <span>API</span>
      <div style="display: flex; gap: 8px; align-items: center">
        <input
          v-model="s.wechat_app_api"
          placeholder="点右侧按钮生成"
          style="flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px"
        />
        <button class="btn sm" @click="genAppAPI">随机生成API</button>
      </div>
      <div class="tip">
        企业微信后台「应用 → 接收消息 → 设置API接收」里填的地址就是：<br />
        <span class="mono" style="word-break: break-all">
          外网地址 + /api/wechat/app/callback?token={{ s.wechat_app_api || 'API' }}
        </span>
        <br />
        现在算出来是 <span class="mono" style="word-break: break-all">{{ appCallbackURL }}</span><br />
        地址里的 token 是访问凭据；下面 Token / EncodingAESKey 只在后台选「安全模式」时需要一致，
        选「明文模式」可以留空。
      </div>
    </div>

    <div class="grid c2 mt12">
      <label class="field">
        <span>Token</span>
        <input
          v-model="s.wechat_app_token"
          placeholder="自己定一个，字母数字都行"
          style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px"
        />
        <div class="tip">必须和企业微信后台填的完全一致</div>
      </label>
      <label class="field">
        <span>EncodingAESKey</span>
        <input
          v-model="s.wechat_app_aeskey"
          placeholder="43 位"
          style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px"
        />
        <div class="tip">
          后台「随机获取」后整串复制过来；选<strong>明文模式</strong>可留空
        </div>
      </label>
    </div>

    <div v-if="wxStatus" class="notice mt16">
      <div v-if="wxStatus.last_error">
        <strong>最近一次失败：</strong>{{ wxStatus.last_error }}
        <span class="muted">（{{ wxStatus.last_error_at }}）</span>
      </div>
      <div v-else>
        <strong>链路正常</strong>
        <span v-if="wxStatus.last_ok_at" class="muted">（最近成功 {{ wxStatus.last_ok_at }}）</span>
        <span v-else class="muted">（还没成功过一次）</span>
      </div>
      <div class="muted mt8">
        应用{{ wxStatus.app_configured ? '已配置' : '未配置' }} ·
        接收：{{ wxStatus.to_user }} ·
        接收消息地址{{ wxStatus.app_callback_ready ? '已就绪' : '未填 API' }} ·
        指令{{ wxStatus.commands ? '已开启' : '未开启' }}
      </div>
    </div>

    <div class="notice mt16">
      <div>
        自建应用要求服务器出口 IP 在「企业可信 IP」白名单里，否则会报 <strong>60020</strong>。
        微信接口（<span class="mono">qyapi.weixin.qq.com</span>）国内直连，<strong>不走</strong>上面的海外代理；
        消息按企业微信 2048 字节上限自动分片，长任务日志不会发送失败。
      </div>
    </div>

    <button class="btn primary mt12" :disabled="wxSaving" @click="saveWeChat">
      {{ wxSaving ? '保存中…' : '保存微信设置' }}
    </button>
  </section>
</template>

<style scoped>
/* .notice 是 flex 容器，里面长代理地址不换行会把整页撑宽 */
.notice {
  flex-wrap: wrap;
}
.notice > div {
  min-width: 0;
  overflow-wrap: anywhere;
}

.kinds {
  position: relative;
  max-width: 420px;
}
.kinds-btn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  height: 34px;
  padding: 0 10px;
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  font-size: 13px;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.kinds-btn.open,
.kinds-btn:focus-visible {
  border-color: var(--brand);
  outline: none;
}
.kinds-txt {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kinds-arrow {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 12px;
}
.kinds-pop {
  position: absolute;
  z-index: 20;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  padding: 6px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
}
/* 只有列表滚动，全选/清空 始终贴着底边可见 */
.kinds-list {
  max-height: 232px;
  overflow-y: auto;
}
.kinds-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 7px;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.kinds-item:hover {
  background: var(--panel-soft);
}
/* 全局 input 是 width:100%，复选框会被拉满整行，这里收回去 */
.kinds-item input[type='checkbox'] {
  width: auto;
  flex: 0 0 auto;
  margin: 0;
  padding: 0;
  accent-color: var(--brand);
}
.kinds-acts {
  display: flex;
  gap: 8px;
  padding: 6px 2px 0;
  border-top: 1px solid var(--line);
  margin-top: 4px;
}
</style>
