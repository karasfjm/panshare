/* ==========================================================================
   core.js · 图标 / 工具函数 / 主题 / 弹层
   --------------------------------------------------------------------------
   与 panshare-ui 保持同一套设计系统（图标、弹窗、Toast、主题）。
   ========================================================================== */
(function () {
  'use strict';

  /* ---------------------------------------------------------------- 图标 */
  const P = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/>',
    box: '<path d="M21 8.5v7a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 15.5v-7a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8.5z"/><path d="M3.3 7.2 12 12l8.7-4.8"/><path d="M12 22V12"/>',
    db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    dots: '<circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    check: '<path d="m4 12 5 5L20 6"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    eyeOff: '<path d="M3 3l18 18"/><path d="M10.6 6.2A10.9 10.9 0 0 1 12 6c6.4 0 10 6 10 6a17.6 17.6 0 0 1-3 3.6"/><path d="M6.7 6.7A17.4 17.4 0 0 0 2 12s3.6 6 10 6a10.6 10.6 0 0 0 4.3-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    arrowLeft: '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.5 12.5 8-8"/><path d="m16 5 3 3M18.5 2.5l3 3"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
    lock: '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    play: '<path d="m7 4 12 8-12 8V4z"/>',
    download: '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 20h16"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    power: '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    tv: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="m8 3 4 4 4-4"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-5.5 6.5-5.5s6.5 1.9 6.5 5.5"/><path d="M17 5.2a3.5 3.5 0 0 1 0 6.6"/><path d="M18.5 14.3c2 .6 3.5 2 3.5 4.4"/>',
    zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
    shield: '<path d="M12 3 5 6v6c0 4.5 3 8 7 9 4-1 7-4.5 7-9V6l-7-3z"/>',
    upload: '<path d="M12 21V9"/><path d="m7 13 5-5 5 5"/><path d="M4 4h16"/>',
    drive: '<rect x="3" y="8" width="18" height="8" rx="2"/><path d="M7 12h.01M11 12h.01"/><path d="m6 16-2 4h16l-2-4"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    article: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="M9 11h7M9 15h7M9 19h4"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
    broom: '<path d="m13 11 6-6"/><path d="M9.5 9.5 5 14c-1 1-1 3 0 4s3 1 4 0l4.5-4.5"/><path d="m14 15 4 4M17 18l1.5 1.5"/>',
    shuffle: '<path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="m15 15 6 6M4 4l5 5"/>',
    wifi: '<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 16a5 5 0 0 1 7 0"/><path d="M12 20h.01"/><path d="M2 9a15 15 0 0 1 20 0"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    warn: '<path d="M12 4 2.5 20h19L12 4z"/><path d="M12 10v4M12 17h.01"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v5h5"/><path d="M12 8v4l3 2"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5"/>',
    plug: '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v4a6 6 0 0 1-12 0V8z"/><path d="M12 18v3"/>'
  };

  function icon(name, size) {
    const d = P[name] || P.info;
    return `<svg width="${size || 18}" height="${size || 18}" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true">${d}</svg>`;
  }

  /* ---------------------------------------------------------------- 存储 */
  const K = {
    theme: 'p115.theme', logs: 'p115.logs', accounts: 'p115.accounts',
    settings: 'p115.settings', strmTasks: 'p115.strmTasks', session: 'p115.session'
  };

  function storeGet(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }
  function storeSet(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* ignore */ }
  }

  /* ---------------------------------------------------------------- 工具 */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }
  function uid(prefix) {
    return (prefix || 'id') + Math.random().toString(36).slice(2, 9);
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function hhmmss(d) {
    const t = d || new Date();
    return pad(t.getHours()) + ':' + pad(t.getMinutes()) + ':' + pad(t.getSeconds());
  }
  function stamp(d) {
    const t = d || new Date();
    return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate()) + ' ' +
      pad(t.getHours()) + ':' + pad(t.getMinutes()) + ':' + pad(t.getSeconds());
  }
  function fmtSize(bytes) {
    if (!bytes) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), u.length - 1);
    return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + u[i];
  }
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  /* ------------------------------------------------------------ 事件绑定 */
  /**
   * 只绑一次的委托监听。
   *
   * 页面重绘时如果复用同一个容器元素，绑在它上面的监听器会**逐次累加**：
   * 点一次按钮触发 N 次、弹 N 个提示。用这个函数登记过的监听器，
   * 同一个 (元素, 类型, 处理器) 只会真正绑定一次。
   */
  function bindOnce(el, type, handler, key) {
    if (!el) return;
    const store = el.__wbOnce || (el.__wbOnce = {});
    const id = (key || handler.name || String(handler)) + '::' + type;
    if (store[id]) return;
    store[id] = true;
    el.addEventListener(type, handler);
  }

  /* ---------------------------------------------------------------- Toast */
  const TOAST_MAX = 4;          // 最多同时显示几个，避免刷屏堆成一片
  const TOAST_MS = 2200;
  function toast(msg, kind, ms) {
    const box = $('#toasts');
    if (!box) return;
    const text = String(msg == null ? '' : msg);
    // 同内容只留**一条**：连点时复用同一个元素（先删再建会短暂出现两条），
    // 顺便把它的消失计时重置，让它重新完整显示一遍
    let el = null;
    Array.prototype.slice.call(box.children).forEach((node) => {
      if (node.getAttribute('data-msg') !== text) return;
      if (!el) {
        el = node;
        if (node._hideTimer) { clearTimeout(node._hideTimer); node._hideTimer = null; }
        node.style.transition = '';
        node.style.opacity = '1';
        node.style.transform = '';
      } else {
        node.remove();
      }
    });
    if (!el) {
      el = document.createElement('div');
      el.className = 'toast' + (kind ? ' ' + kind : '');
      el.setAttribute('data-msg', text);
      el.innerHTML = (kind === 'ok' ? icon('check', 14) : kind === 'err' ? icon('warn', 14) : '') +
        '<span>' + esc(text) + '</span>';
      box.appendChild(el);
      // 超出上限就把最旧的挤掉
      while (box.children.length > TOAST_MAX) box.removeChild(box.firstChild);
    }
    if (el._hideTimer) clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(-6px)';
      el._removeTimer = setTimeout(() => el.remove(), 260);
    }, ms || TOAST_MS);
  }

  /* ---------------------------------------------------------------- 弹窗 */
  // 弹窗可以**嵌套**：典型场景是「配置表单」里点「选择目录」，选择器又开一层。
  // 早先只有一个 #modalBox，第二层直接把第一层的内容冲掉，选完目录回来
  // querySelector 全是 null（表现为 "cannot set properties of null (setting 'value')"）。
  // 所以这里改成栈：新弹窗叠在上面，close 只关最上面那层，下层 DOM 原样保留。
  const modalStack = [];

  function layerAt(index) {
    if (index === 0) return { wrap: $('#modalWrap'), box: $('#modalBox') };
    const wrap = document.createElement('div');
    wrap.className = 'modal-wrap stacked';
    const box = document.createElement('div');
    box.className = 'modal';
    wrap.appendChild(box);
    document.body.appendChild(wrap);
    return { wrap, box };
  }

  function openModal(opts) {
    const layer = layerAt(modalStack.length);
    const wrap = layer.wrap;
    const box = layer.box;
    // 越靠上层 z-index 越大，不然新弹窗会钻到下层面板背后
    wrap.style.zIndex = String(60 + modalStack.length * 4);
    box.className = 'modal' + (opts.wide ? ' wide' : '');
    box.innerHTML =
      '<div class="modal-head"><h3>' + esc(opts.title || '') + '</h3>' +
      '<button class="icon-btn" data-close-modal>' + icon('x', 16) + '</button></div>' +
      '<div class="modal-body">' + (opts.body || '') + '</div>' +
      (opts.footer === false ? '' : '<div class="modal-foot">' + (opts.footer ||
        '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>确定</button>') + '</div>');
    wrap.hidden = false;
    const okBtn = $('[data-modal-ok]', box);
    if (okBtn && opts.onOk) {
      okBtn.addEventListener('click', () => { if (opts.onOk() !== false) closeModal(); });
    }
    $$('[data-close-modal]', box).forEach(
      (b) => b.addEventListener('click', () => closeModal()));
    wrap.onclick = (e) => { if (e.target === wrap) closeModal(); };
    modalStack.push(layer);
    if (opts.mount) opts.mount(box);
    return box;
  }

  /** 关掉最上面那层弹窗（嵌套时下层不动）。 */
  function closeModal() {
    const layer = modalStack.pop();
    if (!layer) return;
    layer.wrap.hidden = true;
    layer.box.innerHTML = '';
    layer.box.className = 'modal';
    // 动态建的层用完就拆掉，别在 body 里越积越多
    if (layer.wrap.id !== 'modalWrap') layer.wrap.remove();
  }

  /** 一次关掉所有层级（例如会话过期整页重绘） */
  function closeAllModals() {
    while (modalStack.length) closeModal();
  }
  function confirmBox(title, text, opts) {
    return new Promise((resolve) => {
      openModal({
        title: title,
        body: '<p style="margin:2px 0 6px;color:var(--text-2);font-size:13px">' + text + '</p>',
        footer: '<button class="btn" data-no>取消</button>' +
          '<button class="btn ' + ((opts && opts.danger) ? 'btn-danger' : 'btn-primary') +
          '" data-yes>' + esc((opts && opts.okText) || '确定') + '</button>',
        mount(box) {
          $('[data-no]', box).onclick = () => { closeModal(); resolve(false); };
          $('[data-yes]', box).onclick = () => { closeModal(); resolve(true); };
        }
      });
    });
  }

  /* ---------------------------------------------------------------- 主题 */
  const THEMES = [
    { id: 'light', name: '日间', bg: '#f4f5f7', ink: '#3b6ef6' },
    { id: 'dark', name: '夜间', bg: '#181b21', ink: '#5b8cff' },
    { id: 'pink', name: '粉蓝', bg: '#fdf3f8', ink: '#e8578f' },
    { id: 'violet', name: '紫霞', bg: '#f5f2fd', ink: '#7c5cf0' }
  ];
  function applyTheme(id) {
    const theme = THEMES.some((t) => t.id === id) ? id : 'light';
    document.documentElement.setAttribute('data-theme', theme);
    storeSet(K.theme, theme);
    return theme;
  }

  /* ---------------------------------------------------------------- 导出 */
  window.NS = {
    K, $, $$, esc, uid, pad, hhmmss, stamp, fmtSize, sleep,
    icon, toast, storeGet, storeSet, THEMES, applyTheme, bindOnce,
    modal: {
      open: openModal, close: closeModal, closeAll: closeAllModals,
      depth: () => modalStack.length
    }, confirm: confirmBox
  };
})();
