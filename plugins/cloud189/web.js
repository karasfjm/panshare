/* ==========================================================================
   tool-cloud189.js · 天翼云盘（189）：多账号登录 + 选目录（含家庭云）+ 生成 strm
   --------------------------------------------------------------------------
   · 账号存在服务端（可多个），每条配置绑定一个账号；
   · token 到期靠保存下来的会话密钥自动续期，页面显示到期时间与续期开关；
   · 配置 = 「云端目录 → 本地映射目录」，一个目录配一行；
   · strm 内容 = <请求头>/p189/play/<账号>/<fileId>，播放时由本服务换直链再 302。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  const DEFAULT_EXTS = 'mp4,mkv,ts,m2ts,mts,iso,avi,wmv,flv,mov,rmvb,rm,' +
    'webm,mpg,mpeg,m4v,3gp,asf,vob,divx,f4v,ogv';

  // 最近一次拉到的配置列表：卡片按钮只拿到 id，操作时从这里取完整记录
  let CONFIGS = [];
  // 账号列表（配置卡片显示归属账号、表单下拉选账号都从这里取）
  let ACCOUNTS = [];

  // 后端只回「矩阵字符串」，二维码由前端画（不引第三方库、不联网取图）
  function qrSvg(rows) {
    const n = (rows || []).length;
    if (!n) return '<div class="form-hint">二维码生成失败，请重试</div>';
    let d = '';
    for (let y = 0; y < n; y++) {
      const line = String(rows[y] || '');
      for (let x = 0; x < n; x++) {
        if (line.charAt(x) === '1') d += 'M' + x + ' ' + y + 'h1v1h-1z';
      }
    }
    return '<svg class="qr-svg" viewBox="0 0 ' + n + ' ' + n +
      '" shape-rendering="crispEdges" role="img" aria-label="登录二维码">' +
      '<rect width="' + n + '" height="' + n + '" fill="#ffffff"/>' +
      '<path d="' + d + '" fill="#000000"/></svg>';
  }

  function statCard(ico, cls, label, num) {
    return `<div class="strm-stat">
      <span class="ss-ico ${cls}">${icon(ico, 18)}</span>
      <div class="ss-main"><div class="ss-label">${esc(label)}</div>
        <div class="ss-num">${esc(num)}</div></div>
    </div>`;
  }

  /* ------------------------------------------------------------ 登录区 */
  function remainText(a) {
    if (!a.logged_in) return '';
    const h = a.remaining_hours || 0;
    if (!a.expires_at) return '有效期未知';
    return h > 72 ? '剩 ' + Math.round(h / 24) + ' 天' : '剩 ' + h + ' 小时';
  }

  function accCard(a) {
    const state = !a.logged_in
      ? (a.expired ? '<span class="tag err">已过期</span>'
        : '<span class="tag warn">未登录</span>')
      : '<span class="tag ok">已登录</span>';
    const renew = a.can_refresh
      ? (a.auto_refresh ? '<span class="tag">自动续期</span>'
        : '<span class="tag warn">续期已关</span>')
      : '<span class="tag warn" title="手动填的 token，或老版本迁移过来的账号：没有会话密钥，换不了新 token">不能续期</span>';
    const defaultTag = a.is_default ? '<span class="tag brand">默认</span>' : '';
    return `
      <div class="task-card" data-accid="${esc(a.id)}">
        <div class="tc-head">
          <div class="tc-name">${esc(a.display || a.id)} ${defaultTag}</div>
          <button class="icon-btn" data-acc-relogin title="重新登录（重扫一次码）">${icon('refresh', 14)}</button>
        </div>
        <div class="tc-row"><span class="tc-ico v1">${icon('user', 12)}</span>
          <span class="tc-key">登录名</span>
          <span class="tc-val mono">${esc(a.login_name || '—')}</span></div>
        <div class="tc-row"><span class="tc-ico v2">${icon('clock', 12)}</span>
          <span class="tc-key">token</span>
          <span class="tc-val mono">${esc(a.expires_text || '—')}${a.logged_in ? ' · ' + esc(remainText(a)) : ''}</span></div>
        <div class="tc-tags">${state}${renew}${a.configs ? '<span class="tag">' + a.configs + ' 条配置</span>' : ''}</div>
        ${a.last_error ? '<p class="form-hint">' + esc(a.last_error) + '</p>' : ''}
        ${a.can_refresh ? '' : '<p class="form-hint">没有会话密钥（手动填的 token / 老版本迁移过来的账号）：' +
          'token 到期只能重新登录。点右上角「重新登录」用扫码或 Cookie 登一次，之后就能自动续期。</p>'}
        <div class="tc-foot">
          <span class="tc-meta">${a.refresh_count ? '已续期 ' + a.refresh_count + ' 次' : '未续期过'}</span>
          <div class="tc-acts">
            <button class="btn btn-sm" data-acc-default ${a.is_default ? 'disabled' : ''}>设为默认</button>
            <button class="btn btn-sm" data-acc-refresh ${a.can_refresh ? '' : 'disabled'}
              title="用保存的会话密钥换一枚新 token">续期</button>
            <button class="btn btn-sm" data-acc-renew ${a.can_refresh ? '' : 'disabled'}
              title="到期前自动续期">${a.auto_refresh ? '关续期' : '开续期'}</button>
            <button class="btn btn-sm" data-acc-logout>退出</button>
            <button class="btn btn-sm btn-danger" data-acc-del title="删除账号">${icon('trash', 11)}</button>
          </div>
        </div>
      </div>`;
  }

  function loginHtml(st) {
    const accounts = st.accounts || [];
    const live = accounts.filter((a) => a.logged_in).length;
    const head = `
      <div class="card-head">
        <h2>天翼云盘账号${accounts.length ? '（' + live + '/' + accounts.length + ' 在线）' : ''}</h2>
        <p class="card-sub">token 剩不到 ${st.renew_ahead_hours || 24} 小时会自动续期
          （用登录时保存的会话密钥换新 token）；会话失效才需要重新扫码</p>
      </div>`;
    const add = `
      <div class="inline" style="margin-top:12px">
        <button class="btn btn-primary btn-sm" data-login>${icon('scan', 13)} 扫码添加账号</button>
        <button class="btn btn-sm" data-cookie>${icon('key', 13)} 用 Cookie 登录</button>
        <button class="btn btn-sm" data-token>${icon('key', 13)} 手动填 token</button>
      </div>
      <p class="form-hint">扫码没反应就用 Cookie 登录：浏览器登录 cloud.189.cn → F12 →
        Application → Cookies → 复制 <b>COOKIE_LOGIN_USER</b> 的值。
        扫码 / Cookie 登录会把会话密钥一起存下来，<b>之后 token 到期能自动续期</b>；
        手动填的 token 没有会话密钥，到期只能重新填。</p>`;
    if (!accounts.length) {
      return head + `<div class="empty"><span class="empty-ico">${icon('user', 20)}</span>
        还没有登录任何天翼云盘账号</div>` + add;
    }
    return head + `<div class="task-grid" style="margin-top:10px">${
      accounts.map(accCard).join('')}</div>` + add;
  }

  /* ------------------------------------------------------------ 配置卡片 */
  function accLabel(accountId) {
    const a = ACCOUNTS.find((x) => x.id === accountId)
      || ACCOUNTS.find((x) => x.is_default);
    if (!a) return '';
    return a.display || a.login_name || a.id;
  }

  function configCard(c) {
    const running = c.status === 'running';
    const tag = running ? '<span class="tag brand">运行中</span>'
      : c.status === 'error' ? '<span class="tag err">失败</span>' : '';
    const exts = c.all_files ? '全部文件' : (c.media_exts || DEFAULT_EXTS);
    const who = accLabel(c.account);
    const family = String(c.remote_id || '').indexOf('fam') === 0;
    return `
      <div class="task-card" data-cfg="${esc(c.id)}">
        <div class="tc-head">
          <div class="tc-name">${esc(c.name || c.remote_id)}</div>
          <button class="icon-btn" data-edit title="设置">${icon('pen', 14)}</button>
        </div>
        <div class="tc-row"><span class="tc-ico v3">${icon('folder', 12)}</span>
          <span class="tc-key">源</span>
          <span class="tc-val mono">${esc(c.remote_path || ('id=' + c.remote_id))}</span></div>
        <div class="tc-row"><span class="tc-ico v2">${icon('drive', 12)}</span>
          <span class="tc-key">目标</span>
          <span class="tc-val mono">${esc(c.local_dir ? '/strm/' + c.local_dir.replace(/^\/+/, '') : '/strm')}</span></div>
        <div class="tc-row"><span class="tc-ico v1">${icon('layers', 12)}</span>
          <span class="tc-key">后缀</span>
          <span class="tc-val" title="${esc(exts)}">${esc(String(exts).slice(0, 46))}${String(exts).length > 46 ? '…' : ''}</span></div>
        <div class="tc-tags">${who ? '<span class="tag">' + esc(who) + '</span>' : ''}${family ? '<span class="tag brand">家庭云</span>' : ''}${c.all_files ? '<span class="tag">不过滤</span>' : ''}${tag}</div>
        <div class="tc-foot">
          <span class="tc-meta">${c.files || 0} 个文件${c.last_run ? ' · ' + N.stamp(new Date(c.last_run * 1000)) : ''}</span>
          <div class="tc-acts">
            <button class="btn btn-sm" data-inc>${icon('play', 11)} 增量</button>
            <button class="btn btn-sm" data-full title="strm 内容全部重新写一遍">${icon('refresh', 11)} 全量</button>
            <button class="btn btn-sm" data-rebuild title="先删掉这条配置生成过的 strm，再从头生成">${icon('broom', 11)} 全新</button>
            <button class="btn btn-sm btn-danger" data-del>${icon('trash', 11)}</button>
          </div>
        </div>
      </div>`;
  }

  /* ------------------------------------------------------------ 事件日志 */
  const ELOG_LABEL = {
    login_start: '登录', login: '已登录', login_fail: '登录失败', logout: '退出',
    refresh: '续期', refresh_fail: '续期失败',
    account_default: '默认账号', account_update: '账号设置', account_delete: '删账号',
    config_save: '配置', config_delete: '删除',
    run_start: '开始', run_done: '完成', run_warn: '警告', run_error: '失败',
  };

  function eventRow(e) {
    const cls = e.level === 'err' ? 'err' : e.level === 'warn' ? 'warn' : 'ok';
    const at = e.created_at ? N.stamp(new Date(e.created_at * 1000)) : '';
    return `<div class="elog-row">
      <span class="tag ${cls}">${esc(ELOG_LABEL[e.kind] || e.kind || '事件')}</span>
      <span class="elog-msg">${esc(e.message || '')}</span>
      <span class="elog-time mono">${esc(at)}</span>
    </div>`;
  }

  /* ------------------------------------------------------------ 页面 */
  function page() {
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>天翼云盘</h1>
            <p>登录天翼云盘（189）→ 选云端目录（个人云 / 家庭云）→ 生成 strm 到本地映射目录。
              strm 内容 = <span class="mono">请求头 + /p189/play/&lt;账号&gt;/&lt;fileId&gt;</span>，
              播放时由本服务实时换新鲜直链再 302（直链带时效和凭据，不落盘）。<br>
              可以登录<b>多个账号</b>，每条配置绑一个账号；token 快到期时用登录时保存的会话密钥
              <b>自动续期</b>，会话失效才需要重新扫码。<br>
              <b>增量</b>只补新增；<b>全量</b>把索引里的 strm 全部重写；<b>全新</b>先删掉这条配置生成过的
              strm（不动别人的）再从头生成。<br>
              <b>家庭云</b>可以直接整选：每个家庭生成到各自同名的子目录。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-events-refresh>${icon('refresh', 14)} 刷新</button>
            <button class="btn btn-primary btn-sm" data-add>${icon('plus', 14)} 添加配置</button>
          </div>
        </div>

        <div class="strm-stats" id="c189Stats">
          ${statCard('user', 'v1', '登录状态', '读取中…')}
          ${statCard('layers', 'v2', '生成配置', '—')}
          ${statCard('play', 'v3', '已生成 strm', '—')}
          ${statCard('clock', 'v4', 'token 到期', '—')}
        </div>

        <div class="card" id="c189Login"><span class="form-hint">加载中…</span></div>

        <div id="c189Configs">
          <div class="card"><div class="empty">
            <span class="empty-ico">${icon('box', 20)}</span>读取配置中…</div></div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>事件日志</h2>
            <p class="card-sub">登录、配置变更、每次生成写了多少都在这里，刷新页面不会丢</p>
          </div>
          <div class="inline" style="margin-bottom:8px">
            <button class="btn btn-sm" data-events-refresh>${icon('refresh', 12)} 刷新</button>
            <button class="btn btn-sm" data-events-clear>${icon('broom', 12)} 清空</button>
            <span class="form-hint" id="c189EventHint"></span>
          </div>
          <div class="elog" id="c189Events"><span class="form-hint">加载中…</span></div>
        </div>`,
      mount(root) {
        bind(root);
        reload(root);
      }
    };
  }

  function reload(root) {
    if (!root || !root.isConnected) return;
    window.API.cloud189().then((st) => {
      if (!root.isConnected) return;
      const accounts = st.accounts || [];
      ACCOUNTS = accounts;
      const live = accounts.filter((a) => a.logged_in).length;
      // 最近到期的那个账号：顶部「token 到期」显示它，一眼能看出什么时候要续
      const soon = accounts.filter((a) => a.logged_in && a.expires_at)
        .sort((a, b) => a.expires_at - b.expires_at)[0];
      root.querySelector('#c189Stats').innerHTML =
        statCard('user', 'v1', '登录状态',
          accounts.length ? live + '/' + accounts.length + ' 个账号' : '未登录') +
        statCard('layers', 'v2', '生成配置', String((st.configs || []).length)) +
        statCard('play', 'v3', '已生成 strm', String(st.files || 0)) +
        statCard('clock', 'v4', 'token 到期', soon ? soon.expires_text : '—');
      root.querySelector('#c189Login').innerHTML = loginHtml(st);
      const box = root.querySelector('#c189Configs');
      const list = st.configs || [];
      CONFIGS = list;
      box.innerHTML = list.length
        ? `<div class="task-grid">${list.map(configCard).join('')}</div>`
        : `<div class="card"><div class="empty">
             <span class="empty-ico">${icon('box', 20)}</span>
             还没有生成配置：点右上角「添加配置」，选一个天翼云盘目录和本地映射目录
           </div></div>`;
      // 刷新页面时还有任务在跑：接着盯，跑完自动恢复
      list.forEach((c) => { if (c.status === 'running') watchConfig(root, c.id, 0); });
    }).catch((err) => {
      const el = root.querySelector('#c189Login');
      if (el) el.innerHTML = '<span class="form-hint">读取失败：' + esc(err.message) + '</span>';
    });
    loadEvents(root);
  }

  function loadEvents(root) {
    const box = root.querySelector('#c189Events');
    const hint = root.querySelector('#c189EventHint');
    if (!box) return;
    window.API.cloud189Events(80).then((d) => {
      const list = (d && d.events) || [];
      box.innerHTML = list.length ? list.map(eventRow).join('')
        : '<span class="form-hint">还没有事件（登录或跑一次生成就会记下来）</span>';
      if (hint) hint.textContent = '最近 ' + list.length + ' 条';
    }).catch((err) => {
      box.innerHTML = '<span class="form-hint">读取失败：' + esc(err.message) + '</span>';
    });
  }

  /* ------------------------------------------------------------ 扫码登录 */
  function loginModal(accountId) {
    const target = accountId || '';
    const who = target ? ACCOUNTS.find((x) => x.id === target) : null;
    const box = N.modal.open({
      title: who ? '重新登录：' + (who.display || who.id) : '扫码添加天翼云盘账号',
      body: '<div id="qrBox" class="qr-box"><span class="form-hint">正在获取二维码…</span></div>' +
        (who ? '<p class="form-hint">扫码确认后，这个账号的 token 会被换掉（配置不变）</p>' : '') +
        '<p class="form-hint" id="qrHint"></p>',
      footer: '<button class="btn" data-close-modal>关闭</button>'
    });
    let timer = null;
    let tries = 0;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const boxEl = box.querySelector('#qrBox');
    const hint = box.querySelector('#qrHint');

    function poll(uuid) {
      timer = setInterval(() => {
        // 最多等 5 分钟：用户走开了别再一直打接口
        if (++tries > 150) {
          stop();
          if (hint) hint.textContent = '等待超时，关掉重新点「扫码登录」';
          return;
        }
        window.API.cloud189LoginPoll(uuid, target).then((r) => {
          if (r.status === 'ok') {
            stop();
            N.toast('天翼云盘登录成功' + (r.account ? '：' + r.account : ''), 'ok');
            N.modal.close();
            window.App.rerender();
          } else if (r.status === 'expired') {
            stop();
            if (hint) hint.textContent = '二维码已失效，关掉重新点「扫码登录」';
          } else if (r.status === 'error') {
            stop();
            // 后端会把原始响应放在 detail 里，鼠标悬停能看到
            if (hint) hint.textContent = '登录失败：' + (r.message || '未知错误');
            if (hint && r.detail) hint.title = r.detail;
          } else if (hint) {
            const sec = tries * 2;
            hint.textContent = '等待手机确认…（用「天翼云盘」App 右上角扫一扫，已等 ' +
              sec + ' 秒）' +
              (sec >= 40 ? '。手机上已点确认的话，先别关，服务端还在换 token' : '');
          }
        }).catch((err) => {
          stop();
          if (hint) hint.textContent = '轮询失败：' + err.message;
        });
      }, 2000);
    }

    window.API.cloud189LoginStart().then((d) => {
      boxEl.innerHTML = qrSvg(d.matrix);
      if (hint) hint.textContent = '用「天翼云盘」App 扫一扫；手机上会打开一个页面，' +
        '要点里面的「确认登录」，光扫码不点确认服务端收不到';
      if (d.uuid) poll(d.uuid);
    }).catch((err) => {
      boxEl.innerHTML = '<span class="form-hint">二维码获取失败：' + esc(err.message) +
        '（也可以用「用 Cookie 登录」或「手动填 token」）</span>';
    });
    // 关掉弹窗就把轮询停掉，别在后台一直打接口。
    // 弹窗可以嵌套，所以要盯自己那一层的 wrap，不能写死 #modalWrap。
    const wrap = box.closest('.modal-wrap') || document.querySelector('#modalWrap');
    if (wrap) {
      const obs = new MutationObserver(() => {
        if (wrap.hidden) { stop(); obs.disconnect(); }
      });
      obs.observe(wrap, { attributes: true, attributeFilter: ['hidden'] });
    }
  }

  function cookieModal(accountId) {
    const target = accountId || '';
    const who = target ? ACCOUNTS.find((x) => x.id === target) : null;
    const box = N.modal.open({
      title: who ? '重新登录（Cookie）：' + (who.display || who.id) : '用浏览器 Cookie 登录',
      body: `
        <div class="form-row">
          <label>COOKIE_LOGIN_USER</label>
          <textarea class="input mono" id="c189Cookie" rows="4" spellcheck="false"
            placeholder="粘贴 COOKIE_LOGIN_USER 的值，或整条 Cookie"></textarea>
          <span class="form-hint">浏览器先登录 cloud.189.cn → F12 → Application → Cookies →
            cloud.189.cn → 复制 <b>COOKIE_LOGIN_USER</b> 的值。服务端会取出里面的
            SESSION_KEY 换成 accessToken，并<b>把会话密钥留着自动续期</b>；cookie 本身不存。</span>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>验证并登录</button>',
      onOk() {
        const cookie = box.querySelector('#c189Cookie').value.trim();
        if (!cookie) { N.toast('请先粘贴 cookie', 'err'); return false; }
        window.API.cloud189SetCookie(cookie, target).then((r) => {
          N.toast('天翼云盘登录成功' + (r.account ? '：' + r.account : ''), 'ok');
          N.modal.close();
          window.App.rerender();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      }
    });
  }

  function tokenModal(accountId) {
    const target = accountId || '';
    const who = target ? ACCOUNTS.find((x) => x.id === target) : null;
    const box = N.modal.open({
      title: who ? '重新填 token：' + (who.display || who.id) : '手动填写 accessToken',
      body: `
        <div class="form-row">
          <label>accessToken</label>
          <textarea class="input mono" id="c189Token" rows="4" spellcheck="false"
            placeholder="粘贴天翼云盘接口返回的 accessToken"></textarea>
          <span class="form-hint">只在扫码 / Cookie 都不可用时用；保存前会先验证一次，错的 token 不会写进库。
            <b>这条路没有会话密钥，token 到期后不能自动续期。</b></span>
        </div>
        <div class="form-row">
          <label>有效期（可选，秒）</label>
          <input class="input" id="c189TokenTtl" type="number" min="0" placeholder="3600">
          <span class="form-hint">不知道就留空，页面会显示「未知」，过期后重新填</span>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存并验证</button>',
      onOk() {
        const token = box.querySelector('#c189Token').value.trim();
        const ttl = Number(box.querySelector('#c189TokenTtl').value) || 0;
        if (!token) { N.toast('请先粘贴 token', 'err'); return false; }
        window.API.cloud189SetToken(token, ttl, target).then((r) => {
          N.toast('token 已保存' + (r.account ? '：' + r.account : ''), 'ok');
          N.modal.close();
          window.App.rerender();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      }
    });
  }

  /* ------------------------------------------------------------ 配置弹窗 */
  function accountOptions(selected) {
    const fallback = ACCOUNTS.find((x) => x.is_default) || ACCOUNTS[0] || null;
    const cur = selected || (fallback ? fallback.id : '');
    return ACCOUNTS.map((a) => `<option value="${esc(a.id)}"${a.id === cur ? ' selected' : ''}>${
      esc(a.display || a.login_name || a.id)}${a.is_default ? '（默认）' : ''}${
      a.logged_in ? '' : ' · 未登录'}</option>`).join('');
  }

  function configForm(cfg) {
    const c = cfg || {
      id: '', account: '', name: '', remote_id: '-11', remote_path: '', local_dir: '',
      base_url: '', media_exts: DEFAULT_EXTS, all_files: false
    };
    let remote = { id: c.remote_id || '-11', path: c.remote_path || '' };
    let local = c.local_dir || '';
    const fallbackAcc = (ACCOUNTS.find((x) => x.is_default) || ACCOUNTS[0] || {}).id || '';
    let account = c.account || fallbackAcc;

    const box = N.modal.open({
      title: cfg ? '配置 · ' + (cfg.name || cfg.remote_id) : '添加天翼云盘配置',
      wide: true,
      body: `
        <div class="form-grid two">
          <div class="form-row">
            <label>配置名称</label>
            <input class="input" id="c189Name" value="${esc(c.name || '')}"
              placeholder="例如：天翼电影">
          </div>
          <div class="form-row">
            <label>使用的账号</label>
            <select class="input" id="c189Account">${accountOptions(account)}</select>
            <span class="form-hint">生成、播放都走这个账号；账号不可用时这条配置会报错</span>
          </div>
        </div>

        <div class="form-row">
          <label>STRM 请求头</label>
          <input class="input" id="c189Base" value="${esc(c.base_url || '')}"
            placeholder="留空用全局地址">
          <span class="form-hint">客户端（Emby / 播放器）能访问到的本服务地址</span>
        </div>

        <div class="form-row">
          <label>云端目录（个人云 / 家庭云）</label>
          <div class="inline">
            <input class="input mono" id="c189Remote" value="${esc(c.remote_path || '个人云')}" readonly>
            <button class="btn btn-sm" data-pick-remote>${icon('folder', 13)} 选择目录</button>
          </div>
          <span class="form-hint" id="c189RemoteId">目录 id：${esc(c.remote_id || '-11')}</span>
        </div>

        <div class="form-row">
          <label>本地 STRM 输出目录（映射目录内）</label>
          <div class="inline">
            <input class="input mono" id="c189Local" value="${esc(local)}" placeholder="例如：天翼">
            <button class="btn btn-sm" data-pick-local>${icon('folder', 13)} 选择</button>
          </div>
          <span class="form-hint">填相对 STRM_ROOT 的子目录；空 = 直接放在 strm 根目录下</span>
        </div>

        <div class="form-row">
          <label>STRM 内容预览</label>
          <div class="code-block" id="c189Preview">${esc(previewUrl(c.base_url, account, '1234567890', '影片.mkv'))}</div>
        </div>

        <div class="form-grid two">
          <div class="form-row">
            <label>媒体文件扩展名</label>
            <input class="input" id="c189Exts" value="${esc(c.media_exts || DEFAULT_EXTS)}">
            <span class="form-hint">命中这些后缀才生成 strm，英文逗号分隔</span>
          </div>
          <div class="form-row">
            <label>不过滤</label>
            <label class="switch" style="margin-top:6px"><input type="checkbox" id="c189All"
              ${c.all_files ? 'checked' : ''}><span class="track"></span></label>
            <span class="form-hint">打开后云盘里所有文件都生成 strm（含图片、字幕）</span>
          </div>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存配置</button>',
      onOk() {
        const name = box.querySelector('#c189Name').value.trim();
        if (!local.trim()) { N.toast('请选择本地 STRM 输出目录', 'err'); return false; }
        const payload = {
          id: c.id, name: name || remote.path || c.remote_id,
          account: account,
          remote_id: remote.id || '-11', remote_path: remote.path || '',
          local_dir: local.trim(),
          base_url: box.querySelector('#c189Base').value.trim(),
          media_exts: box.querySelector('#c189Exts').value.trim(),
          all_files: box.querySelector('#c189All').checked
        };
        window.API.saveCloud189Config(payload).then(() => {
          D.logs.add('info', '天翼云盘配置已保存：<b>' + esc(payload.name) + '</b>');
          N.toast('配置已保存', 'ok');
          N.modal.close();
          window.App.rerender();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      },
      mount(m) {
        const base = m.querySelector('#c189Base');
        const pv = m.querySelector('#c189Preview');
        const syncPreview = () => {
          if (!pv) return;
          pv.textContent =
            previewUrl(base.value.trim(), account, '1234567890', '影片.mkv');
        };
        base.addEventListener('input', syncPreview);
        const sel = m.querySelector('#c189Account');
        if (sel) {
          sel.addEventListener('change', () => {
            account = sel.value;
            syncPreview();
          });
        }
        // 选目录会再开一层弹窗（选择器）；这层表单的 DOM 会保留，
        // 但万一节点被外部重绘掉了，也不要抛错把整个操作打断。
        m.querySelector('[data-pick-remote]').onclick = () => {
          window.Shell.pickCloud189Dir(account).then((p) => {
            if (!p) return;
            remote = { id: String(p.id), path: p.path };
            const input = m.querySelector('#c189Remote');
            if (input) input.value = p.path;
            const hint = m.querySelector('#c189RemoteId');
            if (hint) hint.textContent = '目录 id：' + p.id;
            const nameInput = m.querySelector('#c189Name');
            if (nameInput && !nameInput.value.trim() && p.name) nameInput.value = p.name;
          }).catch((err) => N.toast(err.message, 'err'));
        };
        m.querySelector('[data-pick-local]').onclick = () => {
          window.Shell.pickLocalDir(local, { relative: true }).then((p) => {
            if (p === null || p === undefined) return;
            local = p;
            const input = m.querySelector('#c189Local');
            if (input) input.value = p;
          }).catch((err) => N.toast(err.message, 'err'));
        };
        m.querySelector('#c189Local').addEventListener('input', (e) => {
          local = e.target.value.trim();
        });
      }
    });
  }

  function previewUrl(base, account, fileId, name) {
    const b = (base || location.origin).replace(/\/+$/, '');
    return b + '/p189/play/' + (account ? account + '/' : '') + fileId +
      '/' + encodeURIComponent(name || 'x.mkv');
  }

  /* ------------------------------------------------------------ 运行 */
  function watchConfig(root, id, tries) {
    const round = tries || 0;
    if (round > 100) return;  // 最多盯 5 分钟
    setTimeout(() => {
      window.API.cloud189Configs().then((d) => {
        const row = ((d && d.configs) || []).find((x) => x.id === id);
        if (row && row.status === 'running') {
          watchConfig(root, id, round + 1);
        } else {
          reload(root);
        }
      }).catch(() => watchConfig(root, id, round + 1));
    }, 3000);
  }

  const MODE_TEXT = { inc: '增量', full: '全量', rebuild: '全新' };

  function runConfig(root, cfg, mode) {
    const text = MODE_TEXT[mode] || '增量';
    const go = () => {
      N.toast(text + '生成已提交');
      window.API.runCloud189Config(cfg.id, mode).then(() => {
        watchConfig(root, cfg.id, 0);
      }).catch((err) => N.toast(err.message, 'err'));
    };
    if (mode === 'rebuild') {
      N.confirm('全新生成',
        '会先删除「' + esc(cfg.name || cfg.remote_id) + '」这条配置生成过的**所有 .strm**' +
        '（只删它自己记录的，别的配置 / 别的账号的 strm 不动；nfo、字幕、海报等保留），' +
        '然后再从云盘重新生成一遍。',
        { danger: true, okText: '开始全新生成' }).then((ok) => { if (ok) go(); });
      return;
    }
    go();
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-add]')) { configForm(null); return; }
      if (e.target.closest('[data-events-refresh]')) { loadEvents(root); return; }
      if (e.target.closest('[data-events-clear]')) {
        N.confirm('清空事件日志', '只删记录，不影响已生成的 strm。',
          { danger: true, okText: '清空' }).then((ok) => {
            if (!ok) return;
            window.API.clearCloud189Events().then(() => {
              N.toast('已清空', 'ok');
              loadEvents(root);
            }).catch((err) => N.toast(err.message, 'err'));
          });
        return;
      }
      if (e.target.closest('[data-login]')) { loginModal(); return; }
      if (e.target.closest('[data-cookie]')) { cookieModal(); return; }
      if (e.target.closest('[data-token]')) { tokenModal(); return; }
      if (e.target.closest('[data-logout]')) {
        N.confirm('退出天翼云盘', '只清掉服务端保存的 token，账号记录和已生成的 strm 都不受影响。',
          { danger: true, okText: '退出' }).then((ok) => {
            if (!ok) return;
            window.API.cloud189Logout().then(() => {
              N.toast('已退出', 'ok');
              window.App.rerender();
            }).catch((err) => N.toast(err.message, 'err'));
          });
        return;
      }
      // ---------------- 账号卡上的按钮 ----------------
      const accCardEl = e.target.closest('[data-accid]');
      if (accCardEl) {
        const aid = accCardEl.getAttribute('data-accid');
        const acc = ACCOUNTS.find((x) => x.id === aid) || { id: aid };
        const name = acc.display || acc.login_name || aid;
        if (e.target.closest('[data-acc-relogin]')) { loginModal(aid); return; }
        if (e.target.closest('[data-acc-refresh]')) {
          N.toast('正在续期…');
          window.API.cloud189Refresh(aid).then(() => {
            N.toast('续期成功', 'ok');
            reload(root);
          }).catch((err) => N.toast(err.message, 'err'));
          return;
        }
        if (e.target.closest('[data-acc-renew]')) {
          window.API.cloud189AutoRefresh(aid, !acc.auto_refresh).then(() => {
            N.toast(acc.auto_refresh ? '已关闭自动续期' : '已开启自动续期', 'ok');
            reload(root);
          }).catch((err) => N.toast(err.message, 'err'));
          return;
        }
        if (e.target.closest('[data-acc-default]')) {
          window.API.cloud189SetDefault(aid).then(() => {
            N.toast('默认账号已切换', 'ok');
            reload(root);
          }).catch((err) => N.toast(err.message, 'err'));
          return;
        }
        if (e.target.closest('[data-acc-logout]')) {
          N.confirm('退出「' + esc(name) + '」',
            '只清掉这个账号的 token 与会话密钥，配置和已生成的 strm 都保留；重新登录后继续可用。',
            { danger: true, okText: '退出' }).then((ok) => {
            if (!ok) return;
            window.API.cloud189AccountLogout(aid).then(() => {
              N.toast('已退出', 'ok');
              reload(root);
            }).catch((err) => N.toast(err.message, 'err'));
          });
          return;
        }
        if (e.target.closest('[data-acc-del]')) {
          const extra = acc.configs
            ? '这个账号下还有 ' + acc.configs + ' 条配置，会跟着一起删掉（已生成的 strm 保留）。'
            : '账号记录会被删除（已生成的 strm 保留）。';
          N.confirm('删除账号「' + esc(name) + '」', extra,
            { danger: true, okText: '删除' }).then((ok) => {
            if (!ok) return;
            window.API.cloud189DeleteAccount(aid, !!acc.configs).then(() => {
              N.toast('账号已删除', 'ok');
              window.App.rerender();
            }).catch((err) => N.toast(err.message, 'err'));
          });
          return;
        }
        return;
      }
      const card = e.target.closest('[data-cfg]');
      if (!card) return;
      const id = card.getAttribute('data-cfg');
      const cfg = CONFIGS.find((x) => x.id === id)
        || { id, name: (card.querySelector('.tc-name') || {}).textContent || id };
      if (e.target.closest('[data-edit]')) { editConfig(id); return; }
      if (e.target.closest('[data-inc]')) { runConfig(root, cfg, 'inc'); return; }
      if (e.target.closest('[data-full]')) { runConfig(root, cfg, 'full'); return; }
      if (e.target.closest('[data-rebuild]')) { runConfig(root, cfg, 'rebuild'); return; }
      if (e.target.closest('[data-del]')) {
        N.confirm('删除配置', '删除「' + esc(cfg.name) + '」。已生成的 strm 是否一起删？',
          { danger: true, okText: '只删配置' }).then((ok) => {
            if (!ok) return;
            window.API.removeCloud189Config(id, false).then(() => {
              N.toast('配置已删除（strm 保留）', 'ok');
              window.App.rerender();
            }).catch((err) => N.toast(err.message, 'err'));
          });
      }
    });
  }

  function editConfig(id) {
    window.API.cloud189Configs().then((d) => {
      const row = ((d && d.configs) || []).find((x) => x.id === id);
      if (!row) { N.toast('配置不存在，刷新一下', 'err'); return; }
      configForm(row);
    }).catch((err) => N.toast(err.message, 'err'));
  }

  window.ToolCloud189 = { page };

  // 注册到侧栏与路由表：这个文件由 /plugin-assets/cloud189.js 按需加载，
  // 插件没装/没启用时脚本根本不会下载，导航项自然也就不会出现。
  if (window.Shell && window.Shell.registerPlugin) {
    window.Shell.registerPlugin({
      id: 'cloud189', hash: '#/tools/cloud189', name: '天翼云盘',
      ico: 'box', section: '工具', view: page
    });
  }
})();
