/* ==========================================================================
   tool-115.js · 115 助手
   --------------------------------------------------------------------------
   需求 3：115 助手不设置主账号，只负责记录「添加的账号」；
   每个账号带一个「设置」按钮，用于修改账号名（显示名）。
   其他工具（分享转存 / ed2k / 秒传 / 115 STRM）的账号都从这里读取。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc, fmtSize = N.fmtSize;

  let tab = 'form';           // form | qr

  function accountRow(acc) {
    return `
      <div class="row-item" data-acc="${esc(acc.name)}">
        <span class="stat-ico v4" style="width:40px;height:40px;border-radius:12px">${icon('user', 18)}</span>
        <div class="row-main">
          <div class="row-title">
            ${esc(acc.label)}
            <span class="tag ${acc.ok ? 'ok' : 'err'}">${acc.ok ? '正常' : '异常'}</span>
            ${acc.copy_enabled ? `<span class="tag brand">同播复制${acc.copy_count ? ' ' + acc.copy_count : ''}</span>` : ''}
          </div>
          <div class="row-desc">
            115 用户 ID：<b class="mono">${esc(acc.uid || '未获取到')}</b>
            ${acc.files ? ' · ' + acc.files + ' 个文件' : ''}
          </div>
          ${acc.strm_root ? `<div class="row-desc mono">strm：${esc(acc.strm_root)}</div>` : ''}
          ${acc.error ? `<div class="row-desc" style="color:var(--danger)">${esc(acc.error)}</div>` : ''}
        </div>
        <div class="row-actions">
          <button class="btn btn-sm" data-settings>${icon('settings', 12)} 设置</button>
          <button class="btn btn-sm btn-danger" data-del title="删除该账号">${icon('trash', 12)}</button>
        </div>
      </div>`;
  }

  function page() {
    // 账号列表：只显示添加过的账号（不再有单独的「主账号位」条目）
    const accounts = D.db.allAccounts();
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>115 助手</h1>
            <p>记录已添加的 115 账号。其他工具（分享转存、ed2k、秒传、115 STRM）的账号都从这里读取。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-primary btn-sm" data-add>${icon('plus', 14)} 添加账号</button>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>添加 115 账号</h2>
            <p class="card-sub">粘贴该账号的 cookies，可添加多个账号</p>
          </div>

          <div class="form-grid two">
            <div class="form-row">
              <label>账号名</label>
              <input class="input" id="accName" placeholder="可不填，默认叫「115」">
              <span class="form-hint">支持中文、字母、数字、中划线、下划线；1-32 位。留空自动命名</span>
            </div>
            <div class="form-row">
              <label>备注</label>
              <input class="input" id="accNote" placeholder="例如：4T 空间 / 备用号">
            </div>
          </div>
          <div class="form-row">
            <label>Cookies</label>
            <textarea class="input" id="accCookie" rows="2" spellcheck="false"
              placeholder="UID=...; CID=...; SEID=...（整串粘贴）"></textarea>
          </div>
          <div class="inline">
            <button class="btn btn-primary" data-save>${icon('check', 14)} 保存账号</button>
            <span class="form-hint">保存时会校验登录态，失败会提示具体原因</span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>账号配置</h2>
            <p class="card-sub">共 ${accounts.length} 个账号 · 各工具页（分享转存 / ed2k / 秒传 / 115 STRM）
              自己选用哪个账号</p>
          </div>
          <div class="row-list" id="accList">
            ${accounts.length ? accounts.map(accountRow).join('') :
              `<div class="empty"><span class="empty-ico">${icon('user', 20)}</span>
                还没有 115 账号。把浏览器里登录后的 cookies 整串粘贴到上面保存即可，
                账号名可以留空</div>`}
          </div>
        </div>`,
      mount(root) { bind(root); }
    };
  }

  /* ------------------------------------------------------------ 设置：账号名 + cookie */
  function accountSettings(acc) {
    const box = N.modal.open({
      title: '账号设置',
      body: `
        <div class="form-row">
          <label>账号名</label>
          <input class="input" id="eName" value="${esc(acc.label)}" placeholder="显示名称，可随时修改">
          <span class="form-hint">只改本服务里的显示名称，不影响 115 账号本身</span>
        </div>
        <div class="form-row">
          <label>115 用户 ID</label>
          <input class="input mono" value="${esc(acc.uid || '未获取到')}" disabled>
          <span class="form-hint">strm 末尾就是这串数字，播放时按它找回这个账号</span>
        </div>
        <div class="form-row">
          <label>内部标识</label>
          <input class="input" value="${esc(acc.name || '（主账号）')}" disabled>
          <span class="form-hint">仅用于区分本地索引目录，不影响 strm 地址</span>
        </div>
        ${acc.strm_root ? `<div class="kv">
          <dt>strm 目录</dt><dd class="mono">${esc(acc.strm_root)}</dd>
          <dt>已索引文件</dt><dd>${acc.files || 0} 个</dd>
        </div>` : ''}
        <div class="divider"></div>
        <div class="form-row">
          <label>当前 Cookies</label>
          <textarea class="input mono" id="eCookie" rows="4" spellcheck="false"
            placeholder="加载中…"></textarea>
          <span class="form-hint" id="eCookieHint">
            整串粘贴（UID=…; CID=…; SEID=…）。修改后会先校验登录态，通过才生效
          </span>
        </div>
        <div class="inline">
          <button class="btn btn-sm" data-test-cookie>${icon('refresh', 13)} 测试 cookie 可用性</button>
          <button class="btn btn-sm" data-load-cookie>${icon('refresh', 13)} 重新载入</button>
          <span class="form-hint" id="eCookieMsg"></span>
        </div>
        <div class="divider"></div>
        <div class="switch-row">
          <div class="sr-main">
            <div class="sr-title">同播复制</div>
            <div class="sr-desc">两台以上设备同时播放 <b>同一个文件</b> 时，在 115 里给后来者复制一份，
              各走各的直链、不抢带宽。对这个账号的所有播放生效（strm / ed2k 都算）</div>
          </div>
          <label class="switch"><input type="checkbox" id="eCopyEnabled">
            <span class="track"></span></label>
        </div>
        <div class="form-grid two" id="eCopyOpts">
          <div class="form-row">
            <label>副本保留时长（分钟）</label>
            <input class="input" id="eCopyTtl" type="number" min="5" step="5" value="30">
            <span class="form-hint">最后一次播放之后多久删掉副本</span>
          </div>
          <div class="form-row">
            <label>每个文件最多副本数</label>
            <input class="input" id="eCopyMax" type="number" min="1" max="10" value="3">
            <span class="form-hint">超过上限就沿用主文件，不再复制</span>
          </div>
        </div>
        <div class="inline">
          <span class="form-hint" id="eCopyStat">加载中…</span>
          <button class="btn btn-sm" data-clean-copies>${icon('broom', 13)} 清理过期副本</button>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存</button>',
      onOk() {
        const name = box.querySelector('#eName').value.trim();
        if (!name) { N.toast('账号名不能为空', 'err'); return false; }
        const cookie = box.querySelector('#eCookie').value.trim();
        const changed = cookie && cookie !== (acc._cookies || '');
        const syncCopies = () => window.API.saveAccountCopies(acc.name, {
          enabled: box.querySelector('#eCopyEnabled').checked ? 1 : 0,
          ttl_minutes: Math.max(5, Number(box.querySelector('#eCopyTtl').value) || 30),
          max_copies: Math.min(10, Math.max(1, Number(box.querySelector('#eCopyMax').value) || 3))
        });
        const save = () => {
          // 同播复制是账号级设置，跟着账号一起存
          syncCopies().catch((err) => N.toast('同播复制保存失败：' + err.message, 'err'));
          window.API.renameAccount(acc.name, name).then(() => {
            D.logs.add('info', '账号改名：<b>' + esc(acc.label) + '</b> → <b>' + esc(name) + '</b>');
            N.toast('已保存', 'ok');
            N.modal.close();
            window.App.reload();
          }).catch((err) => N.toast(err.message, 'err'));
        };
        if (!changed) { save(); return false; }
        // cookie 改了：先写入并校验，成功后再保存名字
        window.API.setAccountCookie(acc.name, cookie).then(() => {
          D.logs.add('info', '账号「<b>' + esc(acc.label) + '</b>」的 cookies 已更新');
          save();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      }
    });
    // 载入当前 cookie
    const ta = box.querySelector('#eCookie');
    const msg = box.querySelector('#eCookieMsg');
    const load = () => {
      ta.value = '加载中…';
      window.API.accountCookie(acc.name).then((d) => {
        ta.value = d.cookies || '';
        acc._cookies = d.cookies || '';
        ta.readOnly = false;
      }).catch((err) => {
        ta.value = '';
        ta.readOnly = false;
        if (msg) msg.textContent = '读取失败：' + err.message;
      });
    };
    load();
    box.querySelector('[data-load-cookie]').onclick = load;

    /* ---------------------------------------------------- 同播复制（账号级） */
    const copyOn = box.querySelector('#eCopyEnabled');
    const copyOpts = box.querySelector('#eCopyOpts');
    const copyStat = box.querySelector('#eCopyStat');
    const syncCopyOpts = () => { copyOpts.style.display = copyOn.checked ? '' : 'none'; };
    const statText = (s) => (s.enabled ? '已开启' : '已关闭')
      + ' · 当前 ' + (s.copies || 0) + ' 份副本'
      + (s.size ? '（' + fmtSize(s.size) + '）' : '');
    window.API.accountCopies(acc.name).then((d) => {
      const s = d.settings || {};
      copyOn.checked = s.enabled !== false;
      box.querySelector('#eCopyTtl').value = Math.round(s.ttl_minutes || 30);
      box.querySelector('#eCopyMax').value = s.max || 3;
      if (copyStat) copyStat.textContent = statText(s);
      syncCopyOpts();
    }).catch((err) => {
      if (copyStat) copyStat.textContent = '读取失败：' + err.message;
    });
    copyOn.addEventListener('change', syncCopyOpts);
    box.querySelector('[data-clean-copies]').onclick = () => {
      if (copyStat) copyStat.textContent = '清理中…';
      window.API.cleanCopies(acc.name, false).then((d) => {
        D.logs.add('info', '清理同播副本：删除 ' + (d.removed || 0) + ' 份');
        N.toast('已清理 ' + (d.removed || 0) + ' 份副本', 'ok');
        if (copyStat) copyStat.textContent = statText(d.settings || {});
      }).catch((err) => {
        if (copyStat) copyStat.textContent = '清理失败：' + err.message;
        N.toast(err.message, 'err');
      });
    };
    box.querySelector('[data-test-cookie]').onclick = () => {
      const val = ta.value.trim();
      if (!val) { N.toast('没有 cookies 可测试', 'err'); return; }
      if (msg) msg.textContent = '测试中…';
      // 测的是输入框里的内容 —— 可以先改再测，不用先保存
      window.API.testAccountCookie(acc.name, val).then((d) => {
        if (msg) msg.textContent = d.detail || (d.ok ? '可用' : '不可用');
        N.toast(d.ok ? 'cookies 可用' : ('cookies 不可用：' + (d.detail || '')), d.ok ? 'ok' : 'err');
      }).catch((err) => {
        if (msg) msg.textContent = '测试失败：' + err.message;
      });
    };
  }

  /* ------------------------------------------------------------ 保存账号 */
  function saveAccount(root) {
    const name = root.querySelector('#accName').value.trim();
    const cookies = root.querySelector('#accCookie').value.trim();
    if (!cookies) { N.toast('请粘贴 cookies', 'err'); return; }
    N.toast('正在校验并添加…');
    window.API.addAccount(name, cookies).then((d) => {
      root.querySelector('#accCookie').value = '';
      root.querySelector('#accName').value = '';
      D.logs.add('info', '新增 115 账号「<b>' + esc(d.name) + '</b>」');
      N.toast('账号「' + d.name + '」已添加', 'ok');
      window.App.reload();
    }).catch((err) => {
      N.toast(err.message || '添加失败', 'err');
      D.logs.add('err', '添加账号「' + esc(name || '115') + '」失败：' + esc(err.message || ''));
    });
  }

  /* ------------------------------------------------------------ 事件 */
  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-save]')) { saveAccount(root); return; }
      if (e.target.closest('[data-add]')) {
        // 首次添加只差 cookies：直接把光标放在那里，账号名可留空
        root.querySelector('#accCookie').focus();
        return;
      }
      const row = e.target.closest('[data-acc]');
      if (!row) return;
      const name = row.getAttribute('data-acc');
      const acc = D.db.account(name);
      if (!acc) return;

      if (e.target.closest('[data-settings]')) { accountSettings(acc); return; }
      if (e.target.closest('[data-del]')) {
        N.confirm('删除账号',
          '确定删除「' + esc(acc.label) + '」？' +
          '本地 strm 与索引会一并删除（115 网盘文件不受影响）。',
          { danger: true, okText: '删除' }).then((ok) => {
            if (!ok) return;
            window.API.removeAccount(name).then(() => {
              D.logs.add('warn', '删除 115 账号「' + esc(acc.label) + '」');
              N.toast('已删除', 'ok');
              window.App.reload();
            }).catch((err) => N.toast(err.message, 'err'));
          });
      }
    });
  }

  window.Tool115 = { page };
})();
