/* ==========================================================================
   tool-wxip.js · 企微「企业可信 IP」自动更新
   --------------------------------------------------------------------------
   移植 JYONG719/WXIPUpdaterr 的核心流程：扫码登录企微管理后台 →
   定时检测公网 IP → 变了就自动改自建应用的可信 IP。
   浏览器（playwright）跑在后端，这里只负责展示二维码 / 状态 / 配置。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  let state = null;
  let poll = null;
  let busy = false;

  const LOGIN_TEXT = {
    idle: '未登录',
    qr: '正在打开登录页…',
    waiting: '等待扫码',
    ok: '已登录',
    expired: '登录已过期',
    error: '出错了'
  };

  function page() {
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>企微可信 IP</h1>
            <p>定时检测公网 IP，变了就自动写进企业微信自建应用的「企业可信 IP」，
              免得宽带换 IP 之后应用消息发不出去、回调收不到。需要扫码登录一次企微管理后台。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-refresh>${icon('refresh', 14)} 刷新</button>
            <button class="btn btn-primary btn-sm" data-save>${icon('check', 14)} 保存配置</button>
          </div>
        </div>
        <div id="wxBody"><div class="card"><div class="empty">加载中…</div></div></div>`,
      mount(root) {
        bind(root);
        load(root);
      }
    };
  }

  function renderBody(root) {
    const box = root.querySelector('#wxBody');
    if (!box) return;
    if (!state) {
      box.innerHTML = '<div class="card"><div class="empty">加载中…</div></div>';
      return;
    }
    const s = state.settings || {};
    const login = state.login || 'idle';
    const last = state.last_update || {};
    const hist = (state.history || []).slice().reverse();

    box.innerHTML = `
      ${state.available ? '' : `
      <div class="card">
        <div class="card-head"><h2>还差一步：安装插件依赖</h2></div>
        <p class="form-hint">浏览器内核（playwright + Chromium，约 730MB）不在主程序里，
          需要在这个插件上装一次。装完会下载到 /config 卷上，之后重启、换镜像都不用再装。</p>
        <div class="inline" style="margin-top:10px">
          <a class="btn btn-primary btn-sm" href="#/plugins">${icon('plug', 13)} 去插件页安装</a>
        </div>
      </div>`}

      <div class="card">
        <div class="card-head">
          <h2>当前状态</h2>
          <p class="card-sub">公网 IP 每 ${s.interval || 300} 秒检测一次；变了才会去动企微的配置</p>
        </div>
        <div class="stat-strip">
          <div class="ss"><div class="ss-label">当前公网 IP</div>
            <div class="ss-val">${esc(state.ip || '—')}</div></div>
          <div class="ss"><div class="ss-label">登录状态</div>
            <div class="ss-val">${esc(LOGIN_TEXT[login] || login)}</div></div>
          <div class="ss"><div class="ss-label">上次检测</div>
            <div class="ss-val">${state.last_check ? N.stamp(new Date(state.last_check * 1000)) : '还没跑过'}</div></div>
          <div class="ss"><div class="ss-label">上次更新</div>
            <div class="ss-val">${last.at ? N.stamp(new Date(last.at * 1000)) : '—'}</div></div>
        </div>
        ${last.detail ? `<p class="form-hint" style="margin-top:8px">
          ${last.ok ? icon('check', 12) : icon('warn', 12)} ${esc(last.detail)}</p>` : ''}
        ${state.last_error ? `<p class="form-hint err">${esc(state.last_error)}</p>` : ''}
        <div class="inline" style="margin-top:10px;flex-wrap:wrap">
          <button class="btn btn-sm" data-wx-login>${icon('key', 13)} ${login === 'waiting' ? '重新生成二维码' : '扫码登录'}</button>
          <button class="btn btn-sm" data-wx-update>${icon('refresh', 13)} 立即检测并更新</button>
          <button class="btn btn-sm" data-wx-shot>${icon('scan', 13)} 截图诊断</button>
          <button class="btn btn-sm" data-wx-logout>${icon('logout', 13)} 退出登录</button>
        </div>
      </div>

      ${(login === 'qr' || login === 'waiting' || state.qr) ? `
      <div class="card">
        <div class="card-head">
          <h2>扫码登录企微管理后台</h2>
          <p class="card-sub">用管理员微信扫码，<b>扫完还要在手机上点一次「确认登录」</b>；确认后这里会自动变成「已登录」，登录态存在容器里，过期再扫一次</p>
        </div>
        ${state.qr ? `<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
          <img src="${state.qr}" alt="企微登录二维码" style="width:220px;height:220px;border:1px solid var(--line);border-radius:10px;background:#fff">
          <div style="max-width:420px">
            <p class="form-hint">二维码 ${state.qr_at ? N.stamp(new Date(state.qr_at * 1000)) : ''} 生成，
              过期就点「重新生成二维码」。二维码只在容器内部截图，不会外传。</p>
            <p class="form-hint">扫完没反应？点「截图诊断」看看浏览器停在哪一页。</p>
          </div>
        </div>` : '<p class="form-hint">正在生成二维码…</p>'}
      </div>` : ''}

      ${state.shot ? `
      <div class="card">
        <div class="card-head"><h2>浏览器当前画面</h2>
          <p class="card-sub">定位失败时用它看页面结构（比如企微改了后台布局）</p></div>
        <img src="${state.shot}" alt="诊断截图" style="max-width:100%;border:1px solid var(--line);border-radius:10px">
      </div>` : ''}

      <div class="card">
        <div class="card-head">
          <h2>自动更新</h2>
          <p class="card-sub">只改「自建应用」的企业可信IP；不填 AgentId 时会去后台列表里自动抓</p>
        </div>

        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">启用守护</div>
            <div class="sr-desc">关掉之后完全不检测，也不动企微配置</div></div>
          <label class="switch"><input type="checkbox" id="wxEnabled" ${s.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">IP 变了就自动改</div>
            <div class="sr-desc">关掉后只发通知提醒，需要你自己去后台改</div></div>
          <label class="switch"><input type="checkbox" id="wxAuto" ${s.auto !== false ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">变化 / 失败时发通知</div>
            <div class="sr-desc">走「通知与联动」里配的渠道（企微、TG、Webhook）</div></div>
          <label class="switch"><input type="checkbox" id="wxNotify" ${s.notify !== false ? 'checked' : ''}><span class="track"></span></label>
        </div>

        <div class="form-row">
          <label>自建应用 AgentId</label>
          <textarea class="input mono" id="wxAgents" rows="2"
            placeholder="1000002, 1000003（留空 = 自动抓后台列表）">${esc(s.agent_ids || '')}</textarea>
          <span class="form-hint">在企微后台「应用管理 → 自建应用」里，应用详情页地址最后的数字就是 AgentId</span>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>检测间隔（秒）</label>
            <input class="input" id="wxInterval" type="number" min="60" step="60" value="${s.interval || 300}">
          </div>
          <div class="form-row">
            <label>访问企微后台的代理（可选）</label>
            <input class="input mono" id="wxProxy" value="${esc(s.proxy || '')}"
              placeholder="http://192.168.2.1:7890">
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>更新记录</h2>
          <p class="card-sub">最近 20 次，只记结果不记 cookie</p>
        </div>
        ${hist.length ? `<table class="table">
          <thead><tr><th>时间</th><th>IP</th><th>结果</th></tr></thead>
          <tbody>${hist.map((h) => `<tr>
            <td>${N.stamp(new Date(h.at * 1000))}</td>
            <td class="mono">${esc(h.ip || '')}${h.old && h.old !== h.ip ? ` <span class="muted">(原 ${esc(h.old)})</span>` : ''}</td>
            <td>${h.ok ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}
              ${esc(h.detail || '')}</td>
          </tr>`).join('')}</tbody>
        </table>` : '<p class="form-hint">还没有记录</p>'}
      </div>`;
  }

  function bind(root) {
    root.addEventListener('click', (e) => {
      if (e.target.closest('[data-refresh]')) { load(root); return; }
      if (e.target.closest('[data-save]')) { save(root); return; }
      if (e.target.closest('[data-wx-login]')) { act(root, 'wxipLogin', '正在打开企微登录页…'); return; }
      if (e.target.closest('[data-wx-update]')) { act(root, 'wxipUpdate', '已开始检测'); return; }
      if (e.target.closest('[data-wx-shot]')) { act(root, 'wxipShot', '正在截图…'); return; }
      if (e.target.closest('[data-wx-logout]')) { logout(root); }
    });
  }

  function collect(root) {
    const val = (id) => (root.querySelector('#' + id) || {}).value || '';
    const chk = (id) => !!(root.querySelector('#' + id) || {}).checked;
    return {
      enabled: chk('wxEnabled'),
      auto: chk('wxAuto'),
      notify: chk('wxNotify'),
      agent_ids: val('wxAgents').trim(),
      interval: Math.max(60, Number(val('wxInterval')) || 300),
      proxy: val('wxProxy').trim()
    };
  }

  function load(root) {
    window.API.wxip().then((d) => {
      state = d;
      renderBody(root);
      syncPoll(root);
    }).catch((err) => {
      const box = root.querySelector('#wxBody');
      if (box) box.innerHTML = '<div class="card"><div class="empty">' + esc(err.message) + '</div></div>';
    });
  }

  /** 登录过程中每 2.5 秒刷一次状态（二维码 / 是否扫成功） */
  function syncPoll(root) {
    const need = state && (state.login === 'qr' || state.login === 'waiting');
    if (need && !poll) {
      poll = setInterval(() => {
        if (!document.body.contains(root)) { stopPoll(); return; }
        window.API.wxip().then((d) => {
          const before = state && state.login;
          state = d;
          renderBody(root);
          if (before !== 'ok' && d.login === 'ok') N.toast('已登录企微管理后台', 'ok');
          syncPoll(root);
        }).catch(() => stopPoll());
      }, 2500);
    } else if (!need) {
      stopPoll();
    }
  }

  function stopPoll() {
    if (poll) { clearInterval(poll); poll = null; }
  }

  /** 等后台任务真的跑完再刷新。
   *  更新一次要几十秒（开浏览器 → 逐个应用点弹窗 → 写完核对），
   *  以前提交后 1.5 秒就重载，用户看到的还是旧状态，会以为没生效。 */
  function waitDone(root, before, ms) {
    const end = Date.now() + (ms || 180000);
    const tick = () => {
      if (!document.body.contains(root)) { busy = false; return; }
      window.API.wxip().then((d) => {
        const at = ((d.last_update || {}).at) || 0;
        if (at > (before || 0)) {
          state = d;
          renderBody(root);
          busy = false;
          const last = d.last_update || {};
          N.toast(last.ok ? '已更新：' + (last.detail || '成功')
            : '更新失败：' + (last.detail || '看更新记录'), last.ok ? 'ok' : 'err');
          return;
        }
        if (Date.now() > end) {
          state = d;
          renderBody(root);
          busy = false;
          N.toast('还在跑，点右上「刷新」看结果', 'warn');
          return;
        }
        setTimeout(tick, 3000);
      }).catch(() => { busy = false; });
    };
    setTimeout(tick, 2000);
  }

  function act(root, method, tip) {
    if (busy) return;
    busy = true;
    const before = ((state && state.last_update) || {}).at || 0;
    window.API[method]().then((r) => {
      if (r && r.ok === false) { N.toast(r.detail || '操作失败', 'err'); busy = false; return; }
      N.toast(tip, 'ok');
      if (method === 'wxipUpdate') waitDone(root, before);
      else setTimeout(() => { busy = false; load(root); }, 1500);
    }).catch((err) => { busy = false; N.toast(err.message, 'err'); });
  }

  function logout(root) {
    N.confirm('退出企微登录', '会删掉容器里保存的登录态，下次要重新扫码。', { danger: true, okText: '退出登录' })
      .then((ok) => {
        if (!ok) return;
        return window.API.wxipLogout().then(() => {
          N.toast('已退出登录', 'ok');
          load(root);
        });
      });
  }

  function save(root) {
    if (busy) return;
    busy = true;
    window.API.saveWxip(collect(root)).then((r) => {
      state = r.status || state;
      renderBody(root);
      syncPoll(root);
      N.toast('配置已保存', 'ok');
      D.logs.add('info', '企微可信IP 配置已更新');
    }).catch((err) => N.toast(err.message, 'err'))
      .finally(() => { busy = false; });
  }

  window.ToolWxip = { page };

  // 插件自注册：没安装企微插件时这份脚本不会被加载，侧栏也不会有入口
  if (window.Shell && window.Shell.registerPlugin) {
    window.Shell.registerPlugin({
      id: 'wxip', hash: '#/wxip', name: '企微可信IP',
      ico: 'key', section: '系统', view: page
    });
  }
})();
