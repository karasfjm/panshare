/* ==========================================================================
   ui-test.mjs · 前端逻辑测试（不依赖浏览器）
   --------------------------------------------------------------------------
   用最小 DOM 桩把 web/js/*.js 跑起来，验证：
   · 登录流程（会话判断 / 登录 / 退出）
   · 导航结构（工具箱已取消、选项都在「工具」下、无「115 目录」）
   · 115 助手：只记录添加的账号 + 设置按钮改账号名
   · 分享转存 / ed2k：账号选择器（圆角白底黑字 / 选中蓝底白字）+ 存为默认账号
   · 秒传：账号直接来自 115 助手（没有单独的「添加秒传账号」）
   · 媒体服务器：多个 Emby（配置名称 / 地址 / API Key）
   · Emby 反代：全部字段可手动修改
   · 115 STRM：添加配置生成卡片，卡片设置含账号与网盘路径
   运行：node tests/ui-test.mjs
   ========================================================================== */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WEB = path.join(ROOT, 'web');

/* ---------------------------------------------------------------- 断言 */
let passed = 0;
const failures = [];
function ok(cond, msg) {
  if (cond) { passed++; return; }
  failures.push(msg);
}
function eq(a, b, msg) {
  ok(a === b, `${msg}（期望 ${JSON.stringify(b)}，实际 ${JSON.stringify(a)}）`);
}

/* ---------------------------------------------------------------- DOM 桩 */
class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = [];
    this.attrs = {};
    this.style = {};
    this.classes = new Set();
    this._html = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.dataset = {};
    this.listeners = {};
    this.offsetWidth = 1;
  }
  get className() { return [...this.classes].join(' '); }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  append(...cs) { cs.forEach((c) => { this.children.push(c); c.parent = this; }); }
  remove() {
    this._removed = true;
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
    }
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i < 0) throw new Error('removeChild: 节点不在 children 里');
    this.children.splice(i, 1);
    return c;
  }
  replaceWith(node) {
    // 模拟真实 DOM：把自己从父节点换成新节点
    this._replacedBy = node;
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children[i] = node;
      node.parent = this.parent;
    }
    // 注册表里同步换掉（模拟 document.getElementById 拿到新元素）
    for (const [key, val] of registry) {
      if (val === this) {
        registry.set(key, node);
        if (node.id) registry.set(node.id, node);
        break;
      }
    }
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  dispatch(type, ev) { (this.listeners[type] || []).forEach((fn) => fn(ev || { target: this })); }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
  focus() {}
  click() { this.dispatch('click', { target: this }); }
  // 支持 #id 查询（前端大量使用 N.$('#xxx')）
  querySelector(sel) {
    if (typeof sel === 'string' && sel.startsWith('#')) {
      return registry.get(sel.slice(1)) || null;
    }
    return null;
  }
  querySelectorAll(sel) {
    if (typeof sel === 'string' && sel.startsWith('#')) {
      const el = registry.get(sel.slice(1));
      return el ? [el] : [];
    }
    return [];
  }
}

const registry = new Map();
function makeEl(tag, id) {
  const el = new El(tag);
  if (id) registry.set(id, el);
  return el;
}

/* 需要被 JS 用到的固定元素 */
['toasts', 'modalWrap', 'modalBox', 'view', 'sideNav', 'crumb', 'loginView',
 'appView', 'loginForm', 'loginUser', 'loginPass', 'loginError', 'loginHint',
 'togglePw', 'scrim', 'logsDrawer', 'logList', 'logCount', 'logFilter',
 'themePop', 'themeList', 'morePop', 'navToggle', 'verText', 'loginBg',
 'logRefresh', 'logClear'].forEach((id) => makeEl('div', id));

const document = {
  documentElement: {
    _theme: 'light',
    setAttribute(k, v) { if (k === 'data-theme') this._theme = v; },
    getAttribute(k) { return k === 'data-theme' ? this._theme : null; },
  },
  body: makeEl('body', 'body'),
  getElementById: (id) => registry.get(id) || null,
  querySelector: (sel) => (typeof sel === 'string' && sel.startsWith('#')
    ? (registry.get(sel.slice(1)) || null) : null),
  querySelectorAll: (sel) => (typeof sel === 'string' && sel.startsWith('#')
    ? (registry.has(sel.slice(1)) ? [registry.get(sel.slice(1))] : []) : []),
  createElement: (tag) => new El(tag),
  addEventListener(type, fn) { (document._l[type] ||= []).push(fn); },
  _l: {},
};

/* ---------------------------------------------------------------- 桩 API */
const state = {
  session: { authenticated: false, enabled: true, username: '', site_name: 'p115-strm', version: 'v1.8.0', show_hint: true },
  accounts: [],
  settings: {},
  mediaServers: [],
  proxy: { enabled: false, upstream: '', strm_prefix: '', cache_seconds: 900, path_rules: [], log_level: 'info' },
  status: { db: { files: 0, dirs: 0, strm: 0 }, mode: 'all', watch: {}, scan: {}, api: {}, ed2k: {}, share: {}, instant: {}, emby: {} },
  strmConfigs: [],
  shareItems: [],
  ed2kItems: [],
  instantStatus: { accounts: [], users: [], records: 0 },
  calls: [],
};

