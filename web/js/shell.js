/* ==========================================================================
   shell.js · 登录 / 侧边导航 / 路由 / 顶栏面板
   --------------------------------------------------------------------------
   导航结构：
     工具   → 115 助手 / 115 分享转存 / ed2k STRM / 115 秒传 / 115 STRM
     媒体与反代 → 媒体服务器 / Emby 反代
     系统   → 插件 / 通知与联动* / 企微可信IP*
   带 * 的是**插件**提供的页面：插件没装就没有这个入口，装了以后由插件自己的
   web.js 调 Shell.registerPlugin() 把页面塞进 NAV / ROUTES。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  const NAV = [
    {
      section: '',
      items: [
        { id: 'status', name: '运行状态', ico: 'grid', hash: '#/status' }
      ]
    },
    {
      section: '工具',
      items: [
        { id: 'p115', name: '115 助手', ico: 'user', hash: '#/tools/p115' },
        { id: 'share', name: '115 分享转存', ico: 'download', hash: '#/tools/share' },
        { id: 'ed2k', name: 'ed2k STRM', ico: 'zap', hash: '#/tools/ed2k' },
        { id: 'instant', name: '115 秒传', ico: 'shuffle', hash: '#/tools/instant' },
        { id: 'strm115', name: '115 STRM', ico: 'layers', hash: '#/tools/strm115' }
      ]
    },
    {
      section: '媒体与反代',
      items: [
        { id: 'media', name: '媒体服务器', ico: 'server', hash: '#/media' },
        { id: 'proxy', name: 'Emby 反代', ico: 'wifi', hash: '#/proxy' }
      ]
    },
    {
      section: '系统',
      items: [
        { id: 'plugins', name: '插件', ico: 'plug', hash: '#/plugins' }
      ]
    }
  ];

  const ROUTES = {
    '#/status': { view: () => window.Views.status(), crumb: ['运行状态'] },
    '#/tools/p115': { view: () => window.Tool115.page(), crumb: ['工具', '115 助手'] },
    '#/tools/share': { view: () => window.ToolShare.page(), crumb: ['工具', '115 分享转存'] },
    '#/tools/ed2k': { view: () => window.ToolEd2k.page(), crumb: ['工具', 'ed2k STRM'] },
    '#/tools/instant': { view: () => window.ToolInstant.page(), crumb: ['工具', '115 秒传'] },
    '#/tools/strm115': { view: () => window.ToolStrm115.page(), crumb: ['工具', '115 STRM'] },
    '#/plugins': { view: () => window.ToolPlugins.page(), crumb: ['系统', '插件'] },
    '#/media': { view: () => window.Views.mediaServer(), crumb: ['媒体与反代', '媒体服务器'] },
    '#/proxy': { view: () => window.Views.proxy(), crumb: ['媒体与反代', 'Emby 反代'] }
  };

  /* ------------------------------------------------------------ 插件注册 */
  /**
   * 插件把自己的页面挂进侧栏与路由表。
   *
   * 插件 JS 由 `/plugin-assets/<id>.js` 按需加载（见 loadPlugins），脚本末尾
   * 调这里注册。所以「卸载插件」之后脚本不会被下载，入口自然消失 —— 不需要
   * 在前端硬编码任何插件名单。
   */
  function registerPlugin(meta) {
    if (!meta || !meta.hash || typeof meta.view !== 'function') return;
    const section = meta.section || '插件';
    const item = {
      id: meta.id || meta.hash,
      name: meta.name || meta.hash,
      ico: meta.ico || 'box',
      hash: meta.hash
    };
    let group = NAV.filter((g) => g.section === section)[0];
    if (!group) {
      // 插件的分区默认排在「系统」之前（系统里固定有「插件」这一项）
      const at = Math.max(1, NAV.length - 1);
      group = { section: section, items: [] };
      NAV.splice(at, 0, group);
    }
    if (!group.items.some((x) => x.hash === meta.hash)) group.items.push(item);
    ROUTES[meta.hash] = { view: meta.view, crumb: [section, item.name] };
    renderNav();
  }

  /** 拉插件清单 → 给已启用的插件加载前端脚本。启动时调用一次。 */
  async function loadPlugins() {
    let rows = [];
    try {
      const data = await window.API.plugins();
      rows = (data && data.plugins) || [];
    } catch (e) {
      return rows;      // 后端没起来时不该挡住页面，插件入口先空着
    }
    const ver = N.$('#verText') ? N.$('#verText').textContent : '';
    // 顺序加载（不是 Promise.all）：插件各自往侧栏里 push，并发加载会让
    // 导航顺序每次刷新都不一样。清单是按目录名排好的，串行就稳定。
    for (const p of rows) {
      if (!p.enabled || !p.has_web) continue;
      await new Promise((done) => {
        const s = document.createElement('script');
        s.src = '/plugin-assets/' + encodeURIComponent(p.id) + '.js?v=' +
          encodeURIComponent(ver + '-' + p.version);
        s.onload = () => done();
        s.onerror = () => done();   // 单个插件脚本坏掉不影响别的
        document.head.appendChild(s);
      });
    }
    return rows;
  }


  /* ------------------------------------------------------------ 账号选择 */
  function accountPills(selected, opts) {
    // 只列添加过的账号（selectable() 里已排除主账号位）
    const accounts = D.db.selectable();
    if (!accounts.length) {
      return '<span class="form-hint">还没有账号，请先到「工具 → 115 助手」添加</span>';
    }
    const multi = opts && opts.multi;
    const chosen = Array.isArray(selected) ? selected : [selected];
    return accounts.map((a) => {
      const on = chosen.indexOf(a.name) >= 0;
      // 账号名为空时（历史数据 / 后端返回空名）用 @main 占位，
      // 避免生成 data-acc-multi="" 这种语义脆弱的属性值。
      const key = multi ? encodeAccKey(a.name) : a.name;
      return `<button type="button" class="pill ${on ? 'active' : ''}"
        data-${multi ? 'acc-multi' : 'acc-pick'}="${esc(key)}"
        data-acc-name="${esc(a.name)}">${icon('user', 12)} ${esc(a.label)}</button>`;
    }).join('');
  }

  /** 账号名 → 属性值：主账号（空名）用 MAIN_KEY 占位，空名原样传给后端 */
  const MAIN_KEY = '@main';
  function encodeAccKey(name) { return name === '' ? MAIN_KEY : name; }
  function decodeAccKey(key) { return key === MAIN_KEY ? '' : key; }

  /* ------------------------------------------------------------ 渲染导航 */
  function renderNav() {
    const nav = N.$('#sideNav');
    const current = location.hash || '#/status';
    nav.innerHTML = NAV.map((group) => {
      const head = group.section
        ? `<div class="nav-group">${esc(group.section)}</div>` : '';
      const items = group.items.map((it) => {
        const active = it.hash === current;
        return `<a class="nav-item ${active ? 'active' : ''}" href="${it.hash}"
          data-nav="${it.id}"><span class="nav-ico">${icon(it.ico, 18)}</span>${esc(it.name)}</a>`;
      }).join('');
      return head + items;
    }).join('');
  }

  function renderCrumb(crumb) {
    const box = N.$('#crumb');
    if (!box) return;
    box.innerHTML = (crumb || []).map((c, i) => {
      if (i === crumb.length - 1) {
        return `<span class="crumb-now">${esc(c)}</span>`;
      }
      return `<span class="crumb-link">${esc(c)}</span><span class="crumb-sep">/</span>`;
    }).join('');
  }

  /* ------------------------------------------------------------ 路由 */
  function currentHash() { return location.hash || '#/status'; }

  function render() {
    const hash = currentHash();
    const route = ROUTES[hash] || ROUTES['#/status'];
    renderNav();
    renderCrumb(route.crumb);
    const old = N.$('#view');
    // 每次渲染换一个**全新的容器元素**：各页面把事件绑在这个容器上，
    // 复用同一个元素会让监听器逐次累加（点一次按钮触发 N 次、弹 N 个提示）。
    // 换掉元素后，旧元素连同它的监听器一起被回收。
    const box = document.createElement('div');
    box.id = 'view';
    box.className = old ? old.className : '';
    old.replaceWith(box);
    try {
      const page = route.view();
      box.innerHTML = page.html || '';
      if (page.mount) page.mount(box);
    } catch (err) {
      box.innerHTML = `<div class="card"><div class="empty">
        <span class="empty-ico">${icon('warn', 20)}</span>页面加载失败：${esc(err.message)}
      </div></div>`;
    }
  }

  /* ------------------------------------------------------------ 登录 */
  function showLogin(message) {
    N.$('#appView').hidden = true;
    N.$('#loginView').hidden = false;
    const err = N.$('#loginError');
    if (err) err.textContent = message || '';
    loadLoginPhoto();
  }

  /* 登录页背景：随机取一张 TMDB 热门影片剧照。
     拿不到（没配 TMDB_API_KEY / 网络不通）就保持原来的渐变背景。 */
  let photoLoaded = false;
  function loadLoginPhoto() {
    const el = N.$('#loginPhoto');
    if (!el || photoLoaded) return;
    window.API.loginBackdrop().then((d) => {
      if (!d || !d.ok || !d.url) return;
      const img = new Image();
      img.onload = () => {
        el.style.backgroundImage = 'url("' + d.url + '")';
        el.classList.add('on');
        // 有剧照了就把底下的渐变网格淡掉，否则缝隙会像网格线压在图上
        const view = N.$('#loginView');
        if (view) view.classList.add('has-photo');
        photoLoaded = true;
      };
      img.src = d.url;
    }).catch(() => { /* 静默：背景只是装饰 */ });
  }

  function showApp() {
    N.$('#loginView').hidden = true;
    N.$('#appView').hidden = false;
  }

  async function doLogin(e) {
    e.preventDefault();
    const user = N.$('#loginUser').value.trim();
    const pass = N.$('#loginPass').value;
    const err = N.$('#loginError');
    err.textContent = '';
    try {
      await window.API.login(user, pass);
      N.$('#loginPass').value = '';
      await boot();
    } catch (ex) {
      err.textContent = ex.message || '登录失败';
      const card = N.$('#loginForm');
      card.classList.remove('shake');
      void card.offsetWidth;
      card.classList.add('shake');
    }
  }

  /* ------------------------------------------------------------ 顶栏面板 */
  function openPopover(which, anchor) {
    const pop = N.$('#' + which + 'Pop');
    if (!pop) return;
    const wasOpen = !pop.hidden;
    closePopovers();
    if (wasOpen) return;
    pop.hidden = false;
    // 定位到按钮下方。popover 是 position:fixed 但**不设 top/left**的话会
    // 停在它在 DOM 里的位置（<aside> 之后）—— 很可能在视口外，
    // 表现就是「点了没反应」。
    if (anchor) {
      const r = anchor.getBoundingClientRect();
      const pw = pop.offsetWidth || 200;
      const ph = pop.offsetHeight || 200;
      let left = r.right - pw;
      let top = r.bottom + 8;
      // 别超出视口
      if (left < 8) left = 8;
      if (left + pw > window.innerWidth - 8) {
        left = Math.max(8, window.innerWidth - pw - 8);
      }
      if (top + ph > window.innerHeight - 8) {
        top = Math.max(8, r.top - ph - 8);   // 放不下就翻到上面
      }
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }
    if (which === 'theme') {
      const list = N.$('#themeList');
      list.innerHTML = N.THEMES.map((t) => `
        <button class="pop-item" data-theme="${t.id}">
          <span class="theme-dot" style="background:${t.bg};box-shadow:inset 0 0 0 2px ${t.ink}"></span>
          <span>${esc(t.name)}</span>
        </button>`).join('');
      list.querySelectorAll('[data-theme]').forEach((b) => {
        b.onclick = () => {
          N.applyTheme(b.getAttribute('data-theme'));
          closePopovers();
        };
      });
    }
  }

  function closePopovers() {
    ['themePop', 'morePop'].forEach((id) => {
      const el = N.$('#' + id);
      if (el) el.hidden = true;
    });
  }

  function openLogs() {
    const drawer = N.$('#logsDrawer');
    const scrim = N.$('#scrim');
    drawer.hidden = false;
    scrim.hidden = false;
    D.logs.render(N.$('#logList'), N.$('#logCount'), N.$('#logFilter'));
  }

  function closeDrawer() {
    N.$('#logsDrawer').hidden = true;
    N.$('#scrim').hidden = true;
  }

  /* ------------------------------------------------------------ 侧栏（移动端抽屉） */
  /** 收起移动端侧栏。桌面端侧栏常驻，这个调用无害。 */
  function closeNav() {
    document.body.classList.remove('nav-open');
  }

  function toggleNav() {
    document.body.classList.toggle('nav-open');
  }

  /** 移动端：点了侧栏里的导航项就顺手收起（否则菜单一直挡着内容） */
  function closeNavAfterNav() {
    if (window.matchMedia('(max-width:960px)').matches) closeNav();
  }

  /* ------------------------------------------------------------ 更多菜单 */
  async function onMore(action) {
    closePopovers();
    if (action === 'logout') {
      try { await window.API.logout(); } catch (e) { /* 忽略 */ }
      window.App.onUnauthorized();
      return;
    }
    if (action === 'status') {
      location.hash = '#/status';
      return;
    }
    if (action === 'scan' || action === 'poll') {
      try {
        if (action === 'scan') {
          N.toast('全量扫描已提交，后台执行中…');
          await window.API.scan(true);
        } else {
          N.toast('增量同步中…');
          await window.API.poll();
        }
        window.App.rerender();
      } catch (e) {
        N.toast(e.message || '操作失败', 'err');
      }
    }
  }

  /* ------------------------------------------------------------ 启动 */
  async function boot() {
    let session = null;
    try {
      session = await window.API.session();
    } catch (e) {
      showLogin('无法连接服务：' + e.message);
      return;
    }
    if (!session.authenticated) {
      const hint = N.$('#loginHint');
      if (hint) {
        hint.textContent = session.show_hint
          ? '默认账号：' + (session.username || 'admin')
          : '';
      }
      showLogin();
      return;
    }
    showApp();
    const ver = N.$('#verText');
    if (ver) ver.textContent = session.version || 'PanShare';
    await D.load();
    // 插件页面的脚本按需加载：清单里已启用的才会下载，加载完自己注册进 NAV
    await loadPlugins();
    if (!location.hash) location.hash = '#/status';
    render();
  }

  function bindShell() {
    N.$('#loginForm').addEventListener('submit', doLogin);
    N.$('#togglePw').addEventListener('click', () => {
      const input = N.$('#loginPass');
      input.type = input.type === 'password' ? 'text' : 'password';
    });
    N.$('#sideNav').addEventListener('click', (e) => {
      const a = e.target.closest('[data-nav]');
      if (a) {
        setTimeout(render, 0);
        // 移动端：选完就收起侧栏，不然菜单一直挡着内容
        closeNavAfterNav();
      }
    });
    window.addEventListener('hashchange', render);
    N.$('#navToggle').addEventListener('click', toggleNav);
    // 点侧栏外的遮罩关闭侧栏（移动端）。遮罩是 body::after 伪元素，
    // 没有自己的节点，只能靠「点了不在侧栏里」来判断。
    document.addEventListener('click', (e) => {
      if (!document.body.classList.contains('nav-open')) return;
      if (e.target.closest('.sidebar')) return;
      if (e.target.closest('.nav-toggle')) return;
      closeNav();
    });
    // 视口变宽（转成桌面）时顺手收起，免得 nav-open 残留在 body 上
    window.addEventListener('resize', () => {
      if (!window.matchMedia('(max-width:960px)').matches) closeNav();
    });
    N.$$('.orb').forEach((b) => {
      b.addEventListener('click', () => {
        const panel = b.getAttribute('data-panel');
        if (panel === 'logs') openLogs();
        else openPopover(panel, b);
      });
    });
    N.$('#scrim').addEventListener('click', () => { closeDrawer(); closePopovers(); });
    N.$$('[data-close-drawer]').forEach((b) => b.addEventListener('click', closeDrawer));
    N.$('#logRefresh').addEventListener('click', () => {
      D.logs.render(N.$('#logList'), N.$('#logCount'), N.$('#logFilter'));
    });
    N.$('#logClear').addEventListener('click', () => {
      D.logs.clear();
      D.logs.render(N.$('#logList'), N.$('#logCount'), N.$('#logFilter'));
    });
    N.$('#morePop').addEventListener('click', (e) => {
      const item = e.target.closest('[data-more]');
      if (item) onMore(item.getAttribute('data-more'));
    });
    document.addEventListener('click', (e) => {
      if (e.target.closest('.orb') || e.target.closest('.popover')) return;
      closePopovers();
    });
  }

  /* ------------------------------------------------------------ 目录选择器 */
  /**
   * 通用「选目录」弹窗。
   *
   * `opts.load(token)` 返回 `{dirs:[{name, token, strm}]}`；内部用一个栈记住
   * 「怎么走到这里的」——所以「上级」不依赖后端返回父目录，面包屑也能显示
   * 可读路径（115 的目录 id 本身不携带路径）。
   *
   * 返回 Promise：确定 → `opts.pick(token, names)` 的结果；取消 → null。
   * ``pick`` 返回 false 表示「还不该关窗」（例如用户还没点进任何目录）。
   */
  function pickerModal(opts) {
    return new Promise((resolve) => {
      const stack = [{ token: opts.start || '', name: opts.startName || '' }];
      const names = () => stack.map((x) => x.name).filter(Boolean);
      const where = () => (opts.where
        ? opts.where(stack[stack.length - 1], names())
        : '/' + names().join('/'));
      N.modal.open({
        title: opts.title || '选择目录',
        body: '<div class="dir-head" id="pkHead"></div>' +
          '<div class="dir-pick" id="pkList" style="max-height:46vh;overflow:auto">' +
          '<span class="form-hint">加载中…</span></div>' +
          '<p class="form-hint" id="pkHint">' + esc(opts.hint || '') + '</p>',
        footer: '<button class="btn" data-pk-cancel>取消</button>' +
          '<button class="btn btn-primary" data-pk-take>' +
          esc(opts.okText || '用这个目录') + '</button>',
        mount(m) {
          const list = m.querySelector('#pkList');
          const head = m.querySelector('#pkHead');
          const hintEl = m.querySelector('#pkHint');
          const take = m.querySelector('[data-pk-take]');

          function headHtml(disabled) {
            return '<button type="button" class="dir-opt" data-up="1"' +
              (disabled ? ' disabled' : '') + '>' + icon('folder', 12) + ' 上级</button>' +
              '<span class="dir-path mono">' + esc(where()) + '</span>';
          }

          function load() {
            const node = stack[stack.length - 1];
            head.innerHTML = headHtml(stack.length <= 1);
            list.innerHTML = '<span class="form-hint">加载中…</span>';
            opts.load(node.token).then((data) => {
              const dirs = (data && data.dirs) || [];
              // 后端有时更清楚这一层能干什么（比如「家庭云」能整选），优先用它
              if (hintEl) hintEl.textContent = (data && data.hint) || opts.hint || '';
              list.innerHTML = dirs.length ? dirs.map((d) => `
                <button type="button" class="dir-opt" data-enter="${esc(d.token)}"
                  data-name="${esc(d.name)}">${icon('folder', 12)} ${esc(d.name)}
                  ${d.strm ? '<span class="tag mono">' + d.strm + '</span>' : ''}
                </button>`).join('')
                : '<span class="form-hint">这一层没有子目录</span>';
            }).catch((err) => {
              list.innerHTML = '<span class="form-hint">读取失败：' +
                esc(err.message) + '</span>';
            });
          }

          N.bindOnce(list, 'click', (e) => {
            const b = e.target.closest('[data-enter]');
            if (!b) return;
            stack.push({
              token: b.getAttribute('data-enter'),
              name: b.getAttribute('data-name')
            });
            load();
          });
          N.bindOnce(head, 'click', (e) => {
            if (!e.target.closest('[data-up]') || stack.length <= 1) return;
            stack.pop();
            load();
          });
          take.onclick = () => {
            const node = stack[stack.length - 1];
            const picked = opts.pick(node.token, names());
            if (picked === false) return;
            N.modal.close();
            resolve(picked);
          };
          m.querySelector('[data-pk-cancel]').onclick = () => {
            N.modal.close();
            resolve(null);
          };
          load();
        }
      });
    });
  }

  /** 选 115 网盘目录 → Promise<{cid, name, path}|null> */
  function pickRemoteDir(accountName) {
    return pickerModal({
      title: '选择 115 网盘目录',
      hint: '点目录进入下一层，「上级」返回；确定后把当前所在目录填进配置',
      pick(cid, list) {
        if (!cid || cid === '0') {
          N.toast('先点进要选的目录', 'err');
          return false;
        }
        return { cid: String(cid), name: list[list.length - 1] || '',
                 path: '/' + list.join('/') };
      },
      load(cid) {
        return window.API.dirs(cid || 0, accountName).then((d) => ({
          dirs: (d.dirs || []).map((x) => ({ name: x.name, token: String(x.id) })),
        }));
      },
    });
  }

  /** 选天翼云盘（189）目录 → Promise<{id, name, path}|null>
   *
   * 目录树里同时有「个人云」和「家庭云」：后端把两套接口（/open/file 与
   * /open/family/file）统一成一套 id（``-11`` / ``fam`` / ``fam:<家庭id>[:<目录id>]``），
   * 所以这里只当普通目录树走，不用管底下是哪套接口。
   *
   * 「家庭云」本身也能直接确定：等于整个家庭云（生成时每个家庭一个子目录），
   * 想只要某一个家庭就点进去、再选具体家庭里的目录。
   */
  function pickCloud189Dir(account) {
    return pickerModal({
      title: '选择天翼云盘目录',
      hint: '最上面那项是「家庭云」：点进去选具体家庭；直接点「用这个目录」' +
        '＝整个家庭云（每个家庭生成到同名子目录）',
      start: '-11',
      startName: '个人云',
      okText: '用这个目录',
      pick(id, list) {
        if (!id) {
          N.toast('先点进要选的目录', 'err');
          return false;
        }
        if (id === 'fam') {
          return { id: 'fam', name: '家庭云（全部家庭）',
                   path: '/个人云/家庭云（全部家庭）' };
        }
        return { id: String(id), name: list[list.length - 1] || '个人云',
                 path: '/' + list.join('/') };
      },
      load(folderId) {
        return window.API.cloud189Dirs(folderId || '-11', account).then((d) => ({
          dirs: (d.dirs || []).map((x) => ({ name: x.name, token: String(x.id) })),
          hint: d.hint || '',
        }));
      },
    });
  }

  /** 选本地映射目录（容器内 STRM_ROOT）→ Promise<string|null>
   *
   * ``opts.relative`` 为真时返回**相对 STRM_ROOT** 的路径（ed2k / 分享的
   * 「strm 输出子目录」就是这种语义，存的是 ``ed2k`` 而不是 ``/strm/ed2k``）。
   */
  function pickLocalDir(startPath, opts) {
    opts = opts || {};
    const start = (startPath || '').replace(/\/+$/, '');
    let rootPath = '';        // STRM_ROOT 本身也可以选（strm 直接放根下）
    return pickerModal({
      title: '选择本地目录（映射目录内）',
      hint: '只列出映射目录（STRM_ROOT）里的文件夹；填的是容器内路径',
      start,
      startName: start.split('/').filter(Boolean).pop() || '',
      // 本地目录直接显示绝对路径，比面包屑有用
      where(node) { return node.token || rootPath || '/'; },
      pick(path) {
        const abs = path || rootPath || '';
        if (!opts.relative) return abs;
        const root = String(rootPath || '').replace(/\/+$/, '');
        if (!root) return abs;
        if (abs === root) return '';
        return abs.startsWith(root + '/') ? abs.slice(root.length + 1) : abs;
      },
      load(path) {
        return window.API.localDirs(path || '').then((d) => {
          rootPath = d.root || rootPath;
          return {
            dirs: (d.dirs || []).map((x) => ({
              name: x.name, token: x.path, strm: x.strm,
            })),
          };
        });
      },
    });
  }

  window.Shell = {
    NAV, ROUTES, render, boot, bindShell, accountPills, showLogin, showApp,
    closePopovers, pickRemoteDir, pickLocalDir, pickCloud189Dir,
    registerPlugin, loadPlugins
  };
})();
