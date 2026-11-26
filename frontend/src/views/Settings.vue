<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../lib/api'
import { toast, toastError } from '../lib/toast'
import { loadSettings, patchSettings } from '../lib/settings'
import type { Account, Settings } from '../lib/types'
import DirPicker from '../components/DirPicker.vue'

const s = ref<Settings | null>(null)
const accounts = ref<Account[]>([])
const derived = ref<{ strm_root: string; base_url: string; uptime: string; version: string } | null>(null)
const saving = ref(false)

onMounted(async () => {
  try {
    const [res, a] = await Promise.all([
      api.get<{ settings: Settings; derived: any }>('/settings'),
      api.get<{ accounts: Account[] }>('/accounts'),
    ])
    s.value = res.settings
    derived.value = res.derived
    accounts.value = a.accounts || []
  } catch (e) {
    toastError(e)
  }
})

/** 目录显示：有名字就用名字，别的都是兜底 */
function dirLabel(cid: string, name: string) {
  if (name) return cid === '0' ? '根目录' : name
  if (!cid || cid === '0') return '根目录'
  return cid
}

function setShareDir(d: { id: string; name: string }) {
  if (!s.value) return
  s.value.share_dir_cid = d.id
  s.value.share_dir_name = d.name
}
function setEd2kDir(d: { id: string; name: string }) {
  if (!s.value) return
  s.value.ed2k_dir_cid = d.id
  s.value.ed2k_dir_name = d.name
}

async function save() {
  if (!s.value) return
  saving.value = true
  try {
    await patchSettings({
      site_name: s.value.site_name,
      strm_root: s.value.strm_root,
      public_base_url: s.value.public_base_url,
      default_exts: s.value.default_exts,
      default_meta_exts: s.value.default_meta_exts,
      watch_delay_sec: s.value.watch_delay_sec,
      watch_interval_sec: s.value.watch_interval_sec,
      download_meta: s.value.download_meta,
      api_qps: s.value.api_qps,
      share_dir_cid: s.value.share_dir_cid,
      share_dir_name: s.value.share_dir_name,
      ed2k_account_id: s.value.ed2k_account_id,
      ed2k_dir_cid: s.value.ed2k_dir_cid,
      ed2k_dir_name: s.value.ed2k_dir_name,
      ed2k_transfer_on_add: s.value.ed2k_transfer_on_add,
      ed2k_ttl_hours: s.value.ed2k_ttl_hours,
    })
    toast('已保存')
    const res = await api.get<{ settings: Settings; derived: any }>('/settings')
    s.value = res.settings
    derived.value = res.derived
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="card" v-if="s">
    <div class="card-head">
      <div class="h">
        <h3>基础</h3>
        <div class="desc">请求头必须填播放器能访问到的地址（局域网 IP 或域名），不能写 127.0.0.1 / 172.17.0.1。</div>
      </div>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>站点名</span>
        <input v-model="s.site_name" />
      </label>
      <label class="field">
        <span>请求头（写进 strm）</span>
        <input v-model="s.public_base_url" placeholder="http://192.168.1.10:9527" />
        <div class="tip">改完已生成的 strm 不会自动变，需要跑一次「全量」重写</div>
      </label>
      <label class="field">
        <span>映射根</span>
        <input v-model="s.strm_root" placeholder="/strm" />
        <div class="tip">strm 只允许写在这个目录下；改了立即生效（旧文件留在旧根里）</div>
      </label>
      <label class="field">
        <span>115 接口 QPS</span>
        <input v-model.number="s.api_qps" type="number" min="1" />
        <div class="tip">115 前面有 WAF，太高会返回 405；一般 1~2 就够</div>
      </label>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>默认视频扩展名</span>
        <input v-model="s.default_exts" placeholder=".mp4,.mkv,.ts" />
      </label>
      <label class="field">
        <span>默认元数据扩展名</span>
        <input v-model="s.default_meta_exts" placeholder=".nfo,.jpg,.srt,.ass" />
        <div class="tip">nfo / 海报 / 字幕等，配置里不填就按这个下载</div>
      </label>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>事件监控默认扫描间隔（秒）</span>
        <input v-model.number="s.watch_interval_sec" type="number" min="15" />
      </label>
      <label class="field">
        <span>事件监控默认延时（秒）</span>
        <input v-model.number="s.watch_delay_sec" type="number" min="0" />
      </label>
      <label class="field">
        <span>默认下载元数据</span>
        <label class="switch" style="margin-top: 6px">
          <input type="checkbox" v-model="s.download_meta" /><span class="track"></span>
          <span class="label">{{ s.download_meta ? '开' : '关' }}</span>
        </label>
      </label>
    </div>
  </section>

  <section class="card" v-if="s">
    <div class="card-head">
      <div class="h">
        <h3>分享 / ed2k 默认值</h3>
        <div class="desc">新建任务时的默认接收位置，页面上还可以单独改</div>
      </div>
    </div>

    <div class="grid c2">
      <label class="field">
        <span>分享接收目录</span>
        <div class="inline">
          <input :value="dirLabel(s.share_dir_cid, s.share_dir_name)" readonly />
          <DirPicker
            :account-id="s.share_account_id || accounts[0]?.id || ''"
            :model-value="s.share_dir_cid"
            @pick="setShareDir"
          />
        </div>
        <div class="tip">选完记得点下面的「保存设置」；留空则自动建「分享接收」</div>
      </label>
      <label class="field">
        <span>ed2k 默认账号</span>
        <select v-model="s.ed2k_account_id">
          <option value="">跟随默认账号</option>
          <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name || a.uid }}</option>
        </select>
      </label>
      <label class="field">
        <span>ed2k 转存目录</span>
        <div class="inline">
          <input :value="dirLabel(s.ed2k_dir_cid, s.ed2k_dir_name)" readonly />
          <DirPicker
            :account-id="s.ed2k_account_id || accounts[0]?.id || ''"
            :model-value="s.ed2k_dir_cid"
            @pick="setEd2kDir"
          />
        </div>
        <div class="tip">选完记得点下面的「保存设置」；留空则自动建「ed2k」</div>
      </label>
      <label class="field">
        <span>ed2k 默认时机</span>
        <label class="switch" style="margin-top: 6px">
          <input type="checkbox" v-model="s.ed2k_transfer_on_add" /><span class="track"></span>
          <span class="label">{{ s.ed2k_transfer_on_add ? '添加时转存' : '播放时转存' }}</span>
        </label>
      </label>
    </div>

    <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存设置' }}</button>
  </section>

  <section class="card" v-if="derived">
    <div class="card-head">
      <div class="h"><h3>运行信息</h3></div>
    </div>
    <div class="kv">
      <div class="k">实际映射根</div>
      <div class="mono wrap-any">{{ derived.strm_root }}</div>
      <div class="k">实际请求头</div>
      <div class="mono wrap-any">{{ derived.base_url }}</div>
      <div class="k">运行时长</div>
      <div>{{ derived.uptime }}</div>
      <div class="k">版本</div>
      <div class="mono">{{ derived.version }}</div>
    </div>
  </section>
</template>