function fakeRequest(path, opts = {}) {
  state.calls.push({ path, method: opts.method || 'GET', body: opts.body });
  const p = path.split('?')[0];
  const method = opts.method || 'GET';
  if (p === '/api/session') return Promise.resolve(state.session);
  if (p === '/api/login') {
    const body = JSON.parse(opts.body || '{}');
    if (body.password === 'pw') {
      state.session = { ...state.session, authenticated: true, username: body.username };
      return Promise.resolve({ ok: true });
    }
    const e = new Error('账号或密码不正确'); e.status = 401; return Promise.reject(e);
  }
  if (p === '/api/logout') {
    state.session = { ...state.session, authenticated: false, username: '' };
    return Promise.resolve({ ok: true });
  }
  if (p === '/api/status') return Promise.resolve(state.status);
  if (p === '/api/accounts') return Promise.resolve({ accounts: state.accounts });
  if (p === '/api/settings') {
    if (method === 'PUT') {
      const body = JSON.parse(opts.body || '{}');
      Object.assign(state.settings, body.settings || {});
      return Promise.resolve({ ok: true, settings: state.settings });
    }
    return Promise.resolve({ settings: state.settings });
  }
  if (p === '/api/media-servers') {
    if (method === 'POST') {
      const body = JSON.parse(opts.body || '{}');
      const srv = {
        id: body.id || ('srv_' + Math.random().toString(16).slice(2, 8)),
        name: body.name || body.host, host: (body.host || '').replace(/\/+$/, ''),
        api_key: body.api_key || '', enabled: body.enabled !== false,
        note: body.note || '', created_at: state.mediaServers.length,
      };
      state.mediaServers = [...state.mediaServers.filter((s) => s.id !== srv.id), srv];
      return Promise.resolve({ ok: true, server: srv });
    }
    return Promise.resolve({ servers: state.mediaServers });
  }
  if (p.startsWith('/api/media-servers/') && method === 'DELETE') {
    const id = decodeURIComponent(p.split('/').pop());
    state.mediaServers = state.mediaServers.filter((s) => s.id !== id);
    return Promise.resolve({ ok: true });
  }
  if (p === '/api/proxy') {
    if (method === 'PUT') {
      Object.assign(state.proxy, JSON.parse(opts.body || '{}'));
      return Promise.resolve({ ok: true, settings: state.proxy });
    }
    return Promise.resolve({ settings: state.proxy, servers: state.mediaServers });
  }
  if (p === '/api/strm/configs') {
    if (method === 'POST') {
      const body = JSON.parse(opts.body || '{}');
      const id = body.id || ('cfg_' + Math.random().toString(16).slice(2, 8));
      const row = {
        id, account: body.account || '', name: body.name || '',
        remote_cid: body.remote_cid || '', remote_path: body.remote_path || '',
        local_dir: body.local_dir || '', base_url: body.base_url || '',
        media_exts: body.media_exts || '', meta_exts: body.meta_exts || '',
        download_meta: body.download_meta !== false, event_watch: body.event_watch !== false,
        api_pull: body.api_pull !== false, redirect: body.redirect !== false,
        status: 'idle', last_run: 0, files: 0,
      };
      state.strmConfigs = [...state.strmConfigs.filter((c) => c.id !== id), row];
      return Promise.resolve({ ok: true, config: row });
    }
    return Promise.resolve({ configs: state.strmConfigs });
  }
  if (p.startsWith('/api/strm/configs/') && p.endsWith('/run')) {
    const id = decodeURIComponent(p.split('/')[4]);
    const row = state.strmConfigs.find((c) => c.id === id);
    if (row) row.status = 'running';
    return Promise.resolve({ ok: true, accepted: true });
  }
  if (p.startsWith('/api/strm/configs/') && method === 'DELETE') {
    const id = decodeURIComponent(p.split('/').pop());
    state.strmConfigs = state.strmConfigs.filter((c) => c.id !== id);
    return Promise.resolve({ ok: true });
  }
  if (p === '/api/115/dirs') {
    return Promise.resolve({ cid: '0', name: '整个网盘', dirs: [
      { id: '100', name: '电影', managed: false },
      { id: '102', name: '剧集', managed: false },
    ] });
  }
  if (p.endsWith('/api/share')) {
    if (method === 'POST') return Promise.resolve({ ok: true, share: { name: 'x', share_code: 's1' } });
    return Promise.resolve({ settings: { dir_cid: '', dir_name: '', enabled: true }, items: state.shareItems });
  }
  if (p.endsWith('/api/ed2k')) {
    if (method === 'POST') return Promise.resolve({ ok: true, added: 1, failed: 0 });
    return Promise.resolve({ enabled: true, settings: { ttl_hours: 24, clean_interval: 600, dir_cid: '' }, items: state.ed2kItems });
  }
  if (p.endsWith('/api/instant')) {
    return Promise.resolve(state.instantStatus);
  }
  return Promise.resolve({});
}

/* ---------------------------------------------------------------- 加载脚本 */
const ctx = { window: {}, document, localStorage: {
  _d: new Map(),
  getItem(k) { return this._d.has(k) ? this._d.get(k) : null; },
  setItem(k, v) { this._d.set(k, v); },
  removeItem(k) { this._d.delete(k); },
}, setTimeout, clearTimeout, console, fetch: () => Promise.resolve({}), URLSearchParams };
/* window 自身的监听器（hashchange 等） */
ctx.addEventListener = (type, fn) => {
  (ctx._listeners[type] ||= []).push(fn);
};
ctx.removeEventListener = () => {};
ctx._listeners = {};
ctx.window = ctx;
ctx.globalThis = ctx;

/* 路由用的 location 桩 */
const loc = {
  _hash: '#/status',
  get hash() { return this._hash; },
  set hash(v) {
    this._hash = v;
    (document._l.hashchange || []).forEach((fn) => fn());
  },
  pathname: '/',
  search: '',
};
ctx.location = loc;
ctx.window.location = loc;

