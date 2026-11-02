/* ==========================================================================
   tool-share.js · 115 分享转存
   --------------------------------------------------------------------------
   需求 4：分享转存中添加一个账号选择按钮，账号从 115 助手获取，
   保存后选择账号作为默认账号。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  const SH_ST = { ready: '就绪', partial: '部分转存', pending: '转存中', failed: '失败' };
  const SH_TAG = { ready: 'ok', partial: 'warn', pending: 'warn', failed: 'err' };

  let items = [];
  let settings = {};
  let busy = false;

  function account() {
    return D.db.defaultAccount('share');
  }

  function page() {
    const accounts = D.db.selectable();
    const acc = account();
    const accRow = D.db.account(acc);

    return {
      html: `
        <div class="page-head">
          <div>
            <h1>115 分享转存</h1>
            <p>粘贴 115 分享链接 → 转存到「接收目录」→ 用转存好的文件创建自己的长期分享
              → strm 指向自己的分享，播放时从分享通道解析直链。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-refresh>${icon('refresh', 14)} 刷新</button>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>账号</h2>
            <p class="card-sub">分享转存使用哪个账号执行（来自 115 助手）</p>
          </div>
          <div class="pill-tabs" id="shAccountPills">
            ${window.Shell.accountPills(acc)}
          </div>
          <p class="form-hint">保存后作为分享转存的默认账号</p>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>接收目录</h2>
            <p class="card-sub">分享内容转存到 115 的哪个目录</p>
          </div>
          <div class="stat-strip">
            <div class="ss"><div class="ss-label">使用账号</div>
              <div class="ss-val">${esc(accRow ? accRow.label : '主账号')}</div></div>
            <div class="ss"><div class="ss-label">接收目录</div>
              <div class="ss-val mono" id="shDir">${settings.dir_cid
                ? esc(settings.dir_path || settings.dir_name || ('id=' + settings.dir_cid))
                : '自动创建'}</div></div>
          </div>
          <div class="inline mt12">
            <button class="btn btn-sm" data-pick-remote>${icon('folder', 13)} 浏览网盘目录</button>
            <button class="btn btn-sm" data-auto-dir>${icon('plus', 13)} 自动创建默认目录</button>
          </div>
          <div class="inline" style="margin-top:6px">
            <input class="input mono" id="shDirInput"
              value="${esc(settings.dir_path || settings.dir_name || '')}"
              placeholder="或直接填 115 目录 id / 目录链接">
            <button class="btn btn-sm" data-set-dir>${icon('check', 13)} 设为接收目录</button>
          </div>
          <div class="form-row" style="margin-top:12px">
            <label>本地 STRM 输出目录</label>
            <div class="inline">
              <input class="input mono" id="shStrmDir" value="${esc(settings.strm_dir || 'share')}">
              <button class="btn btn-sm" data-pick-local>${icon('folder', 13)} 选择</button>
              <button class="btn btn-sm" data-save-local>${icon('check', 13)} 保存</button>
            </div>
            <span class="form-hint">相对映射目录（STRM_ROOT）；只影响之后生成的 strm</span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>转存并生成 strm</h2>
            <p class="card-sub">每行一条分享链接，支持带 #提取码 / 短横线格式</p>
          </div>
          <div class="form-row">
            <textarea class="input" id="shUrl" rows="2" spellcheck="false"
              placeholder="https://115.com/s/xxxxx?password=yyyy"></textarea>
          </div>
          <div class="switch-row">
            <div class="sr-main">
              <div class="sr-title">直接生成（不转存）</div>
              <div class="sr-desc">只生成 strm，直接指向原分享里的文件 —— 不占用自己网盘空间，
                原分享失效时会随之失效；关闭则按原流程转存到自己网盘再播</div>
            </div>
            <label class="switch"><input type="checkbox" id="shDirect"><span class="track"></span></label>
          </div>
          <div class="inline">
            <button class="btn btn-primary" data-add-share>${icon('download', 14)} 转存并生成 strm</button>
            <span class="form-hint" id="shMsg"></span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>转存记录</h2>
            <p class="card-sub">共 ${items.length} 条</p>
          </div>
          <div class="row-list" id="shList">
            ${renderItems()}
          </div>
        </div>`,
      mount(root) {
        bind(root);
        loadData(acc);
      }
    };
  }

  function renderItems() {
    if (!items.length) {
      return `<div class="empty"><span class="empty-ico">${icon('download', 20)}</span>
        还没有转存过分享</div>`;
    }
    return items.map((it) => `
      <div class="row-item" data-share="${esc(it.share_code)}">
        <span class="stat-ico v2" style="width:38px;height:38px;border-radius:11px">${icon('link', 16)}</span>
        <div class="row-main">
          <div class="row-title">${esc(it.name || it.share_code)}
            <span class="tag ${SH_TAG[it.status] || ''}">${esc(SH_ST[it.status] || it.status)}</span>
            ${it.direct ? '<span class="tag">直接生成</span>' : ''}
          </div>
          <div class="row-desc">${esc(it.done || 0)} / ${esc(it.total || 0)} 个文件
            ${it.my_share_code ? ' · 我的分享 ' + esc(it.my_share_code) : ''}</div>
          ${it.message ? `<div class="row-desc">${esc(it.message)}</div>` : ''}
        </div>
        <div class="row-actions">
          <button class="btn btn-sm btn-danger" data-del-share>${icon('trash', 12)}</button>
        </div>
      </div>`).join('');
  }

  async function loadData(acc) {
    try {
      const d = await window.API.share(acc);
      settings = d.settings || {};
      items = d.items || [];
      const box = N.$('#shList');
      if (box) box.innerHTML = renderItems();
      // 接收目录：顶部状态 + 输入框都显示「当前选中的目录」。
      // 以前这里找的是 #shDir，但模板里根本没这个 id，所以选完目录
      // 数字永远停在「自动创建」——用户以为没选中。
      const dirText = settings.dir_path || settings.dir_name ||
        (settings.dir_cid ? 'id=' + settings.dir_cid : '');
      const dirEl = N.$('#shDir');
      if (dirEl) dirEl.textContent = settings.dir_cid ? dirText : '自动创建';
      const dirInput = N.$('#shDirInput');
      if (dirInput && dirInput !== document.activeElement) {
        dirInput.value = settings.dir_cid ? dirText : '';
      }
      // 本地输出目录同理：首屏渲染时 settings 还是空的，拉到真实值后补上
      // （不然保存过的目录一刷新就变回默认的 share，看着像没存上）
      const strmEl = N.$('#shStrmDir');
      if (strmEl && strmEl !== document.activeElement) {
        strmEl.value = settings.strm_dir || 'share';
      }
    } catch (err) {
      const box = N.$('#shList');
      if (box) {
        box.innerHTML = `<div class="empty"><span class="empty-ico">${icon('warn', 20)}</span>
          加载失败：${esc(err.message)}</div>`;
      }
    }
  }

  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-refresh]')) {
        loadData(account());
        N.toast('已刷新');
        return;
      }
      if (e.target.closest('[data-add-share]')) { addShare(root); return; }
      if (e.target.closest('[data-set-dir]')) { setDir(root); return; }
      if (e.target.closest('[data-auto-dir]')) { autoDir(); return; }
      if (e.target.closest('[data-pick-remote]')) { pickRemote(); return; }
      if (e.target.closest('[data-pick-local]')) { pickLocal(root); return; }
      if (e.target.closest('[data-save-local]')) { saveLocal(root); return; }
      const del = e.target.closest('[data-del-share]');
      if (del) {
        const row = e.target.closest('[data-share]');
        const code = row.getAttribute('data-share');
        const rec = items.find((x) => x.share_code === code) || {};
        N.confirm('删除记录', rec.direct
          ? '删除这条「直接生成」记录？只删本地 strm 与本条记录，不会动原分享里的文件。'
          : '删除这条转存记录？115 上已转存的文件也会一并删除（网盘里不再保留）。',
          { danger: true, okText: '删除' }).then((ok) => {
            if (!ok) return;
            window.API.removeShare(code, account()).then(() => {
              D.logs.add('warn', '删除分享转存 ' + esc(code));
              N.toast('已删除', 'ok');
              loadData(account());
            }).catch((err) => N.toast(err.message, 'err'));
          });
      }
    });

    // 账号选择
    N.bindOnce(root, 'click', (e) => {
      const pick = e.target.closest('[data-acc-pick]');
      if (!pick) return;
      // 主账号的 name 是空字符串，属性里用 @main 占位，这里取真实名字
      const name = pick.getAttribute('data-acc-name') || '';
      root.querySelectorAll('[data-acc-pick]').forEach((b) => b.classList.remove('active'));
      pick.classList.add('active');
      const patch = Object.assign({}, D.db.settings(), { default_account_share: name });
      window.API.saveSettings(patch).then(() => {
        D.logs.add('info', '分享转存默认账号：<b>' + esc(D.db.label(name)) + '</b>');
        N.toast('已保存，默认账号：' + D.db.label(name), 'ok');
        window.App.reload();
      }).catch((err) => N.toast(err.message, 'err'));
    });
  }

  function addShare(root) {
    const raw = (N.$('#shUrl') || {}).value || '';
    const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { N.toast('请先粘贴分享链接', 'err'); return; }
    if (busy) return;
    busy = true;
    const msg = N.$('#shMsg');
    const acc = account();
    const directEl = N.$('#shDirect');
    const direct = !!(directEl && directEl.checked);
    (async () => {
      let ok = 0; const errs = [];
      for (const line of lines) {
        if (msg) msg.textContent = (direct ? '直接生成：' : '转存中：') + line.slice(0, 48) + '…';
        try {
          const d = await window.API.addShare(line, acc, direct);
          ok++;
          D.logs.add('info', (direct ? '分享已直接生成 strm：<b>' : '分享转存完成：<b>') +
            esc((d.share && d.share.name) || d.share.share_code) + '</b>');
        } catch (e) { errs.push(e.message); }
      }
      if (msg) {
        msg.textContent = '完成 ' + ok + ' 条' + (errs.length ? '，失败 ' + errs.length + ' 条' : '');
      }
      if (ok === lines.length) {
        const input = N.$('#shUrl');
        if (input) input.value = '';
      }
      busy = false;
      loadData(acc);
    })();
  }

  function setDir(root) {
    const input = N.$('#shDirInput');
    const cid = (input && input.value || '').trim();
    if (!cid) { N.toast('请填 115 目录 id 或目录链接', 'err'); return; }
    window.API.shareSettings(account(), cid).then((d) => {
      N.toast('接收目录已设为「' + (d.dir_path || d.dir_name || d.settings.dir_name) + '」', 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function autoDir() {
    window.API.autoShareDir(account()).then((d) => {
      N.toast('接收目录：' + d.dir_name + ' (id=' + d.dir_cid + ')', 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  /** 从 115 网盘目录树里点选接收目录 */
  function pickRemote() {
    window.Shell.pickRemoteDir(account()).then((picked) => {
      if (!picked || !picked.cid) return;
      window.API.shareSettings(account(), picked.cid, '', picked.path).then((d) => {
        const name = (d && (d.dir_path || d.dir_name)) ||
          (d && d.settings && (d.settings.dir_path || d.settings.dir_name)) ||
          picked.path || picked.cid;
        N.toast('接收目录已设为「' + name + '」', 'ok');
        loadData(account());
      }).catch((err) => N.toast(err.message, 'err'));
    }).catch((err) => N.toast(err.message, 'err'));
  }

  /** 从映射目录里选本地 strm 输出目录（存相对 STRM_ROOT 的子路径） */
  function pickLocal(root) {
    const input = root.querySelector('#shStrmDir');
    window.Shell.pickLocalDir('', { relative: true }).then((rel) => {
      if (rel === null || rel === undefined) return;
      if (input) input.value = rel;
      N.toast('已选目录：' + (rel || '（映射目录根）'), 'ok');
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function saveLocal(root) {
    const input = root.querySelector('#shStrmDir');
    const value = (input && input.value || '').trim();
    if (!value) { N.toast('请先选一个目录', 'err'); return; }
    window.API.shareSettings(account(), '', value).then(() => {
      N.toast('本地输出目录已保存：' + value, 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  window.ToolShare = { page };
})();
