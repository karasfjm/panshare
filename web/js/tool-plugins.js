/* ==========================================================================
   tool-plugins.js · 插件管理
   --------------------------------------------------------------------------
   天翼云盘 / 通知与联动 / 企微可信IP 都是「可选功能」：
     · 源码随包附带（几十 KB），**重依赖按需下载**（企微那套浏览器约 730MB）；
     · 下载的东西落在 /config/plugins/<id>/（配置卷），容器重建不用重装；
     · 插件的前端脚本由 /plugin-assets/<id>.js 按需加载 —— 没装就没有
       侧栏入口，也不用在核心代码里硬编码插件名单。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  let state = null;
  let poll = null;        // 安装进度轮询
  let pollId = '';        // 正在装哪个插件
  let busy = false;

  function page() {
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>插件</h1>
            <p>天翼云盘、通知与联动、企微可信IP 这些功能都不在主程序里，
              用的是哪几个就装哪几个。企微可信IP 要下载浏览器内核（约 730MB），
              装在配置卷上，镜像和升级都不受影响。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-pg-refresh>${icon('refresh', 14)} 刷新</button>
          </div>
        </div>
        <div id="pgBody"><div class="card"><div class="empty">加载中…</div></div></div>`,
      mount(root) {
        bind(root);
        load(root);
      }
    };
  }

  function bind(root) {
    root.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-pg]');
      if (!btn) return;
      const act = btn.getAttribute('data-pg');
      const id = btn.getAttribute('data-id');
      if (act === 'refresh') load(root);
      if (act === 'install') install(root, id);
      if (act === 'uninstall') uninstall(root, id);
      if (act === 'enable') toggle(root, id, true);
      if (act === 'disable') toggle(root, id, false);
      if (act === 'open') location.hash = btn.getAttribute('data-hash') || '#/plugins';
    });
  }

  function load(root) {
    window.API.plugins().then((d) => {
      state = d;
      render(root);
      // 上次没装完（刷新/换页走的）：接着显示进度
      const running = (d.plugins || []).filter((p) => p.busy)[0];
      if (running) { pollId = running.id; startPoll(root); }
    }).catch((err) => {
      const box = root.querySelector('#pgBody');
      if (box) {
        box.innerHTML = '<div class="card"><div class="empty">加载失败：' +
          esc(err.message) + '</div></div>';
      }
    });
  }

  function statusOf(p) {
    if (!p.installed) return { text: '未安装', cls: '' };
    if (!p.enabled) return { text: '已停用', cls: 'warn' };
    if (!p.deps.ready) return { text: '缺依赖', cls: 'warn' };
    return { text: '已启用', cls: 'ok' };
  }

  function depsText(p) {
    const parts = [];
    if ((p.deps.python || []).length) parts.push(p.deps.python.join('、'));
    if ((p.deps.browser || []).length) parts.push(p.deps.browser.join('、') + ' 浏览器');
    if (!parts.length) return '无额外依赖';
    return parts.join(' + ');
  }

  function actionsHtml(p) {
    if (p.busy) {
      return `<span class="form-hint">${icon('clock', 12)} 正在安装…</span>`;
    }
    const out = [];
    if (!p.installed) {
      out.push(`<button class="btn btn-primary btn-sm" data-pg="install" data-id="${esc(p.id)}">
        ${icon('download', 13)} 安装</button>`);
    } else {
      if (!p.deps.ready) {
        out.push(`<button class="btn btn-primary btn-sm" data-pg="install" data-id="${esc(p.id)}">
          ${icon('download', 13)} 安装依赖</button>`);
      }
      if (p.enabled) {
        if (p.hash) {
          out.push(`<button class="btn btn-sm" data-pg="open" data-id="${esc(p.id)}"
            data-hash="${esc(p.hash)}">${icon('external', 13)} 打开</button>`);
        }
        out.push(`<button class="btn btn-sm" data-pg="disable" data-id="${esc(p.id)}">
          ${icon('power', 13)} 停用</button>`);
      } else {
        out.push(`<button class="btn btn-primary btn-sm" data-pg="enable" data-id="${esc(p.id)}">
          ${icon('power', 13)} 启用</button>`);
      }
      out.push(`<button class="btn btn-sm" data-pg="uninstall" data-id="${esc(p.id)}">
        ${icon('trash', 13)} 卸载</button>`);
    }
    return out.join('');
  }

  function cardHtml(p) {
    const st = statusOf(p);
    const task = p.task || {};
    const log = (task.log || []).join('\n');
    const showLog = p.busy || task.status === 'error' || (log && p.id === pollId);
    const dl = p.download_size ? `<span>下载体积 ${esc(p.download_size)}</span>` : '';
    return `
      <div class="card plugin-card">
        <div class="card-head">
          <span class="plugin-ico">${icon(p.icon, 20)}</span>
          <div style="min-width:0">
            <h2>${esc(p.name)}
              <span class="tag mono">v${esc(p.version || '1')}</span>
              <span class="tag ${st.cls}">${st.text}</span>
            </h2>
            <p class="card-sub">${esc(p.summary)}</p>
          </div>
          <div class="card-actions">${actionsHtml(p)}</div>
        </div>
        <p class="plugin-desc">${esc(p.desc)}</p>
        <div class="plugin-meta">
          <span>代码 ${esc(p.size)}</span>
          <span>依赖 ${esc(depsText(p))}</span>
          ${dl}
        </div>
        ${showLog ? `<div class="code-block plugin-log" id="pgLog-${esc(p.id)}">${
          log ? esc(log) : '等待输出…'}</div>` : ''}
        ${task.status === 'error' ? `<p class="form-hint err">安装失败：${
          esc(task.error || '看上面的日志')}</p>` : ''}
      </div>`;
  }

  function render(root) {
    const box = root.querySelector('#pgBody');
    if (!box) return;
    if (!state) {
      box.innerHTML = '<div class="card"><div class="empty">加载中…</div></div>';
      return;
    }
    const rows = state.plugins || [];
    const installed = rows.filter((p) => p.installed && p.enabled).length;
    box.innerHTML = `
      <div class="card">
        <div class="card-head">
          <h2>概要</h2>
          <p class="card-sub">已安装 ${installed} / ${rows.length} 个插件；
            依赖下载到 <span class="mono">${esc(state.work_dir || '')}</span>，
            放在 /config 卷上，容器重建/升级都不用重装</p>
        </div>
        <p class="form-hint">安装时会用 <span class="mono">${esc(state.pip_index || 'pip')}</span>
          拉依赖；国内网络慢的话可以先在容器里配好代理，再来点安装。</p>
      </div>
      ${rows.map(cardHtml).join('')}`;
  }

  /* ------------------------------------------------------------ 操作 */
  function install(root, id) {
    if (busy) return;
    const row = (state && state.plugins || []).filter((p) => p.id === id)[0];
    const tip = row && row.download_size
      ? `首次安装要下载 ${row.download_size}，慢的话要等几分钟，装完自动刷新页面。继续？`
      : '安装后即可在本页「打开」，装完自动刷新页面。继续？';
    N.confirm('安装插件', tip, { okText: '开始安装' }).then((ok) => {
      if (!ok) return;
      busy = true;
      window.API.pluginInstall(id).then(() => {
        pollId = id;
        startPoll(root);
      }).catch((err) => {
        busy = false;
        N.toast(err.message, 'err');
      });
    });
  }

  function uninstall(root, id) {
    if (busy) return;
    const row = (state && state.plugins || []).filter((p) => p.id === id)[0];
    const extra = row && row.deps.size && row.deps.size !== '0 B'
      ? ` 已下载的依赖（${row.deps.size}）会保留，重装时不用再下一次。`
      : '';
    N.confirm('卸载插件', '卸载后入口会消失，配置和已生成的文件都还在。' + extra,
      { danger: true, okText: '卸载' }).then((ok) => {
      if (!ok) return;
      busy = true;
      window.API.pluginUninstall(id, false).then(() => {
        N.toast('已卸载，正在刷新…', 'ok');
        setTimeout(() => location.reload(), 900);
      }).catch((err) => { busy = false; N.toast(err.message, 'err'); });
    });
  }

  function toggle(root, id, enabled) {
    if (busy) return;
    busy = true;
    window.API.pluginEnable(id, enabled).then(() => {
      N.toast(enabled ? '已启用，正在刷新…' : '已停用，正在刷新…', 'ok');
      setTimeout(() => location.reload(), 900);
    }).catch((err) => { busy = false; N.toast(err.message, 'err'); });
  }

  /* ------------------------------------------------------------ 进度轮询 */
  function startPoll(root) {
    stopPoll();
    poll = setInterval(() => {
      if (!document.body.contains(root)) { stopPoll(); return; }
      const id = pollId;
      window.API.pluginLog(id).then((task) => {
        window.API.plugins().then((d) => {
          state = d;
          // 日志可能比 plugins() 新：直接把最新的贴进去，避免一帧空白
          const row = (d.plugins || []).filter((p) => p.id === id)[0];
          if (row) {
            row.task = { status: task.status, error: task.error, log: task.log };
            row.busy = task.status === 'running';
          }
          render(root);
          if (task.status === 'done' || task.status === 'error') {
            stopPoll();
            busy = false;
            if (task.status === 'done') {
              N.toast('安装完成，正在刷新页面…', 'ok');
              setTimeout(() => location.reload(), 1200);
            } else {
              N.toast('安装失败：' + (task.error || '看下面的日志'), 'err');
            }
          }
        });
      }).catch(() => stopPoll());
    }, 1500);
  }

  function stopPoll() {
    if (poll) { clearInterval(poll); poll = null; }
  }

  window.ToolPlugins = { page };
})();