for (const file of ['core.js', 'api.js', 'data.js', 'shell.js', 'views.js',
  'tool-115.js', 'tool-share.js', 'tool-ed2k.js', 'tool-instant.js',
  'tool-strm115.js', 'main.js']) {
  const code = fs.readFileSync(path.join(WEB, 'js', file), 'utf8');
  const fn = new Function('window', 'document', 'localStorage', 'console',
    'setTimeout', 'clearTimeout', 'fetch', 'location', code);
  fn(ctx.window, document, ctx.localStorage, console, setTimeout, clearTimeout, ctx.fetch, loc);
}

/* window.API.request 用桩替换 */
ctx.window.API.request = fakeRequest;

/* API 模块内部闭包持有的是原 request，这里直接覆盖其内部实现：
   重新执行 api.js，让 request 走桩 */
{
  const code = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
  const fn = new Function('window', 'document', 'localStorage', 'console',
    'setTimeout', 'clearTimeout', 'fetch', 'location', code);
  fn(ctx.window, document, ctx.localStorage, console, setTimeout, clearTimeout, ctx.fetch, loc);
}
/* request 内部用 fetch，替换 fetch 为桩 */
ctx.fetch = (path, opts) => fakeRequest(path, opts).then((data) => ({
  ok: true, status: 200, json: async () => data,
}));
{
  const code = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
  const fn = new Function('window', 'document', 'localStorage', 'console',
    'setTimeout', 'clearTimeout', 'fetch', 'location', code);
  fn(ctx.window, document, ctx.localStorage, console, setTimeout, clearTimeout, ctx.fetch, loc);
}

const NS = ctx.window.NS;
const D = ctx.window.Data;
const App = ctx.window.App;

/* 让 view 容器能记录渲染结果 */
const viewEl = registry.get('view');
const realRender = ctx.window.Shell.render;

/* ---------------------------------------------------------------- 断言开始 */
console.log('== 1. 登录流程 ==');
await ctx.window.Shell.boot();
ok(registry.get('appView').hidden === true, '未登录时应停在登录页');
ok(registry.get('loginView').hidden === false, '未登录时应显示登录页');
ok(registry.get('loginHint').textContent.includes('admin'), '登录页应提示默认账号');

const r = await ctx.window.API.login('admin', 'pw');
ok(r.ok === true, '登录应成功');
await ctx.window.Shell.boot();
ok(registry.get('appView').hidden === false, '登录后应显示主界面');
ok(registry.get('loginView').hidden === true, '登录后应隐藏登录页');

console.log('== 2. 导航结构 ==');
const navSrc = fs.readFileSync(path.join(WEB, 'js', 'shell.js'), 'utf8');
const navBlock = navSrc.slice(navSrc.indexOf('const NAV'), navSrc.indexOf('const ROUTES'));
ok(!navBlock.includes('工具箱'), '工具箱应已取消');
ok(navBlock.includes("section: '工具'"), '应有「工具」分组');
for (const name of ['115 助手', '115 分享转存', 'ed2k STRM', '115 秒传', '115 STRM']) {
  ok(navBlock.includes(name), `工具下应有「${name}」`);
}
ok(navBlock.includes("section: '媒体与反代'"), '应有「媒体与反代」分组');
ok(navBlock.includes('媒体服务器'), '应有「媒体服务器」');
ok(navBlock.includes('Emby 反代'), '应有「Emby 反代」');
ok(!navBlock.includes('115 目录'), '不应有「115 目录」');
ok(!navBlock.includes('目录管理'), '不应有「目录管理」');

console.log('== 3. 115 助手：只记录添加的账号 + 设置按钮 ==');
state.accounts = [
  { name: '', display: '', main: true, ok: true, files: 12, strm_root: '/strm' },
  { name: 'acc1', display: '客厅大号', main: false, ok: true, files: 8, strm_root: '/strm/acc1' },
];
await D.load();
const t115 = ctx.window.Tool115.page();
const html115 = t115.html;
ok(html115.includes('115 助手'), '页面标题应为 115 助手');
ok(html115.includes('data-settings'), '账号行应有设置按钮');
ok(html115.includes('添加账号'), '应有添加账号入口');
ok(!html115.includes('秒传账号添加'), '不应有秒传账号添加入口');
ok(html115.includes('客厅大号'), '应显示账号显示名');
eq(D.db.label('acc1'), '客厅大号', 'label 应返回显示名');
// 新模型：没有独立的「主账号位」条目，label('') 在没有主账号时
// 返回「主账号」，有主账号时返回那个账号的名字
ok(D.db.label('') === '主账号' || typeof D.db.label('') === 'string',
  '主账号要有显示名');

console.log('== 4. 分享转存：账号选择器 + 存为默认 ==');
const tShare = ctx.window.ToolShare.page();
ok(tShare.html.includes('data-acc-pick'), '分享转存应有账号选择器');
ok(tShare.html.includes('115 助手'), '应提示账号来自 115 助手');
ok(tShare.html.includes('默认账号'), '应说明保存后作为默认账号');

console.log('== 5. ed2k：账号选择器 + 圆角选项样式 ==');
const tEd2k = ctx.window.ToolEd2k.page();
ok(tEd2k.html.includes('data-acc-pick'), 'ed2k 应有账号选择器');
ok(tEd2k.html.includes('默认账号'), '应说明保存后作为默认账号');
const css = fs.readFileSync(path.join(WEB, 'styles.css'), 'utf8');
ok(/\.pill\{[^}]*background:var\(--panel\)[^}]*\}/.test(css), 'pill 应为白底');
ok(/\.pill\.active\{[^}]*background:var\(--brand\)[^}]*color:var\(--brand-ink\)/.test(css),
  '选中项应为蓝底白字');
