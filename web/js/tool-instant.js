/* ==========================================================================
   tool-instant.js · 115 秒传
   --------------------------------------------------------------------------
   需求 7：删除「秒传账号添加项」，账号直接从 115 助手获取，可以添加多个。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  let status = { accounts: [], users: [], records: 0, known_users: [] };

  /** Emby 用户 → 秒传账号 的映射列表 */
  function renderUserList() {
    const users = status.users || [];
    if (!users.length) {
      return `<div class="empty"><span class="empty-ico">${icon('user', 20)}</span>` +
        `还没有映射，播放时按默认账号秒传</div>`;
    }
    return users.map((u) => `
      <div class="row-item" data-inuser="${esc(u.emby_user_id)}">
        <span class="stat-ico v1" style="width:36px;height:36px;border-radius:11px">${icon('user', 16)}</span>
        <div class="row-main">
          <div class="row-title">${esc(u.emby_user_name || 'Emby 用户')}
            <span class="tag mono">${esc(u.emby_user_id)}</span></div>
          <div class="row-desc">播放时秒传到：${u.account
            ? '<b>' + esc(D.db.label(u.account)) + '</b>' : '未映射'}</div>
        </div>
        <div class="row-actions">
          <button class="btn btn-sm btn-danger" data-del-user>${icon('trash', 12)}</button>
        </div>
      </div>`).join('');
  }

  /** 已知 Emby 用户（下拉选项） */
  function renderKnownUsers() {
    return (status.known_users || []).map((u) =>
      `<option value="${esc(u.id)}">${esc(u.name || u.id)}${u.mapped ? '（已映射）' : ''}</option>`
    ).join('');
  }

  /** 校验缓存的占用：一句话说明缓存了多少内容、上限是多少。
   *  这一层是「第二个人播同一部片子时不用再读源盘」的底气，
   *  页面上看得见，用户才知道它在替自己省什么。 */
  function digestText() {
    const d = status.digest || {};
    const max = status.digest_max_mb || 0;
    if (!d.blocks) return max ? '空（上限 ' + max + ' MB）' : '空';
    const mb = (d.bytes || 0) / (1 << 20);
    const size = mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(1) + ' MB';
    const tail = max ? ' / ' + max + ' MB' : '';
    return d.blocks + ' 块 / ' + size + tail;
  }

  /** 状态回来后**局部**更新这几处 —— 不整页重绘。
   *  整页重绘会再调 page() → mount() → 加载 → 重绘，成死循环（页面卡死）。 */
  function applyStatus(root) {
    // 暂存目录输入框 + 顶部那格：跟着「预缓存与暂存」里选的账号走
    syncStage(root);
    const list = root.querySelector('#inUserList');
    if (list) list.innerHTML = renderUserList();
    const rec = root.querySelector('#inRecCount');
    if (rec) rec.textContent = (status.records || 0) + ' 条';
    const dig = root.querySelector('#inDigestCount');
    if (dig) dig.textContent = digestText();
    const sel = root.querySelector('#inUidSel');
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">选择 Emby 用户…</option>' + renderKnownUsers();
      sel.value = cur;
    }
  }

  /** 某个账号自己设过的暂存目录：路径优先，其次目录 id，没设过就是空串 */
  function stageOf(acc) {
    const st = (status.staging || {})[acc] || {};
    return st.path || (st.cid ? String(st.cid) : '');
  }

  /** 顶部那格「暂存目录」该显示什么：设过就显示真实目录，没设过才显示默认名。
   *  之前这里写死取 status.instant_dir（配置里的默认目录名），
   *  用户改完暂存目录后顶部还显示 p115-strm-instant，看着像没生效。 */
  function stageLabel(acc) {
    return stageOf(acc) || status.instant_dir || 'p115-strm-instant';
  }

  /** 暂存目录相关的几处一起刷新：输入框 + 顶部那格。
   *  账号下拉换人、保存成功、恢复默认之后都走这里，不整页重绘。 */
  function syncStage(root) {
    const warm = root.querySelector('#inWarmAcc');
    const acc = warm ? warm.value : '';
    const pathInput = root.querySelector('#inStagePath');
    if (pathInput) {
      pathInput.value = stageOf(acc);
      pathInput.dataset.account = acc;
    }
    const stat = root.querySelector('#inStageStat');
    if (stat) {
      const own = stageOf(acc);
      stat.textContent = stageLabel(acc);
      stat.title = own
        ? (D.db.label(acc) || '') + ' 自定义暂存目录：' + own
        : (D.db.label(acc) || '') + ' 用默认目录（' +
          (status.instant_dir || 'p115-strm-instant') + '），可在下面改成别的目录';
    }
  }

  function page() {
    const accounts = D.db.selectable();
    const selected = D.db.settings().instant_accounts || [];
    const proxy = D.db.proxy() || {};
    const firstAcc = accounts.length ? accounts[0].name : '';

    return {
      html: `
        <div class="page-head">
          <div>
            <h1>115 秒传</h1>
            <p>第二个 115 账号也能用主号的资源播放：播放时把文件秒传到该账号
              （115 按 sha1 识别文件，不需要上传数据），再用那个账号的直链播放。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-refresh>${icon('refresh', 14)} 刷新</button>
            <button class="btn btn-primary btn-sm" data-save>${icon('check', 14)} 保存</button>
          </div>
        </div>

        <div class="stat-strip">
          <div class="ss"><div class="ss-label">秒传账号</div>
            <div class="ss-val" id="inAccCount">${selected.length} 个</div></div>
          <div class="ss"><div class="ss-label">暂存目录</div>
            <div class="ss-val mono" id="inStageStat">${esc(stageLabel(firstAcc))}</div></div>
          <div class="ss"><div class="ss-label">已秒传记录</div>
            <div class="ss-val" id="inRecCount">${status.records || 0} 条</div></div>
          <div class="ss"><div class="ss-label">校验缓存</div>
            <div class="ss-val" id="inDigestCount">${digestText()}</div></div>
          <div class="ss"><div class="ss-label">Emby 上游</div>
            <div class="ss-val mono">${esc(proxy.upstream || '—')}</div></div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>秒传账号</h2>
            <p class="card-sub">直接使用 115 助手中的账号，可多选，不需要单独添加</p>
          </div>
          <div class="pill-tabs" id="inAccountPills">
            ${window.Shell.accountPills(selected, { multi: true })}
          </div>
          <span class="form-hint" id="inAccHint">
            勾选后点右上角「保存」；保存后只有勾选的账号能用于秒传（不勾＝全部可用）
          </span>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>Emby 用户 → 秒传账号</h2>
            <p class="card-sub">Emby 用户 ID 在「设置 → 用户」里能看到；
              客户端登录过一次后这里就能直接选</p>
          </div>
          <div class="row-list" id="inUserList">
            ${renderUserList()}
          </div>
          <div class="inline mt12">
            <select class="select" id="inUidSel" style="max-width:220px">
              <option value="">选择 Emby 用户…</option>
              ${renderKnownUsers()}
            </select>
            <input class="input" id="inUid" placeholder="Emby 用户 ID" style="max-width:200px">
            <select class="select" id="inAccSel" style="max-width:180px">
              <option value="">未映射（不指定账号）</option>
              ${accounts.map((a) => `<option value="${esc(a.name)}">${esc(a.label)}</option>`).join('')}
            </select>
            <input class="input" id="inUserName" placeholder="用户名（可选）" style="max-width:180px">
            <button class="btn btn-sm" data-add-user>${icon('plus', 13)} 保存映射</button>
          </div>
          <div class="form-hint">
            下拉里的用户直接来自 Emby（<code>/Users/Public</code>），
            <b>不需要谁先登录一次</b>；Emby 地址不可达时会退回「登录过的用户」。
            也可以手填 Emby 用户 ID（Emby「设置 → 用户」里能看到）。
            <br>提示：播放请求不一定带用户 ID，程序靠登录令牌认人 ——
            新用户第一次播放前登录一次 Emby，映射才会立刻命中。
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>校验缓存</h2>
            <p class="card-sub">115 二次校验要的那段字节会被记下来 ——
              同一份内容被校验过一次之后，换个用户播就不用再读源盘</p>
          </div>
          <div class="inline">
            <span class="form-hint" id="inDigestText">${digestText()}</span>
            <button class="btn btn-sm" data-sweep-digest>${icon('refresh', 13)} 立即清理</button>
            <button class="btn btn-sm btn-danger" data-clear-digest>${icon('trash', 13)} 清空校验缓存</button>
            <span class="form-hint">
              清掉不影响已经能播的文件，只是下次播放要多读一次源盘。
              缓存按内容算大小，服务会定期清理超出的部分。
            </span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>预缓存与暂存</h2>
            <p class="card-sub">预缓存把索引里最近的文件提前秒传过去，配合反代播放零等待；
              清空暂存目录后下次播放会自动重新秒传</p>
          </div>
          <div class="inline">
            <select class="select" id="inWarmAcc" style="max-width:200px">
              ${accounts.map((a) => `<option value="${esc(a.name)}">${esc(a.label)}</option>`).join('')}
            </select>
            <button class="btn btn-sm" data-prewarm>${icon('zap', 13)} 预缓存最近 50 个文件</button>
            <button class="btn btn-sm btn-danger" data-clean-stage>${icon('trash', 13)} 清空暂存目录</button>
            <span class="form-hint" id="inMsg"></span>
          </div>
          <div class="divider"></div>
          <div class="form-row">
            <label>暂存目录</label>
            <div class="inline">
              <input class="input mono" id="inStagePath" style="max-width:260px"
                placeholder="网盘里的目录路径，留空＝默认目录">
              <button class="btn btn-sm" id="inStagePick-toggle">${icon('folder', 13)} 选择目录</button>
              <button class="btn btn-sm btn-primary" data-save-stage>${icon('check', 13)} 保存</button>
              <button class="btn btn-sm" data-reset-stage>${icon('refresh', 13)} 恢复默认</button>
            </div>
            <span class="form-hint">
              秒传的文件先落在目标账号的这个目录里。默认是网盘根下的
              <span class="mono">${esc(status.instant_dir || 'p115-strm-instant')}</span>；
              可以改成任意目录（也可以直接填目录 id）。
            </span>
            <div class="dir-pick" id="inStagePick" hidden></div>
          </div>
        </div>`,
      mount(root) {
        bind(root);
        // 状态是异步拉的：拉回来**局部更新**这几处（不整页重绘 ——
        // 整页重绘会再调 page() → mount() → 加载 → 重绘，死循环把页面卡死）。
        loadStatus().then(() => {
          if (root.isConnected) applyStatus(root);
        }).catch(() => { /* 状态拉不到就保持原样 */ });
      }
    };
  }

  async function loadStatus(refresh) {
    try {
      status = await window.API.instant(D.db.defaultAccount('instant'), refresh);
    } catch (err) {
      const msg = N.$('#inMsg');
      if (msg) msg.textContent = '加载失败：' + err.message;
    }
  }

  function bind(root) {
    // 换账号 → 暂存目录输入框和顶部那格跟着换（每个账号的暂存目录是分开记的）
    const warmSel = root.querySelector('#inWarmAcc');
    if (warmSel) warmSel.onchange = () => syncStage(root);
    // 选了已知用户 → 把 ID 填进输入框并显示用户名（省得用户去别处找 ID）
    const uidSel = root.querySelector('#inUidSel');
    if (uidSel) {
      uidSel.onchange = () => {
        if (!uidSel.value) return;
        const idInput = root.querySelector('#inUid');
        if (idInput) idInput.value = uidSel.value;
        const nameInput = root.querySelector('#inUserName');
        const known = (status.known_users || []).find((u) => u.id === uidSel.value);
        if (nameInput && known && known.name && !nameInput.value) {
          nameInput.value = known.name;
        }
      };
    }
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-refresh]')) {
        // 带上 refresh=1：强制重新问一次 Emby 的用户列表
        loadStatus(true).then(() => { if (root.isConnected) applyStatus(root); });
        N.toast('已刷新（含 Emby 用户列表）');
        return;
      }
      if (e.target.closest('[data-save]')) { saveAccounts(root); return; }
      if (e.target.closest('[data-add-user]')) { addUser(root); return; }
      if (e.target.closest('[data-del-user]')) { delUser(e); return; }
      if (e.target.closest('[data-prewarm]')) { prewarm(root); return; }
      if (e.target.closest('[data-clean-stage]')) { cleanStage(root); return; }
      if (e.target.closest('[data-clear-digest]')) { clearDigest(root); return; }
      if (e.target.closest('[data-sweep-digest]')) { sweepDigest(root); return; }
      if (e.target.closest('[data-save-stage]')) { saveStage(root); return; }
      if (e.target.closest('[data-reset-stage]')) { resetStage(root); return; }
      if (e.target.closest('#inStagePick-toggle')) {
        const picker = root.querySelector('#inStagePick');
        picker.hidden = !picker.hidden;
        if (!picker.hidden) loadStageDirs(root);
        return;
      }

      // 勾选秒传账号：只切换选中状态，不重绘（重绘会把未保存的勾选冲掉）
      const multi = e.target.closest('[data-acc-multi]');
      if (multi) {
        multi.classList.toggle('active');
        // 实时更新「秒传账号 N 个」与提示
        const n = pickedAccounts(root).length;
        const stat = root.querySelector('#inAccCount');
        if (stat) stat.textContent = n + ' 个';
        const hint = root.querySelector('#inAccHint');
        if (hint) {
          const dirty = n !== ((window.Data.db.settings() || {}).instant_accounts || []).length;
          hint.textContent = dirty
            ? '有未保存的改动（勾选 ' + n + ' 个），点右上角「保存」生效'
            : '勾选后点右上角「保存」；保存后只有勾选的账号能用于秒传（不勾＝全部可用）';
        }
      }
    });
  }

  /** 收集当前勾选的账号名 */
  function pickedAccounts(root) {
    return Array.prototype.map.call(
      root.querySelectorAll('#inAccountPills [data-acc-multi].active'),
      (b) => b.getAttribute('data-acc-name') || '');
  }

  function saveAccounts(root) {
    // data-acc-name 存真实账号名；data-acc-multi 只是选中标记
    const ids = pickedAccounts(root);
    const patch = Object.assign({}, D.db.settings(), { instant_accounts: ids });
    window.API.saveSettings(patch).then(() => {
      D.logs.add('info', '秒传配置已保存，账号 ' + ids.length + ' 个');
      N.toast('秒传配置已保存（' + ids.length + ' 个账号）', 'ok');
      // 只更新本地状态，不整页重绘：重绘会把用户刚勾的其它账号冲掉
      window.Data.state.settings.instant_accounts = ids;
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function addUser(root) {
    // 下拉选了已知用户就用它，否则用手填的 ID
    const sel = root.querySelector('#inUidSel');
    const uid = (sel && sel.value ? sel.value : root.querySelector('#inUid').value).trim();
    const acc = root.querySelector('#inAccSel').value;
    const name = root.querySelector('#inUserName').value.trim();
    if (!uid) { N.toast('请选择或填写 Emby 用户', 'err'); return; }
    window.API.setInstantUser(uid, name, acc).then(() => {
      N.toast(acc ? '已映射到「' + D.db.label(acc) + '」' : '已清除映射', 'ok');
      D.logs.add('info', 'Emby 用户映射：' + esc(uid) + ' → ' + esc(acc || '（清除）'));
      root.querySelector('#inUid').value = '';
      root.querySelector('#inUserName').value = '';
      if (sel) sel.value = '';
      // 等状态拉回来再**局部更新**（不整页重绘：那会死循环把页面卡死）
      loadStatus().then(() => {
        if (root.isConnected) applyStatus(root);
      });
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function delUser(e) {
    const row = e.target.closest('[data-inuser]');
    const uid = row.getAttribute('data-inuser');
    const root = row.closest('#view') || document;
    N.confirm('移除映射', '移除「' + esc(uid) + '」的秒传账号映射？', { danger: true, okText: '移除' })
      .then((ok) => {
        if (!ok) return;
        window.API.removeInstantUser(uid).then(() => {
          N.toast('已移除', 'ok');
          // 等状态拉回来再局部更新
          loadStatus().then(() => {
            if (root.isConnected) applyStatus(root);
          });
        }).catch((err) => N.toast(err.message, 'err'));
      });
  }

  /** 读目标账号的 115 目录列表（给暂存目录选择器用） */
  function loadStageDirs(root) {
    const picker = root.querySelector('#inStagePick');
    const acc = root.querySelector('#inWarmAcc').value;
    picker.innerHTML = '<span class="form-hint">加载中…</span>';
    window.API.dirs(0, acc).then((d) => {
      const dirs = d.dirs || [];
      picker.innerHTML = dirs.length ? dirs.map((x) =>
        `<button class="dir-opt" data-pick="${esc(x.id)}" data-name="${esc(x.name)}">
          ${icon('folder', 12)} ${esc(x.name)}</button>`).join('') :
        '<span class="form-hint">没有可选目录</span>';
      picker.querySelectorAll('[data-pick]').forEach((b) => {
        b.onclick = () => {
          const pathInput = root.querySelector('#inStagePath');
          pathInput.value = '/' + b.getAttribute('data-name');
          picker.hidden = true;
        };
      });
    }).catch((err) => {
      picker.innerHTML = '<span class="form-hint">读取失败：' + esc(err.message) + '</span>';
    });
  }

  /** 保存暂存目录设置 */
  function saveStage(root) {
    const acc = root.querySelector('#inWarmAcc').value;
    const raw = root.querySelector('#inStagePath').value.trim();
    const msg = root.querySelector('#inMsg');
    // 纯数字当成目录 id，其余当路径
    const isCid = /^\d+$/.test(raw);
    const path = isCid ? '' : raw;
    const cid = isCid ? Number(raw) : 0;
    window.API.setInstantStaging(acc, path, cid).then(() => {
      N.toast('暂存目录已保存', 'ok');
      // 等状态回来再刷新，顶部那格才会显示刚存进去的真实目录
      loadStatus().then(() => {
        if (!root.isConnected) return;
        applyStatus(root);
        if (msg) msg.textContent = '暂存目录已保存：' + stageLabel(acc);
      });
    }).catch((err) => {
      if (msg) msg.textContent = err.message;
      N.toast(err.message, 'err');
    });
  }

  /** 恢复默认暂存目录 */
  function resetStage(root) {
    const acc = root.querySelector('#inWarmAcc').value;
    const msg = root.querySelector('#inMsg');
    window.API.setInstantStaging(acc, '', 0).then(() => {
      N.toast('已恢复默认', 'ok');
      loadStatus().then(() => {
        if (!root.isConnected) return;
        applyStatus(root);
        if (msg) msg.textContent = '已恢复默认暂存目录：' + stageLabel(acc);
      });
    }).catch((err) => {
      if (msg) msg.textContent = err.message;
      N.toast(err.message, 'err');
    });
  }

  function prewarm(root) {
    const acc = root.querySelector('#inWarmAcc').value;
    const msg = root.querySelector('#inMsg');
    if (msg) msg.textContent = '预缓存中…（在后台跑，日志可见进度）';
    window.API.prewarm(acc).then(() => {
      if (msg) msg.textContent = '预缓存已提交，完成后日志会输出统计';
      D.logs.add('info', '秒传预缓存已提交：' + esc(D.db.label(acc)));
    }).catch((err) => {
      if (msg) msg.textContent = err.message;
    });
  }

  function cleanStage(root) {
    const acc = root.querySelector('#inWarmAcc').value;
    N.confirm('清空暂存目录', '清空账号「' + esc(D.db.label(acc)) + '」暂存目录里的所有文件？' +
      '下次播放会自动重新秒传。', { danger: true, okText: '清空' }).then((ok) => {
        if (!ok) return;
        window.API.cleanInstant(acc).then((d) => {
          N.toast('已清理 ' + d.removed + ' 个文件', 'ok');
          loadStatus();
        }).catch((err) => N.toast(err.message, 'err'));
      });
  }

  /** 清空秒传校验缓存 */
  function clearDigest(root) {
    N.confirm('清空校验缓存', '清空 115 二次校验的字节缓存？' +
      '不影响已经能播的文件，只是下次播放要多读一次源盘。',
      { danger: true, okText: '清空' }).then((ok) => {
        if (!ok) return;
        window.API.clearInstantDigest().then((d) => {
          N.toast('已清空 ' + (d.blocks || 0) + ' 块', 'ok');
          loadStatus().then(() => {
            if (!root.isConnected) return;
            applyStatus(root);
            const txt = root.querySelector('#inDigestText');
            if (txt) txt.textContent = digestText();
          });
        }).catch((err) => N.toast(err.message, 'err'));
      });
  }

  /** 立刻把校验缓存压回预算内（丢最久没用的那些） */
  function sweepDigest(root) {
    window.API.sweepInstantDigest().then((d) => {
      if (d.dropped) {
        N.toast('清理了 ' + d.dropped + ' 块（释放 ' +
          (d.freed / (1 << 20)).toFixed(1) + ' MB）', 'ok');
      } else {
        N.toast('缓存没有超出上限，无需清理', 'ok');
      }
      loadStatus().then(() => {
        if (!root.isConnected) return;
        applyStatus(root);
        const txt = root.querySelector('#inDigestText');
        if (txt) txt.textContent = digestText();
      });
    }).catch((err) => N.toast(err.message, 'err'));
  }

  window.ToolInstant = { page };
})();
