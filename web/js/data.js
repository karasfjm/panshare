/* ==========================================================================
   data.js · 数据仓库（账号 / 界面设置 / 媒体服务器 / 反代 / 日志）
   --------------------------------------------------------------------------
   服务端是唯一事实来源：账号、配置都存在后端，页面刷新不会丢。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS;

  const state = {
    accounts: [],
    settings: {},
    mediaServers: [],
    proxy: {},
    status: null,
    loaded: false
  };

  /* ------------------------------------------------------------ 仓库 */
  const db = {
    state,

    /* 账号：name 是内部标识（strm 目录 / URL 前缀），label 是页面上的显示名 */
    accounts() { return state.accounts; },

    account(name) {
      const hit = state.accounts.find((a) => a.name === name);
      if (hit) return hit;
      // name 为空 = 主账号位：返回当前主账号那个账号
      if (!name) {
        const main = state.accounts.find((a) => a.main);
        if (main) return main;
        return { name: '', display: '', label: '主账号', ok: true };
      }
      return null;
    },

    /** 账号的显示名：优先用用户改过的，否则用内部名。

    name 为空 = 主账号位。此时显示**当前主账号那个账号**的名字 ——
    用户在 115 助手里把谁设为主账号，这里就显示谁，
    免得选了「主账号」却不知道用的谁的凭据。
    */
    label(name) {
      if (!name) {
        const main = state.accounts.find((a) => a.main);
        if (main) return main.display || main.name;
        return '主账号';
      }
      const acc = db.account(name);
      if (!acc) return name;
      if (acc.display) return acc.display;
      return acc.name;
    },

    /** 供其它工具（分享转存 / ed2k / 秒传 / 115 STRM）选账号。

    只列添加过的账号 —— 用户要求把这几个工具里的「主账号」选项去掉：
    主账号现在就是这些账号里的一个（115 助手里标记的那个），
    单独再列一个「主账号」既重复又让人分不清用的是谁的凭据。
    */
    selectable() {
      return state.accounts.filter((a) => a.name);
    },

    /** 115 助手用：只列添加过的账号（不补「主账号」那一项） */
    allAccounts() { return state.accounts; },

    settings() { return state.settings; },

    /** 读取某个工具的默认账号（分享转存 / ed2k / 秒传 / 115 STRM）

    主账号设置已移除：空名（配置文件里那个账号）不再作为可选值出现，
    没保存过选择或保存的账号已被删除时，自动落到第一个账号。
    */
    defaultAccount(tool) {
      const key = 'default_account_' + tool;
      const name = state.settings[key];
      const list = db.selectable();
      if (name) {
        const hit = list.find((a) => a.name === String(name));
        if (hit) return hit.name;
      }
      return list.length ? list[0].name : '';
    },

    mediaServers() { return state.mediaServers; },

    proxy() { return state.proxy; },

    status() { return state.status; }
  };

  /* ------------------------------------------------------------ 日志 */
  const logs = {
    items: N.storeGet(N.K.logs, []),

    add(level, msg) {
      this.items.unshift({ level: level || 'info', msg: msg, t: N.stamp() });
      if (this.items.length > 300) this.items.length = 300;
      N.storeSet(N.K.logs, this.items);
    },

    clear() {
      this.items = [];
      N.storeSet(N.K.logs, []);
    },

    render(box, countEl, filterBox) {
      if (!box) return;
      const levels = [
        { id: 'all', name: '全部' },
        { id: 'info', name: '信息' },
        { id: 'warn', name: '警告' },
        { id: 'err', name: '错误' }
      ];
      if (filterBox) {
        filterBox.innerHTML = levels.map((l) => `
          <button class="filter-chip ${logs.filter === l.id ? 'active' : ''}"
            data-log-filter="${l.id}">${l.name}</button>`).join('');
        filterBox.querySelectorAll('[data-log-filter]').forEach((b) => {
          b.onclick = () => {
            logs.filter = b.getAttribute('data-log-filter');
            logs.render(box, countEl, filterBox);
          };
        });
      }
      const items = logs.filter === 'all'
        ? logs.items
        : logs.items.filter((x) => x.level === logs.filter);
      if (countEl) countEl.textContent = items.length + ' 条记录';
      if (!items.length) {
        box.innerHTML = '<div class="empty"><span class="empty-ico">'
          + N.icon('article', 20) + '</span>还没有日志</div>';
        return;
      }
      box.innerHTML = items.map((x) => `
        <div class="log-item ${x.level}">
          <span class="log-time">${N.esc(x.t)}</span>
          <span class="log-msg">${x.msg}</span>
        </div>`).join('');
    },

    filter: 'all'
  };

  /* ------------------------------------------------------------ 加载 */
  async function load() {
    const results = await Promise.allSettled([
      window.API.accounts(),
      window.API.settings(),
      window.API.mediaServers(),
      window.API.proxy(),
      window.API.status(),
      window.API.strmConfigs()
    ]);
    const val = (i, fallback) => (results[i].status === 'fulfilled' ? results[i].value : fallback);

    const acc = val(0, { accounts: [] });
    state.accounts = (acc.accounts || []).map((a) => Object.assign({}, a, {
      // 后端已经把主账号位的 display 换成占用者的名字了，这里直接用；
      // 没有 display 时退回账号名，再退回「主账号」
      label: a.display || a.name || '主账号'
    }));

    const st = val(1, { settings: {} });
    state.settings = st.settings || {};

    const ms = val(2, { servers: [] });
    state.mediaServers = ms.servers || [];

    const px = val(3, { settings: {}, servers: [] });
    state.proxy = px.settings || {};

    state.status = val(4, null);

    const cfgs = val(5, { configs: [] });
    state.strmConfigs = cfgs.configs || [];

    state.loaded = true;
  }

  window.Data = { db, logs, load, state };
})();