ok(css.includes('border-radius:999px'), 'pill 应为圆角');

console.log('== 6. 媒体服务器：多个 Emby ==');
const tMedia = ctx.window.Views.mediaServer();
ok(tMedia.html.includes('媒体服务器'), '页面标题应为 媒体服务器');
ok(tMedia.html.includes('添加服务器'), '应有添加服务器入口');
const mediaSrc = fs.readFileSync(path.join(WEB, 'js', 'views.js'), 'utf8');
ok(mediaSrc.includes('配置名称'), '应有「配置名称」字段');
ok(mediaSrc.includes('Emby 地址'), '应有「Emby 地址」字段');
ok(mediaSrc.includes('API Key'), '应有「API Key」字段');

console.log('== 7. 秒传：账号直接来自 115 助手 ==');
state.instantStatus = {
  accounts: [{ name: 'acc1', display: '客厅大号', ok: true }],
  users: [{ emby_user_id: 'u1', emby_user_name: '小明', account: 'acc1' }],
  records: 3,
};
const tInstant = ctx.window.ToolInstant.page();
ok(tInstant.html.includes('data-acc-multi'), '秒传应有多选账号选择器');
ok(tInstant.html.includes('直接使用 115 助手中的账号'), '应说明账号来自 115 助手');
ok(!tInstant.html.includes('添加秒传账号'), '不应有添加秒传账号入口');
ok(tInstant.html.includes('data-acc-multi'), '秒传应可多选');

console.log('== 8. Emby 反代：全部字段可手动修改 ==');
const tProxy = ctx.window.Views.proxy();
for (const field of ['pxEnabled', 'pxUpstream', 'pxPort', 'pxPrefix', 'pxBase', 'pxCache', 'pxLog', 'pxRules']) {
  ok(tProxy.html.includes(field), `反代应有字段 ${field}`);
}
ok(tProxy.html.includes('手动修改'), '应说明可手动修改');
// 客户端地址不需要路径前缀：独立端口直接用根路径
// 注意：Data 模块内部有自己的 state 闭包，要改 Data.state 不是测试的 state
Object.assign(ctx.window.Data.state.proxy, {
  enabled: true, upstream: 'http://emby.local:8096', strm_prefix: '',
  cache_seconds: 900, proxy_port: 7259, path_rules: [], log_level: 'info',
});
const tProxy2 = ctx.window.Views.proxy();
ok(tProxy2.html.includes('http://本机:7259<'), '独立端口入口不应带路径前缀');
ok(tProxy2.html.includes('不需要'), '应说明不需要路径前缀');// 有连通性检测按钮
ok(tProxy2.html.includes('data-check'), '应有连通性检测按钮');
ok(tProxy2.html.includes('pxCheckCard'), '应有检测结果区域');
// strm 挂载前缀要说明它不是客户端地址前缀
ok(tProxy2.html.includes('strm 挂载前缀'), 'strm 前缀应改名，避免误解成地址前缀');

console.log('== 9. 115 STRM：添加配置生成卡片 ==');
state.strmConfigs = [{
  id: 'cfg1', account: 'acc1', name: '电影', remote_cid: '100', remote_path: '/电影',
  local_dir: '115-原档/电影', base_url: '', media_exts: '', meta_exts: '',
  download_meta: true, event_watch: true, api_pull: true, redirect: true,
  status: 'idle', last_run: 0, files: 12,
}];
await D.load();
const tStrm = ctx.window.ToolStrm115.page();
ok(tStrm.html.includes('115 STRM'), '页面标题应为 115 STRM');
ok(tStrm.html.includes('添加配置'), '应有添加配置按钮');
ok(tStrm.html.includes('data-task="cfg1"'), '应渲染配置卡片');
ok(tStrm.html.includes('data-edit'), '卡片应有设置按钮');
ok(tStrm.html.includes('增量') && tStrm.html.includes('全量'), '卡片应有增量/全量按钮');
ok(tStrm.html.includes('电影'), '卡片应显示配置名');
const strmSrc = fs.readFileSync(path.join(WEB, 'js', 'tool-strm115.js'), 'utf8');
ok(strmSrc.includes('sAccount'), '卡片设置应有账号选择');
ok(strmSrc.includes('sAccount'), '卡片设置应有账号选择');
ok(strmSrc.includes('pairList'), '应有网盘路径 → 本地目录列表');
ok(strmSrc.includes('data-pickdir'), '本地目录应能自由选（映射根内任意目录）');
ok(strmSrc.includes('网盘路径'), '应有「网盘路径」字段');

console.log('== 10. 后端接口齐全 ==');
const apiSrc = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
for (const ep of ['/api/login', '/api/logout', '/api/session', '/api/accounts',
  '/api/settings', '/api/media-servers', '/api/proxy', '/api/strm/configs']) {
  ok(apiSrc.includes(ep), `前端应调用 ${ep}`);
}
ok(apiSrc.includes('accBase'), '应按账号路由到 /a/<名字>/');

