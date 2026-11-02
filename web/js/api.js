/* ==========================================================================
   api.js · 与后端交互（登录 / 账号 / 配置 / 运行状态）
   --------------------------------------------------------------------------
   · 所有请求带会话 Cookie；401 自动回到登录页。
   · 按账号操作时，请求打到该账号的管理端点：
       主账号 → /
       子账号 → /a/<账号名>/
   ========================================================================== */
(function () {
  'use strict';

  const N = window.NS;

  /** 把后端返回的错误信息转成可读字符串。

  FastAPI 的 ``detail`` 可能是字符串，也可能是对象/数组（校验错误就是
  ``[{loc, msg, type}]``）。直接丢给 toast 会显示成 ``[object Object]``，
  这里统一转成能看懂的话。
   */
  function errText(data, status) {
    const detail = data && data.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      const parts = detail.map((d) => {
        if (typeof d === 'string') return d;
        if (d && typeof d === 'object') {
          const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : '';
          return (field ? field + ': ' : '') + (d.msg || d.type || JSON.stringify(d));
        }
        return String(d);
      });
      return parts.join('；');
    }
    if (detail && typeof detail === 'object') {
      const msg = detail.msg || detail.message || detail.error;
      if (typeof msg === 'string') return msg;
      try { return JSON.stringify(detail); } catch (e) { return String(detail); }
    }
    return 'HTTP ' + status;
  }

  async function request(path, opts) {
    opts = opts || {};
    const init = {
      method: opts.method || 'GET',
      headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
      credentials: 'same-origin'
    };
    if (opts.body !== undefined) {
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
    const resp = await fetch(path, init);
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (resp.status === 401) {
      if (window.App) window.App.onUnauthorized();
      throw new Error(errText(data, resp.status));
    }
    if (!resp.ok) throw new Error(errText(data, resp.status));
    return data;
  }

  const qs = (params) => {
    const p = new URLSearchParams();
    Object.keys(params || {}).forEach((k) => {
      const v = params[k];
      if (v === undefined || v === null || v === '') return;
      p.set(k, v);
    });
    const s = p.toString();
    return s ? '?' + s : '';
  };

  /** 账号对应的 API 前缀：主账号是根，子账号挂在 /a/<名字>/ 下 */
  function accBase(name) {
    if (!name) return '';
    return '/a/' + encodeURIComponent(name);
  }

  /** 账号在 URL 里的标识：主账号内部名是空串，一律用 main（空串会拼出双斜杠路径）。 */
  function accKey(name) {
    return encodeURIComponent(name || 'main');
  }

  const API = {
    request, qs, accBase,

    /* ---------------------------------------------------------- 登录 */
    session: () => request('/api/session'),
    login: (username, password) => request('/api/login', {
      method: 'POST', body: { username, password }
    }),
    logout: () => request('/api/logout', { method: 'POST' }),
    // 登录页背景：随机一张 TMDB 热门影片剧照（没配 key 时返回 ok:false）
    loginBackdrop: () => request('/api/login/backdrop'),
    // 登录页背景配置（TMDB key / 代理 / 自定义直链）—— 保存即生效
    backdropSettings: () => request('/api/login/backdrop/settings'),
    saveBackdropSettings: (payload) => request('/api/login/backdrop/settings', {
      method: 'PUT', body: payload
    }),
    testBackdrop: () => request('/api/login/backdrop/test', { method: 'POST' }),

    /* ---------------------------------------------------------- 状态 */
    status: () => request('/api/status'),

    /* ---------------------------------------------------------- 账号（115 助手） */
    accounts: () => request('/api/accounts'),
    addAccount: (name, cookies) => request('/api/accounts' + qs({ name, cookies }), {
      method: 'POST'
    }),
    // 主账号的内部名是空串，拼进 URL 会变成 /api/accounts//xxx：
    // 路由匹配不到（404），DELETE 更会 405。一律用 main 代表主账号。
    removeAccount: (name) => request('/api/accounts/' + accKey(name), {
      method: 'DELETE'
    }),
    renameAccount: (name, display) => request(
      '/api/accounts/' + accKey(name) + '/display' + qs({ display }), {
        method: 'PUT'
      }),
    accountCookie: (name) => request('/api/accounts/' + accKey(name) + '/cookie'),
    setAccountCookie: (name, cookies) => request(
      '/api/accounts/' + accKey(name) + '/cookie',
      { method: 'PUT', body: { cookies } }),
    testAccountCookie: (name, cookies) => request(
      '/api/accounts/' + accKey(name) + '/test-cookie',
      { method: 'POST', body: { cookies: cookies || '' } }),

    /* 同播复制（账号级）：这个账号的所有播放都按它判断 */
    accountCopies: (name) => request('/api/accounts/' + accKey(name) + '/copies'),
    saveAccountCopies: (name, payload) => request(
      '/api/accounts/' + accKey(name) + '/copies' + qs(payload), { method: 'POST' }),
    cleanCopies: (name, force) => request(
      '/api/accounts/' + accKey(name) + '/copies/clean' + qs({ force: force ? 1 : 0 }),
      { method: 'POST' }),
    // 「设为主账号」已移除：默认引擎固定用配置文件里那个账号，
    // 各工具页自己选要操作的账号，所以这里不再有 promote 接口。

    /* ---------------------------------------------------------- 界面设置 */
    settings: () => request('/api/settings'),
    saveSettings: (settings) => request('/api/settings', {
      method: 'PUT', body: { settings }
    }),

    /* ---------------------------------------------------------- 媒体服务器 */
    mediaServers: () => request('/api/media-servers'),
    saveMediaServer: (row) => request('/api/media-servers', {
      method: 'POST', body: row
    }),
    removeMediaServer: (sid) => request('/api/media-servers/' + encodeURIComponent(sid), {
      method: 'DELETE'
    }),

    /* ---------------------------------------------------------- Emby 反代 */
    proxy: () => request('/api/proxy'),
    saveProxy: (settings) => request('/api/proxy', {
      method: 'PUT', body: settings
    }),
    checkProxy: () => request('/api/proxy/check'),

    /* ---------------------------------------------------------- 115 STRM 配置 */
    strmConfigs: () => request('/api/strm/configs'),
    saveStrmConfig: (row) => request('/api/strm/configs', {
      method: 'POST', body: row
    }),
    removeStrmConfig: (id) => request('/api/strm/configs/' + encodeURIComponent(id), {
      method: 'DELETE'
    }),
    runStrmConfig: (id, mode) => request(
      '/api/strm/configs/' + encodeURIComponent(id) + '/run', {
        method: 'POST', body: { mode: mode }
      }),

    /* ---------------------------------------------------------- 115 目录浏览 */
    dirs: (cid, account) => request(accBase(account) + '/api/115/dirs' + qs({ cid })),
    // 本地映射目录（容器内 STRM_ROOT）：给「本地 STRM 目录」选择器用
    localDirs: (path) => request('/api/local/dirs' + qs({ path })),

    /* ---------------------------------------------------------- 115 STRM 事件日志 */
    strmEvents: (limit, configId) => request(
      '/api/strm/events' + qs({ limit: limit || 100, config_id: configId || '' })),
    clearStrmEvents: (configId) => request(
      '/api/strm/events' + qs({ config_id: configId || '' }), { method: 'DELETE' }),

    /* ---------------------------------------------------------- 分享转存 */
    share: (account) => request(accBase(account) + '/api/share'),
    addShare: (url, account, direct) => request(
      accBase(account) + '/api/share' + qs({ url, direct: direct ? 1 : 0 }), {
        method: 'POST'
      }),
    shareSettings: (account, dirCid, strmDir, dirPath) => request(
      accBase(account) + '/api/share/settings' +
      qs({ dir_cid: dirCid, strm_dir: strmDir, dir_path: dirPath }), { method: 'POST' }),
    autoShareDir: (account) => request(accBase(account) + '/api/share/auto-dir', {
      method: 'POST'
    }),
    removeShare: (code, account) => request(
      accBase(account) + '/api/share/' + encodeURIComponent(code), { method: 'DELETE' }),

    /* ---------------------------------------------------------- ed2k */
    ed2k: (account) => request(accBase(account) + '/api/ed2k'),
    addEd2k: (urls, account) => request(accBase(account) + '/api/ed2k/batch' + qs({ urls }), {
      method: 'POST'
    }),
    ed2kSettings: (account, payload) => request(
      accBase(account) + '/api/ed2k/settings' + qs(payload), { method: 'POST' }),
    autoEd2kDir: (account) => request(accBase(account) + '/api/ed2k/auto-dir', {
      method: 'POST'
    }),
    cleanEd2k: (account, force) => request(
      accBase(account) + '/api/ed2k/clean' + qs({ force: force ? 1 : 0 }), { method: 'POST' }),
    removeEd2k: (key, account) => request(
      accBase(account) + '/api/ed2k/' + encodeURIComponent(key), { method: 'DELETE' }),
    // 换设备用：把任务 + 配置导成 JSON / 把 JSON 导回来
    exportEd2k: (account, includeFiles) => request(
      accBase(account) + '/api/ed2k/export'
      + qs({ include_files: includeFiles === false ? 0 : 1 })),
    importEd2k: (payload, mode, account) => request(
      accBase(account) + '/api/ed2k/import' + qs({ mode: mode || 'merge' }),
      { method: 'POST', body: payload }),

    /* ---------------------------------------------------------- 秒传 */
    // 状态也**必须走主服务**：用户映射表、暂存目录设置、校验缓存都只存在主库，
    // 请求子账号路径 /api/a/<名字>/instant 会 404（子服务没注册这条），
    // 前端就只能一直用初始空状态 —— 表现为「映射保存了但列表还是空」
    // 「暂存目录改了、顶部还显示默认目录」。
    instant: (account, refresh) => request(
      '/api/instant' + qs({ refresh: refresh ? 1 : 0 })),
    // Emby 用户映射存在**主库**里，接口必须走主服务 ——
    // account 是「映射到哪个秒传账号」，不是「在哪个账号下操作」
    setInstantUser: (userId, name, account) => request(
      '/api/instant/user' + qs({ user_id: userId, name, account }), { method: 'POST' }),
    removeInstantUser: (userId, account) => request(
      '/api/instant/user/' + encodeURIComponent(userId), { method: 'DELETE' }),
    // 秒传暂存目录：path 与 cid 给一个即可，都不给＝恢复默认
    setInstantStaging: (account, path, cid) => request(
      '/api/instant/staging' + qs({ account, path, cid }), { method: 'POST' }),
    // 预缓存 / 清空暂存：**走主服务**并把账号作为查询参数传。
    // ① 后端签名是 account: str（必填查询参数），只在路径里带账号会 422
    //    「account: Field required」；
    // ② 子账号路径 /a/<名字>/ 用的是子账号自己的 instant 实例 —— 它没有
    //    接 115 助手的账号/cookies，拿不到别的账号的凭据。
    prewarm: (account) => request('/api/instant/prewarm' + qs({ account }), {
      method: 'POST'
    }),
    cleanInstant: (account) => request('/api/instant/clean' + qs({ account }), {
      method: 'POST'
    }),
    // 秒传校验缓存：占用情况 / 清空
    // 这一层缓存的是「115 要的那段字节」，第二个用户播同一部片子时靠它
    // 省掉整个源盘读取。清掉最坏是下次多读一次，不会影响能播的文件。
    instantDigest: () => request('/api/instant/digest'),
    clearInstantDigest: () => request('/api/instant/digest', { method: 'DELETE' }),
    sweepInstantDigest: () => request('/api/instant/digest/sweep', { method: 'POST' }),

    /* ---------------------------------------------------------- 天翼云盘（189） */
    // 多账号：所有接口都挂在主服务上，账号用 ?account=<id> / 请求体传，
    // 播放地址里也带账号（/p189/play/<账号>/<fileId>）
    cloud189: () => request('/api/cloud189'),
    cloud189Accounts: () => request('/api/cloud189/accounts'),
    cloud189LoginStart: () => request('/api/cloud189/login/start', { method: 'POST' }),
    // account 为空 = 新建账号；非空 = 给这个账号换 token（重新登录）
    cloud189LoginPoll: (uuid, account) => request(
      '/api/cloud189/login/poll' + qs({ uuid, account: account || '' })),
    cloud189SetToken: (token, expiresIn, account) => request('/api/cloud189/token', {
      method: 'POST', body: { token, expires_in: expiresIn || 0, account: account || '' }
    }),
    cloud189SetCookie: (cookie, account) => request('/api/cloud189/cookie', {
      method: 'POST', body: { cookie, account: account || '' }
    }),
    cloud189Logout: (account) => request(
      '/api/cloud189/logout' + qs({ account: account || '' }), { method: 'POST' }),
    cloud189AccountLogout: (id) => request(
      '/api/cloud189/accounts/' + encodeURIComponent(id) + '/logout',
      { method: 'POST' }),
    cloud189SetDefault: (id) => request(
      '/api/cloud189/accounts/' + encodeURIComponent(id) + '/default',
      { method: 'POST' }),
    cloud189Refresh: (id) => request(
      '/api/cloud189/accounts/' + encodeURIComponent(id) + '/refresh',
      { method: 'POST' }),
    cloud189AutoRefresh: (id, enabled) => request(
      '/api/cloud189/accounts/' + encodeURIComponent(id) + '/auto-refresh',
      { method: 'POST', body: { enabled: !!enabled } }),
    cloud189DeleteAccount: (id, force) => request(
      '/api/cloud189/accounts/' + encodeURIComponent(id) + qs({ force: force ? 1 : 0 }),
      { method: 'DELETE' }),
    cloud189Dirs: (folderId, account) => request(
      '/api/cloud189/dirs' + qs({ folder_id: folderId || '-11', account: account || '' })),
    cloud189Configs: () => request('/api/cloud189/configs'),
    saveCloud189Config: (row) => request('/api/cloud189/configs', {
      method: 'POST', body: row
    }),
    removeCloud189Config: (id, purge) => request(
      '/api/cloud189/configs/' + encodeURIComponent(id) + qs({ purge: purge ? 1 : 0 }),
      { method: 'DELETE' }),
    runCloud189Config: (id, mode) => request(
      '/api/cloud189/configs/' + encodeURIComponent(id) + '/run', {
        method: 'POST', body: { mode: mode }
      }),
    cloud189Events: (limit) => request(
      '/api/cloud189/events' + qs({ limit: limit || 80 })),
    clearCloud189Events: () => request('/api/cloud189/events', { method: 'DELETE' }),

    /* ---------------------------------------------------------- 通知 / 联动 */
    notify: () => request('/api/notify'),
    saveNotify: (payload) => request('/api/notify/settings', {
      method: 'POST', body: payload
    }),
    testNotify: () => request('/api/notify/test', { method: 'POST' }),
    link: () => request('/api/link'),
    saveLink: (payload) => request('/api/link/settings', {
      method: 'POST', body: payload
    }),
    // background=1：后台跑一轮（删网盘文件要逐条调接口，可能十几秒）
    checkLink: (force, background) => request(
      '/api/link/check' + qs({
        force: force ? 1 : 0, background: background === false ? 0 : 1
      }), { method: 'POST' }),
    linkPending: (limit) => request('/api/link/pending' + qs({ limit: limit || 200 })),
    commitLink: () => request('/api/link/commit', { method: 'POST' }),

    /* ------------------------------------------------------ 企微可信 IP */
    wxip: () => request('/api/wxip'),
    saveWxip: (payload) => request('/api/wxip/settings', {
      method: 'POST', body: payload
    }),
    wxipLogin: () => request('/api/wxip/login', { method: 'POST' }),
    wxipUpdate: () => request('/api/wxip/update', { method: 'POST' }),
    wxipShot: () => request('/api/wxip/shot', { method: 'POST' }),
    wxipLogout: () => request('/api/wxip/logout', { method: 'POST' }),

    /* ---------------------------------------------------------- 插件 */
    plugins: () => request('/api/plugins'),
    pluginInstall: (id) => request('/api/plugins/' + encodeURIComponent(id) + '/install',
      { method: 'POST' }),
    pluginUninstall: (id, purge) => request('/api/plugins/' + encodeURIComponent(id) +
      '/uninstall' + qs({ purge: purge ? 1 : 0 }), { method: 'POST' }),
    pluginEnable: (id, enabled) => request(
      '/api/plugins/' + encodeURIComponent(id) + '/enable',
      { method: 'POST', body: { enabled: !!enabled } }),
    pluginLog: (id) => request('/api/plugins/' + encodeURIComponent(id) + '/log'),

    /* ---------------------------------------------------------- 扫描 */
    scan: (full) => request('/api/scan' + qs({ full: full ? 1 : 0 }), { method: 'POST' }),
    poll: () => request('/api/poll', { method: 'POST' })
  };

  window.API = API;
})();