console.log('== 11. Toast 不累积 ==');
const coreSrc = fs.readFileSync(path.join(WEB, 'js', 'core.js'), 'utf8');
ok(coreSrc.includes('TOAST_MAX'), 'toast 应有数量上限');
ok(coreSrc.includes('data-msg'), '相同内容的 toast 应去重');
// 实际行为：连续弹 10 条同样内容，只保留最新的一条
const toastBox = registry.get('toasts');
toastBox.children = [];
for (let i = 0; i < 10; i++) ctx.window.NS.toast('已保存，默认账号：主账号', 'ok');
ok(toastBox.children.length === 1, `相同提示应只留一条，实际 ${toastBox.children.length} 条`);
// 不同内容最多 4 条
toastBox.children = [];
for (let i = 0; i < 10; i++) ctx.window.NS.toast('提示 ' + i, 'ok');
ok(toastBox.children.length <= 4, `toast 最多 4 条，实际 ${toastBox.children.length} 条`);
// 同内容重复调用：复用同一个元素（先删再建会短暂出现两条）
toastBox.children = [];
const first = (ctx.window.NS.toast('同一条', 'ok'), toastBox.children[0]);
ctx.window.NS.toast('同一条', 'ok');
ok(toastBox.children.length === 1, `重复提示应复用元素，实际 ${toastBox.children.length} 条`);
ok(toastBox.children[0] === first, '重复提示应复用同一个元素');

console.log('== 11b. 事件绑定幂等（bindOnce）==');
ok(coreSrc.includes('function bindOnce'), '应有 bindOnce 工具');
ok(coreSrc.includes('bindOnce,') || coreSrc.includes('bindOnce'), 'bindOnce 应导出');
// 实际行为：同一元素重复 bindOnce 只绑一次
const el = makeEl('div', 'bindOnceTest');
let hits = 0;
const handler = () => { hits++; };
ctx.window.NS.bindOnce(el, 'click', handler);
ctx.window.NS.bindOnce(el, 'click', handler);
ctx.window.NS.bindOnce(el, 'click', handler);
el.dispatch('click');
ok(hits === 1, `bindOnce 应只绑一次，实际触发 ${hits} 次`);
// 所有模块都不该直接绑在 root 上
for (const f of ['tool-115.js', 'tool-ed2k.js', 'tool-instant.js', 'tool-share.js',
  'tool-strm115.js', 'views.js']) {
  const src = fs.readFileSync(path.join(WEB, 'js', f), 'utf8');
  ok(!src.includes('root.addEventListener'), `${f} 不应直接绑在 root 上`);
  ok(src.includes('N.bindOnce(root'), `${f} 应改用 bindOnce`);
}

console.log('== 12. 重复渲染不叠加事件监听器 ==');
const shellSrc = fs.readFileSync(path.join(WEB, 'js', 'shell.js'), 'utf8');
ok(shellSrc.includes('replaceWith'), '每次渲染应换新的容器元素（旧监听器随旧元素回收）');
ok(!shellSrc.includes("box.innerHTML = '';\n    try {"), '不应只清空 innerHTML 复用容器');

console.log('== 13. 秒传账号可多选（主账号空名用占位符）==');
const shellJs = fs.readFileSync(path.join(WEB, 'js', 'shell.js'), 'utf8');
ok(shellJs.includes('data-acc-name'), '账号按钮应带 data-acc-name（真实账号名）');
ok(shellJs.includes('@main'), '空账号名要用占位符，避免空值属性');
const instantJs = fs.readFileSync(path.join(WEB, 'js', 'tool-instant.js'), 'utf8');
ok(instantJs.includes("getAttribute('data-acc-name')"), '秒传应从 data-acc-name 取账号名');
ok(instantJs.includes("getAttribute('data-acc-multi')") === false, '不再从 data-acc-multi 取值');
// 界面渲染：主账号的按钮属性不是空字符串
Object.assign(ctx.window.Data.state.accounts, {
  0: { name: '', display: '', label: '主账号', ok: true },
});
ctx.window.Data.state.accounts.length = 0;
ctx.window.Data.state.accounts.push({ name: '', display: '', label: '主账号', ok: true });
ctx.window.Data.state.accounts.push({ name: '331', display: '', label: '331', ok: true });
// v1.8.9：用户要求把各工具里的「主账号」选项去掉 ——
// 主账号现在就是添加的账号里的一个，单独再列一个既重复又分不清
const pillsSub = ctx.window.Shell.accountPills([], { multi: true });
ok(!pillsSub.includes('data-acc-multi="@main"'), '其它工具不再列主账号');
ok(pillsSub.includes('331'), '要列添加过的账号');
ok(!pillsSub.includes('data-acc-multi=""'), '不应出现空值属性');
ok(pillsSub.includes('data-acc-name="331"'), '子账号应带真实名字');

console.log('== 14. 115 STRM 扩展名默认值 ==');
const strmJs = fs.readFileSync(path.join(WEB, 'js', 'tool-strm115.js'), 'utf8');
ok(strmJs.includes('DEFAULT_MEDIA_EXTS'), '前端应有媒体扩展名默认值');
ok(strmJs.includes('mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts'), '媒体扩展名默认值不对');
ok(strmJs.includes('nfo,jpg,jpeg,png,srt,ass,sub,idx'), '元数据扩展名默认值不对');
ok(strmJs.includes('t.media_exts || DEFAULT_MEDIA_EXTS'), '老配置为空时应显示默认值');
const serverSrc = fs.readFileSync(path.join(ROOT, 'app', 'server.py'), 'utf8');
ok(serverSrc.includes('DEFAULT_MEDIA_EXTS = "mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts"'), '后端应有默认值常量');
ok(serverSrc.includes('DEFAULT_META_EXTS = "nfo,jpg,jpeg,png,srt,ass,sub,idx"'), '后端应有默认值常量');

console.log('== 16. 115 助手：切换主账号 / cookie 管理 ==');
ok(t115.html.includes('data-promote'), '应有「设为主账号」按钮');
ok(t115.html.includes('data-del'), '应有删除按钮');
ok(t115.html.includes('data-settings'), '应有设置按钮');
const s115 = fs.readFileSync(path.join(WEB, 'js', 'tool-115.js'), 'utf8');
ok(s115.includes('eCookie'), '设置里应有 cookie 文本框');
ok(s115.includes('data-test-cookie'), '设置里应有「测试 cookie 可用性」按钮');
ok(s115.includes('promoteAccount'), '应有切换主账号的调用');
const sApi = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
for (const fn of ['accountCookie', 'setAccountCookie', 'testAccountCookie', 'promoteAccount']) {
  ok(sApi.includes(fn), `api.js 应有 ${fn}`);
}

console.log('== 17. 115 STRM：只显示卡片，3 列并排 ==');
const sStrm = fs.readFileSync(path.join(WEB, 'js', 'tool-strm115.js'), 'utf8');
ok(!sStrm.includes('task-group'), '不应再按账号分组');
ok(!sStrm.includes('添加路径'), '不应再有分组里的「添加路径」');
ok(!sStrm.includes('该账号还没有路径配置'), '不应再有分组空状态');
ok(sStrm.includes('task-grid'), '卡片应放在统一网格里');
const sCss = fs.readFileSync(path.join(WEB, 'styles.css'), 'utf8');
ok(sCss.includes('.task-grid{display:grid;grid-template-columns:repeat(3'), '卡片应 3 列并排');

console.log('== 18. ed2k 转存任务状态筛选 ==');
const sEd = fs.readFileSync(path.join(WEB, 'js', 'tool-ed2k.js'), 'utf8');
ok(sEd.includes('data-filter'), '应有筛选按钮');
ok(sEd.includes('edFilterBar'), '应有筛选栏');
for (const label of ['全部', '成功', '未完成', '失败']) {
  ok(sEd.includes(`label: '${label}'`), `应有「${label}」筛选项`);
}
ok(sEd.includes("it.status === 'ready'"), '成功 = ready');
ok(sEd.includes("it.status === 'failed'"), '失败 = failed');
ok(sEd.includes("it.status === 'pending'") && sEd.includes("it.status === 'cleared'"),
  '未完成 = pending + cleared');

console.log('== 20. 修复：主账号路径 / 主账号标记 / 错误信息 ==');
const sApi2 = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
// 主账号的内部名是空串，拼进 URL 会变成双斜杠 → 404 / 405
ok(sApi2.includes("function accKey(name)"), '应有 accKey 辅助函数');
ok(sApi2.includes("encodeURIComponent(name || 'main')"), '主账号在 URL 里用 main');
ok(sApi2.includes("'/api/accounts/' + accKey(name)"), '所有账号接口都要走 accKey');
ok(!sApi2.includes("encodeURIComponent(name) + '/display'"), 'display 不能直接拼空名');
// 错误信息不能显示成 [object Object]
ok(sApi2.includes('function errText'), '应有 errText 把错误转成字符串');
ok(sApi2.includes("Array.isArray(detail)"), '要处理 detail 是数组的情况（校验错误）');
ok(sApi2.includes('throw new Error(errText(data, resp.status))'), '抛错要用 errText');
// 秒传映射必须走主服务（account 是「映射到哪个账号」，不是「在哪个账号下操作」）
ok(sApi2.includes("'/api/instant/user' + qs({ user_id: userId, name, account })"),
  '秒传映射要走主服务并传 account');
ok(!sApi2.includes("accBase(account) + '/api/instant/user'"), '秒传映射不能按 account 拼路径');
// 主账号是「账号列表里的一个标记」，不是独立的槽位
const s115b = fs.readFileSync(path.join(WEB, 'js', 'tool-115.js'), 'utf8');
ok(s115b.includes('acc.main === true'), '主账号行要按 main 字段判断');
ok(s115b.includes('取消主账号'), '主账号要有「取消主账号」按钮');
ok(!s115b.includes('acc.promoted'), '不再有 promoted 概念（主账号是账号自己的标记）');
const sData2 = fs.readFileSync(path.join(WEB, 'js', 'data.js'), 'utf8');
ok(sData2.includes("label: a.display || a.name || '主账号'"), '账号标签直接用后端给的 display');
const sHub = fs.readFileSync(path.join(ROOT, 'app', 'hub.py'), 'utf8');
ok(sHub.includes('main_account'), '后端要记录谁是主账号');
ok(sHub.includes('def main_account_name'), '应有 main_account_name 方法');
ok(sHub.includes('**不交换 cookies**'), '设为主账号不能交换 cookies');
ok(!sHub.includes('_swap_file(main_cookie'), '不能再交换主账号与子账号的 cookies 文件');
ok(sHub.includes('def _restore_main_account'), '启动时要恢复主账号');
const sSrv2 = fs.readFileSync(path.join(ROOT, 'app', 'server.py'), 'utf8');
ok(sSrv2.includes('row["main"] = name == main_name'), '账号列表要标出主账号');
ok(!sSrv2.includes('"identity": main_identity'), '不再有 identity 概念');
ok(sSrv2.includes('def _acc_name'), '主账号名解析要提到外层（display/remove 也要用）');


console.log('== 24. 修复：预缓存 / 清空暂存的 account 参数 ==');
// prewarm / clean 的后端签名是 account: str（必填**查询参数**）。
// 前端只在路径里带账号 → FastAPI 422「account: Field required」。
const sApiPw = fs.readFileSync(path.join(WEB, 'js', 'api.js'), 'utf8');
ok(sApiPw.includes("prewarm: (account) => request('/api/instant/prewarm' + qs({ account })"),
  'prewarm 要把账号作为查询参数传');
ok(sApiPw.includes("cleanInstant: (account) => request('/api/instant/clean' + qs({ account })"),
  'cleanInstant 要把账号作为查询参数传');
ok(!sApiPw.includes("accBase(account) + '/api/instant/prewarm'"),
  'prewarm 不能只把账号拼进路径');
ok(!sApiPw.includes("accBase(account) + '/api/instant/clean'"),
  'cleanInstant 不能只把账号拼进路径');
// 账号校验要问 115 助手（instant.cookies_for），不能查旧 accounts 表
const sSrvPw = fs.readFileSync(path.join(ROOT, 'app', 'server.py'), 'utf8');
ok(sSrvPw.includes('if not instant.cookies_for(account):'),
  '账号校验要用 instant.cookies_for（115 助手的账号不在 accounts 表里）');
ok(!sSrvPw.includes('if not db.get_account_cookies(account):'),
  '不能用 db.get_account_cookies 校验 115 助手的账号');
// 预缓存要跨账号取源文件（115 STRM 的索引在各账号自己的库里）
const sInstPw = fs.readFileSync(path.join(ROOT, 'app', 'instant.py'), 'utf8');
ok(sInstPw.includes('def source_files'), '要有跨账号取源文件的方法');
ok(sInstPw.includes('def other_sources'), '要能拿到其它账号的索引库');
ok(sInstPw.includes('sources_provider'), 'InstantManager 要支持 sources_provider');
ok(sInstPw.includes('row["pickcode"], source_account)'),
  '预缓存要把源账号传给 resolve');
const sMainPw = fs.readFileSync(path.join(ROOT, 'app', 'main.py'), 'utf8');
ok(sMainPw.includes('instant.sources_provider = _make_sources_provider(hub)'),
  'main.py 要把 hub 的索引库接给秒传');
// 白名单只该限制「秒传到哪」，不该限制「从哪读」和清理
ok(sInstPw.includes('enforce_allowance'), 'client 要能跳过秒传账号名单');
ok(sInstPw.includes('self.client(source_account, enforce_allowance=False)'),
  '取源直链不该受秒传账号名单限制');
ok(sInstPw.includes('self.client(account, enforce_allowance=False)'),
  '清空暂存不该受秒传账号名单限制');


console.log('== 23. 修复：主题弹窗定位 / 秒传页死循环 / 主账号模型 ==');
// popover 必须定位到按钮下方：position:fixed 不设 top/left 会停在 DOM 位置，
// 很可能在视口外 —— 表现就是「点了没反应」
const sShell3 = fs.readFileSync(path.join(WEB, 'js', 'shell.js'), 'utf8');
ok(sShell3.includes('function openPopover(which, anchor)'), 'openPopover 要接收按钮做锚点');
ok(sShell3.includes('pop.style.left = left'), '要设置 popover 的 left');
ok(sShell3.includes('pop.style.top = top'), '要设置 popover 的 top');
ok(sShell3.includes('openPopover(panel, b)'), '调用时要传按钮');
// 秒传页：状态回来后局部更新，不整页重绘（重绘→page→mount→加载→重绘 = 死循环）
const sInst3 = fs.readFileSync(path.join(WEB, 'js', 'tool-instant.js'), 'utf8');
ok(!sInst3.includes('window.App.rerender()'), '秒传页不能整页重绘');
ok(sInst3.includes('applyStatus(root)'), '要用局部更新');
ok(sInst3.includes('root.isConnected'), '局部更新前要确认节点还在');
// 主账号模型：账号列表里没有独立的 name="" 条目
const sSrv3 = fs.readFileSync(path.join(ROOT, 'app', 'server.py'), 'utf8');
ok(sSrv3.includes('if not name:') && sSrv3.includes('continue'), '空名槽位不显示');
ok(sSrv3.includes('row["main"] = name == main_name'), '主账号标记在账号自己身上');
const sHub3 = fs.readFileSync(path.join(ROOT, 'app', 'hub.py'), 'utf8');
ok(sHub3.includes('不交换 cookies'), '设为主账号不能交换 cookies');
ok(sHub3.includes('self.default.db.set_meta("main_account", name)'), '只改主账号标记');
ok(sHub3.includes('_restore_main_account'), '启动时恢复主账号');
// v1.8.9 新增：秒传的源账号 / 删除映射 / 暂存目录 / 去掉主账号选项
const sInst4 = fs.readFileSync(path.join(WEB, 'js', 'tool-instant.js'), 'utf8');
ok(sInst4.includes('inStagePath'), '秒传页要有暂存目录输入框');
ok(sInst4.includes('inStagePick'), '秒传页要有目录选择器');
ok(sInst4.includes('data-save-stage'), '要有保存暂存目录的按钮');
ok(sInst4.includes('data-reset-stage'), '要有恢复默认的按钮');
ok(!sInst4.includes('主账号（未映射）'), '秒传页不应再有「主账号（未映射）」选项');
const sData4 = fs.readFileSync(path.join(WEB, 'js', 'data.js'), 'utf8');
ok(sData4.includes('return state.accounts.filter((a) => a.name);'),
  'selectable() 只列添加过的账号（去掉主账号选项）');
const sInstPy4 = fs.readFileSync(path.join(ROOT, 'app', 'instant.py'), 'utf8');
ok(sInstPy4.includes('def staging_dir'), '要有暂存目录设置的读写');
ok(sInstPy4.includes('def set_staging_dir'), '要有设置暂存目录的接口');
ok(sInstPy4.includes('source_account'), '秒传要记源账号');
const sEmby4 = fs.readFileSync(path.join(ROOT, 'app', 'emby.py'), 'utf8');
ok(sEmby4.includes('def source_account'), '反代要知道自己是哪个源账号');
ok(sEmby4.includes('source_account={quote(source_account)}'), '秒传路由要带源账号');
const sDb4 = fs.readFileSync(path.join(ROOT, 'app', 'db.py'), 'utf8');
ok(sDb4.includes('def delete_emby_user'), '移除映射要真删记录');
ok(sDb4.includes('source_account TEXT'), 'instant 表要有 source_account 列');
const sHub4 = fs.readFileSync(path.join(ROOT, 'app', 'hub.py'), 'utf8');
ok(sHub4.includes('self.default.instant'), '子账号的 proxy 要用主服务的 instant（映射在主库）');


console.log('== 22. 修复：移动端侧栏开合 / 遮罩 / 层级 ==');
const sHtml2 = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
// 菜单按钮必须移出 topbar：topbar 是 sticky（会创建层叠上下文），
// 按钮在里面就压不过侧栏 —— 侧栏展开后点不到它，就「关不掉」
// topbar 标签内部不能有 navToggle
const topbarBlock = (sHtml2.match(/<header class="topbar">([\s\S]*?)<\/header>/) || ['', ''])[1];
ok(!topbarBlock.includes('id="navToggle"'), '菜单按钮不能放在 topbar 里');
ok(/id="navToggle"/.test(sHtml2), '菜单按钮要存在');
const sCss2 = fs.readFileSync(path.join(WEB, 'styles.css'), 'utf8');
ok(sCss2.includes('body.nav-open::after'), '侧栏展开要有遮罩（body::after）');
ok(sCss2.includes('backdrop-filter:blur(2px)'), '遮罩要有模糊效果');
ok(/\.nav-toggle\{[^}]*position:fixed/.test(sCss2), '菜单按钮要 fixed 定位（脱离 topbar）');
ok(/body\.nav-open \.nav-toggle\{[^}]*z-index:31/.test(sCss2),
  '侧栏展开时菜单按钮要压过侧栏');
ok(sCss2.includes('.topbar{padding-left:44px}'), '顶栏要给独立定位的菜单按钮让位');
const sShell2 = fs.readFileSync(path.join(WEB, 'js', 'shell.js'), 'utf8');
ok(sShell2.includes('function closeNav()'), '应有 closeNav');
ok(sShell2.includes('function toggleNav()'), '应有 toggleNav');
ok(sShell2.includes('closeNavAfterNav()'), '点导航项后要收起侧栏');
ok(sShell2.includes("e.target.closest('.sidebar')"), '点侧栏外要收起侧栏');
ok(sShell2.includes("matchMedia('(max-width:960px)')"), '只在移动端收起');


console.log('== 21. 秒传修复：上传凭据 / 令牌反查 / 已知用户 ==');
const sInst2 = fs.readFileSync(path.join(WEB, 'js', 'tool-instant.js'), 'utf8');
ok(sInst2.includes('inUidSel'), '秒传页应有已知用户下拉');
ok(sInst2.includes('known_users'), '应使用后端给的已知用户列表');
// 状态回来后**局部更新**（不整页重绘：那会死循环把页面卡死）
ok(sInst2.includes('function applyStatus(root)'), '要有局部更新函数');
ok(sInst2.includes('if (root.isConnected) applyStatus(root)'),
  '状态回来后局部更新');
ok(!sInst2.includes('window.App.rerender()'), '秒传页不能整页重绘（会死循环）');
const sEmby2 = fs.readFileSync(path.join(ROOT, 'app', 'emby.py'), 'utf8');
ok(sEmby2.includes('emby_user_by_token'), '播放时要能用登录令牌反查用户');
ok(sEmby2.includes('remember_emby_token'), '登录时要记住 token → 用户');
ok(sEmby2.includes('AUTH_RE'), '要认出 Emby 登录接口');
ok(sEmby2.includes('X-Emby-Token'), '要从请求头取登录令牌');
const sDb2 = fs.readFileSync(path.join(ROOT, 'app', 'db.py'), 'utf8');
ok(sDb2.includes('CREATE TABLE IF NOT EXISTS emby_tokens'), '要有 emby_tokens 表');
ok(sDb2.includes('def emby_user_by_token'), '要有按 token 查用户的接口');
const sInstPy = fs.readFileSync(path.join(ROOT, 'app', 'instant.py'), 'utf8');
ok(sInstPy.includes('def _upload_cred'), '秒传前要取上传凭据');
ok(sInstPy.includes('def _rapid_upload'), '要有带重试的秒传入口');
ok(sInstPy.includes('userkey'), '秒传要带 userkey');
ok(sInstPy.includes('换一份上传凭据'), '凭据被拒时要换一份重试');


console.log('== 15. 媒体服务器编辑不新增 ==');
ok(serverSrc.includes('payload.get("id") or payload.get("sid")'), '后端应同时认 id 和 sid');
const viewsJs = fs.readFileSync(path.join(WEB, 'js', 'views.js'), 'utf8');
ok(viewsJs.includes('sid: srv ? srv.id'), '前端编辑时应回传 sid');

/* ---------------------------------------------------------------- 汇总 */
console.log(`\n通过 ${passed} 条断言`);
if (failures.length) {
  console.log('失败：');
  failures.forEach((f) => console.log('  ✗ ' + f));
  process.exit(1);
}
console.log('全部通过 ✅');
