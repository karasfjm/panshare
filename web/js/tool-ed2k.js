/* ==========================================================================
   tool-ed2k.js · ed2k STRM
   --------------------------------------------------------------------------
   需求 5：任务列表与配置选项改为圆角白底黑字，选中的变为圆角蓝底白字
   （参考提交按钮）；配置里添加一个账号选择按钮，账号从 115 助手获取，
   保存后选择账号作为默认账号。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  // idle = 懒转存：strm 已生成，还没提交任何离线任务（播放时才会转存）
  const JOB_ST_TEXT = {
    idle: '待播放转存', ready: '就绪', pending: '转存中', failed: '失败', cleared: '已清理'
  };
  const JOB_ST_TAG = { idle: '', ready: 'ok', pending: 'warn', failed: 'err', cleared: '' };

  // 转存任务的状态筛选：成功 = 就绪；未完成 = 待播放转存 / 转存中 / 已清理；失败 = 失败
  const FILTERS = [
    { id: 'all', label: '全部', match: () => true },
    { id: 'ready', label: '成功', match: (it) => it.status === 'ready' },
    { id: 'pending', label: '未完成', match: (it) => it.status === 'pending' || it.status === 'cleared' || it.status === 'idle' },
    { id: 'failed', label: '失败', match: (it) => it.status === 'failed' },
  ];

  let items = [];
  let settings = {};
  let filter = 'all';

  function account() {
    return D.db.defaultAccount('ed2k');
  }

  function page() {
    const acc = account();
    const accRow = D.db.account(acc);

    return {
      html: `
          <div class="page-head">
            <div>
              <h1>ed2k STRM</h1>
              <p>把 ed2k 链接接入 115 和 STRM 工作流：添加时<b>直接生成 strm</b>，
                默认到播放时才转存到 115（也可以在下面改成添加时就转存）。</p>
            </div>
            <div class="head-actions">
              <button class="btn btn-sm" data-refresh>${icon('refresh', 14)} 刷新状态</button>
            </div>
          </div>

        <div class="card">
          <div class="card-head">
            <h2>配置</h2>
            <p class="card-sub">ed2k 转存使用哪个账号执行（来自 115 助手）</p>
          </div>
          <div class="pill-tabs" id="edAccountPills">
            ${window.Shell.accountPills(acc)}
          </div>
          <p class="form-hint">保存后作为 ed2k 任务的默认账号</p>

          <div class="stat-strip">
            <div class="ss"><div class="ss-label">使用账号</div>
              <div class="ss-val">${esc(accRow ? accRow.label : '主账号')}</div></div>
            <div class="ss"><div class="ss-label">转存目录</div>
              <div class="ss-val mono" id="edStatDir">${settings.dir_cid
                ? esc(settings.dir_path || settings.dir_name || ('id=' + settings.dir_cid))
                : '自动创建'}</div></div>
            <div class="ss"><div class="ss-label">保留时长</div>
              <div class="ss-val" id="edStatTtl">${settings.ttl_hours == null
                ? '—' : settings.ttl_hours + ' 小时'}</div></div>
            <div class="ss"><div class="ss-label">检查间隔</div>
              <div class="ss-val" id="edStatInterval">${Math.round((settings.clean_interval || 600) / 60)} 分钟</div></div>
            <div class="ss"><div class="ss-label">同播副本</div>
              <div class="ss-val" id="edStatCopies">${settings.copies || 0} 份</div></div>
          </div>

          <div class="form-grid two mt12">
            <div class="form-row span2">
              <label>转存目录（115 网盘）</label>
              <div class="inline">
                <button class="btn btn-sm" data-pick-remote>${icon('folder', 13)} 浏览网盘目录</button>
                <button class="btn btn-sm" data-auto-dir>${icon('plus', 13)} 自动创建</button>
              </div>
              <div class="inline" style="margin-top:6px">
                <input class="input mono" id="edDirInput"
                  value="${esc(settings.dir_path || settings.dir_name || '')}"
                  placeholder="或直接填 115 目录 id / 目录链接">
                <button class="btn btn-sm" data-set-dir>${icon('check', 13)} 设为转存目录</button>
              </div>
            </div>
            <!-- 本地输出目录紧跟在转存目录下面（两列网格里用 span2 占满整行） -->
            <div class="form-row span2">
              <label>本地 STRM 输出目录</label>
              <div class="inline">
                <input class="input mono" id="edStrmDir" value="${esc(settings.strm_dir || 'ed2k')}">
                <button class="btn btn-sm" data-pick-local>${icon('folder', 13)} 选择</button>
                <button class="btn btn-sm" data-save-local>${icon('check', 13)} 保存</button>
              </div>
              <span class="form-hint">相对映射目录（STRM_ROOT）；只影响之后生成的 strm</span>
            </div>
            <div class="form-row">
              <label>保留时长（小时）</label>
              <input class="input" id="edTtl" type="number" min="0" step="0.5"
                value="${settings.ttl_hours == null ? '' : settings.ttl_hours}">
              <span class="form-hint">从最后一次播放算，0 = 不自动清理</span>
            </div>
            <div class="form-row">
              <label>检查间隔（分钟）</label>
              <input class="input" id="edInterval" type="number" min="1"
                value="${Math.round((settings.clean_interval || 600) / 60)}">
            </div>
          </div>

          <div class="switch-row">
            <div class="sr-main">
              <div class="sr-title">添加后立即转存</div>
              <div class="sr-desc">关闭（默认）= 添加时只写 strm，播放时才提交离线转存，
                不看的片子不占网盘空间；打开 = 添加时立刻转存到 115，首次播放不用等</div>
            </div>
            <label class="switch"><input type="checkbox" id="edAutoTransfer"
              ${settings.auto_transfer ? 'checked' : ''}><span class="track"></span></label>
          </div>

          <div class="switch-row">
            <div class="sr-main">
              <div class="sr-title">同播复制</div>
              <div class="sr-desc">已上移到账号设置（115 助手 → 账号 →「设置」），
                对这个账号的所有播放生效，不再只针对 ed2k</div>
            </div>
            <a class="btn btn-sm" href="#/tools/p115">去账号设置</a>
          </div>

          <div class="inline">
            <button class="btn btn-primary btn-sm" data-save-cfg>${icon('check', 13)} 保存时长/间隔</button>
            <span class="form-hint">转存目录建议专用，「清空目录」会连同其他文件一起删</span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>添加 ed2k 链接</h2>
            <p class="card-sub">每行一条，支持批量提交</p>
          </div>
          <div class="form-row">
            <textarea class="input" id="edUrl" rows="4" spellcheck="false"
              placeholder="ed2k://|file|文件名|大小|HASH|&#10;支持一次粘贴多条，每行一条"></textarea>
          </div>
          <div class="inline">
            <button class="btn btn-primary" data-add-job>${icon('download', 14)} 添加（可批量）</button>
            <button class="btn btn-sm btn-danger" data-clean-expired>${icon('broom', 13)} 清理过期</button>
            <button class="btn btn-sm btn-danger" data-clean-all>${icon('trash', 13)} 清空目录</button>
            <span class="form-hint" id="edMsg"></span>
          </div>
          <div class="inline" style="margin-top:8px">
            <button class="btn btn-sm" data-export>${icon('download', 13)} 导出备份</button>
            <button class="btn btn-sm" data-import>${icon('upload', 13)} 导入备份</button>
            <input type="file" id="edImportFile" accept=".json,application/json" hidden>
            <span class="form-hint">换设备时用：导出会带上全部任务和 115 文件 id，
              到新设备导入后 strm 会自动重建</span>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>转存任务</h2>
            <p class="card-sub">共 ${items.length} 个${filterCountText()}</p>
          </div>
          <div class="filter-bar" id="edFilterBar">
            ${FILTERS.map((f) => {
              const n = items.filter(f.match).length;
              return `<button class="filter-chip ${filter === f.id ? 'active' : ''}"
                data-filter="${esc(f.id)}">${esc(f.label)} <em>${n}</em></button>`;
            }).join('')}
          </div>
          <div class="row-list" id="jobList">
            ${renderJobs()}
          </div>
        </div>`,
      mount(root) {
        bind(root);
        loadData(acc);
      }
    };
  }

  /** 当前筛选的计数说明（列表为空时告诉用户是筛选后没有，还是真的没有任务） */
  function filterCountText() {
    const f = FILTERS.find((x) => x.id === filter);
    if (!f || f.id === 'all') return '';
    const n = items.filter(f.match).length;
    return ' · ' + f.label + ' ' + n + ' 个';
  }

  function renderJobs() {
    const f = FILTERS.find((x) => x.id === filter) || FILTERS[0];
    const shown = items.filter(f.match);
    if (!shown.length) {
      if (!items.length) {
        return `<div class="empty"><span class="empty-ico">${icon('zap', 20)}</span>
          还没有转存任务，在上面粘贴 ed2k 链接添加</div>`;
      }
      return `<div class="empty"><span class="empty-ico">${icon('list', 20)}</span>
        没有「${esc(f.label)}」状态的任务（当前共 ${items.length} 个）</div>`;
    }
    return shown.map((it) => `
      <div class="row-item" data-job="${esc(it.key)}">
        <span class="stat-ico v3" style="width:38px;height:38px;border-radius:11px">${icon('zap', 16)}</span>
        <div class="row-main">
          <div class="row-title">${esc(it.name || it.key)}
            <span class="tag ${JOB_ST_TAG[it.status] || ''}">${esc(JOB_ST_TEXT[it.status] || it.status)}</span>
          </div>
          <div class="row-desc">${N.fmtSize(it.size || 0)}
            ${it.last_play_at ? ' · 上次播放 ' + N.stamp(new Date(it.last_play_at * 1000)) : ' · 从未播放'}</div>
          ${it.message ? `<div class="row-desc">${esc(it.message)}</div>` : ''}
        </div>
        <div class="row-actions">
          <button class="btn btn-sm btn-danger" data-del-job>${icon('trash', 12)}</button>
        </div>
      </div>`).join('');
  }

  async function loadData(acc) {
    try {
      const d = await window.API.ed2k(acc);
      settings = d.settings || {};
      items = d.items || [];
      refreshJobList();
      // 设置也是异步回来的：页面首次渲染时还是空对象，得补一遍顶部数字与表单初值
      // （以前只刷任务列表，所以「保留时长」显示 undefined、输入框空着）
      applySettings(N.$('#view') || document);
    } catch (err) {
      const box = N.$('#jobList');
      if (box) {
        box.innerHTML = `<div class="empty"><span class="empty-ico">${icon('warn', 20)}</span>
          加载失败：${esc(err.message)}</div>`;
      }
    }
  }

  /** 把 settings 填进顶部数字和表单（不整页重绘，用户正在输入的框不动） */
  function applySettings(root) {
    if (!root || !root.isConnected) return;
    const s = settings || {};
    const dirText = s.dir_path || s.dir_name || (s.dir_cid ? 'id=' + s.dir_cid : '');
    const put = (sel, text) => {
      const el = root.querySelector(sel);
      if (el) el.textContent = text;
    };
    put('#edStatDir', s.dir_cid ? dirText : '自动创建');
    put('#edStatTtl', s.ttl_hours == null ? '—' : s.ttl_hours + ' 小时');
    put('#edStatInterval', Math.round((s.clean_interval || 600) / 60) + ' 分钟');
    put('#edStatCopies', (s.copies || 0) + ' 份');
    const fill = (sel, val) => {
      const el = root.querySelector(sel);
      if (!el || el === document.activeElement) return;
      el.value = val;
    };
    // 转存目录输入框显示当前选中的目录（选完目录后不再是空的）
    fill('#edDirInput', s.dir_cid ? dirText : '');
    fill('#edTtl', s.ttl_hours == null ? '' : s.ttl_hours);
    fill('#edInterval', Math.round((s.clean_interval || 600) / 60));
    fill('#edStrmDir', s.strm_dir || 'ed2k');
    const autoEl = root.querySelector('#edAutoTransfer');
    if (autoEl) autoEl.checked = !!s.auto_transfer;
  }

  /** 刷新任务列表 + 筛选栏计数（数据变化或切换筛选后调用） */
  function refreshJobList() {
    const box = N.$('#jobList');
    if (box) box.innerHTML = renderJobs();
    const bar = N.$('#edFilterBar');
    if (bar) {
      bar.innerHTML = FILTERS.map((f) => {
        const n = items.filter(f.match).length;
        return `<button class="filter-chip ${filter === f.id ? 'active' : ''}"
          data-filter="${esc(f.id)}">${esc(f.label)} <em>${n}</em></button>`;
      }).join('');
    }
    const card = bar ? bar.closest('.card') : null;
    const desc = card ? card.querySelector('.card-sub') : null;
    if (desc) desc.textContent = '共 ' + items.length + ' 个' + filterCountText();
  }

  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-refresh]')) { loadData(account()); N.toast('已刷新'); return; }
      if (e.target.closest('[data-save-cfg]')) { saveCfg(root); return; }
      if (e.target.closest('[data-add-job]')) { addJobs(root); return; }
      if (e.target.closest('[data-set-dir]')) { setDir(root); return; }
      if (e.target.closest('[data-auto-dir]')) { autoDir(); return; }
      if (e.target.closest('[data-pick-remote]')) { pickRemote(); return; }
      if (e.target.closest('[data-pick-local]')) { pickLocal(root); return; }
      if (e.target.closest('[data-save-local]')) { saveLocal(root); return; }
      if (e.target.closest('[data-clean-expired]')) { clean(false); return; }
      if (e.target.closest('[data-clean-all]')) { clean(true); return; }
      if (e.target.closest('[data-export]')) { exportBackup(); return; }
      if (e.target.closest('[data-import]')) { pickImportFile(root); return; }
      // 状态筛选：只重画列表，不重新拉数据（更快）
      const chip = e.target.closest('[data-filter]');
      if (chip) {
        filter = chip.getAttribute('data-filter') || 'all';
        refreshJobList();
        return;
      }
      const del = e.target.closest('[data-del-job]');
      if (del) {
        const row = e.target.closest('[data-job]');
        const key = row.getAttribute('data-job');
        N.confirm('删除转存任务', '会删掉 strm，115 上已转存的文件也会一起删除。',
          { danger: true, okText: '删除' }).then((ok) => {
            if (!ok) return;
            window.API.removeEd2k(key, account()).then(() => {
              N.toast('已删除', 'ok');
              loadData(account());
            }).catch((err) => N.toast(err.message, 'err'));
          });
      }
    });

    // 账号选择（圆角选项：白底黑字 / 选中蓝底白字）
    N.bindOnce(root, 'click', (e) => {
      const pick = e.target.closest('[data-acc-pick]');
      if (!pick) return;
      // 主账号的 name 是空字符串，属性里用 @main 占位，这里取真实名字
      const name = pick.getAttribute('data-acc-name') || '';
      root.querySelectorAll('[data-acc-pick]').forEach((b) => b.classList.remove('active'));
      pick.classList.add('active');
      const patch = Object.assign({}, D.db.settings(), { default_account_ed2k: name });
      window.API.saveSettings(patch).then(() => {
        D.logs.add('info', 'ed2k 默认账号：<b>' + esc(D.db.label(name)) + '</b>');
        N.toast('已保存，默认账号：' + D.db.label(name), 'ok');
        window.App.reload();
      }).catch((err) => N.toast(err.message, 'err'));
    });

    // 转存时机开关：改完立刻生效，不用再点「保存时长/间隔」
    N.bindOnce(root, 'change', (e) => {
      const box = e.target.closest('#edAutoTransfer');
      if (!box) return;
      const on = box.checked;
      window.API.ed2kSettings(account(), { auto_transfer: on }).then(() => {
        N.toast(on ? '已改为：添加时立即转存' : '已改为：播放时才转存', 'ok');
        D.logs.add('info', 'ed2k 转存时机：<b>' + (on ? '添加时立即转存' : '播放时再转存') + '</b>');
        loadData(account());
      }).catch((err) => {
        box.checked = !on;   // 保存失败就退回原状，别让界面骗人
        N.toast(err.message, 'err');
      });
    });
  }

  function saveCfg(root) {
    const ttl = parseFloat(root.querySelector('#edTtl').value);
    const minutes = parseFloat(root.querySelector('#edInterval').value);
    const payload = {};
    if (!isNaN(ttl)) payload.ttl_hours = Math.max(0, ttl);
    if (!isNaN(minutes)) payload.clean_interval = Math.max(1, minutes) * 60;
    window.API.ed2kSettings(account(), payload).then(() => {
      N.toast('设置已保存', 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  /** 导出全部任务 + 配置成 JSON 文件（换设备用） */
  async function exportBackup() {
    try {
      const data = await window.API.exportEd2k(account(), true);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const d = new Date();
      const p = (n) => String(n).padStart(2, '0');
      const name = 'panshare-ed2k-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
        + '-' + p(d.getHours()) + p(d.getMinutes()) + '.json';
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      N.toast('已导出 ' + ((data.items || []).length) + ' 个任务', 'ok');
      D.logs.add('info', 'ed2k 已导出备份：' + ((data.items || []).length) + ' 个任务');
    } catch (err) {
      N.toast(err.message, 'err');
    }
  }

  function pickImportFile(root) {
    const input = root.querySelector('#edImportFile');
    if (!input) return;
    input.value = '';
    input.onchange = () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => { doImport(String(reader.result || '')); };
      reader.onerror = () => N.toast('读取文件失败', 'err');
      reader.readAsText(file, 'utf-8');
    };
    input.click();
  }

  async function doImport(text) {
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      N.toast('不是合法的 JSON 文件', 'err');
      return;
    }
    const count = Array.isArray(data.items) ? data.items.length : 0;
    if (!count) {
      N.toast('文件里没有 ed2k 任务（请用本页导出的 JSON）', 'err');
      return;
    }
    const ok = await N.confirm('导入 ed2k 备份',
      '将导入 ' + count + ' 个任务。已存在的（同一 ed2k hash）会跳过，不会覆盖现有记录；'
      + '导入后缺失的 strm 会自动重建。',
      { okText: '导入' });
    if (!ok) return;
    try {
      const r = await window.API.importEd2k(data, 'merge', account());
      N.toast('导入完成：新增 ' + r.added + '，跳过 ' + r.skipped
        + (r.failed ? '，失败 ' + r.failed : '') + '，补 strm ' + (r.strm || 0), 'ok');
      D.logs.add('info', '导入 ed2k 备份：新增 ' + r.added + ' 条，跳过 ' + r.skipped + ' 条');
      loadData(account());
    } catch (err) {
      N.toast(err.message, 'err');
    }
  }

  function addJobs(root) {
    const raw = root.querySelector('#edUrl').value || '';
    const links = raw.split(/\r?\n/).map((s) => s.trim()).filter((s) => /^ed2k:\/\//i.test(s));
    if (!links.length) {
      N.toast('没有识别到 ed2k 链接（每行一条，以 ed2k:// 开头）', 'err');
      return;
    }
    const msg = root.querySelector('#edMsg');
    const total = links.length;
    const CHUNK = 8;
    (async () => {
      let ok = 0; let fail = 0; const errs = [];
      for (let i = 0; i < links.length; i += CHUNK) {
        const part = links.slice(i, i + CHUNK);
        if (msg) msg.textContent = '提交中… ' + Math.min(i + part.length, total) + '/' + total;
        try {
          const d = await window.API.addEd2k(part.join('\n'), account());
          ok += d.added || 0;
          fail += d.failed || 0;
        } catch (e) {
          fail += part.length;
          errs.push(e.message);
        }
      }
      if (msg) {
        msg.textContent = '已提交 ' + ok + ' 条' + (fail ? '，失败 ' + fail + ' 条' : '');
      }
      if (ok) {
        root.querySelector('#edUrl').value = '';
        D.logs.add('info', '批量添加 ed2k：成功 ' + ok + ' 条');
      }
      loadData(account());
    })();
  }

  function setDir(root) {
    const input = root.querySelector('#edDirInput');
    const cid = (input && input.value || '').trim();
    if (!cid) { N.toast('请填 115 目录 id 或目录链接', 'err'); return; }
    window.API.ed2kSettings(account(), { dir_cid: cid }).then((d) => {
      const s = (d && d.settings) || {};
      N.toast('转存目录已更新：' + (s.dir_path || s.dir_name || cid), 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function autoDir() {
    window.API.autoEd2kDir(account()).then((d) => {
      N.toast('转存目录：' + d.dir_name + ' (id=' + d.dir_cid + ')', 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  /** 从 115 网盘目录树里点选转存目录（不用再手抄目录 id） */
  function pickRemote() {
    window.Shell.pickRemoteDir(account()).then((picked) => {
      if (!picked || !picked.cid) return;
      window.API.ed2kSettings(account(), {
        dir_cid: picked.cid, dir_path: picked.path
      }).then((d) => {
        const s = (d && d.settings) || {};
        N.toast('转存目录已设为：' + (s.dir_path || picked.path || picked.cid), 'ok');
        loadData(account());
      }).catch((err) => N.toast(err.message, 'err'));
    }).catch((err) => N.toast(err.message, 'err'));
  }

  /** 从映射目录里选本地 strm 输出目录（保存相对 STRM_ROOT 的子路径） */
  function pickLocal(root) {
    const input = root.querySelector('#edStrmDir');
    window.Shell.pickLocalDir('', { relative: true }).then((rel) => {
      if (rel === null || rel === undefined) return;
      if (input) input.value = rel;
      N.toast('已选目录：' + (rel || '（映射目录根）'), 'ok');
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function saveLocal(root) {
    const input = root.querySelector('#edStrmDir');
    const value = (input && input.value || '').trim();
    if (!value) { N.toast('请先选一个目录', 'err'); return; }
    window.API.ed2kSettings(account(), { strm_dir: value }).then(() => {
      N.toast('本地输出目录已保存：' + value, 'ok');
      loadData(account());
    }).catch((err) => N.toast(err.message, 'err'));
  }

  function clean(force) {
    const tip = force
      ? '会清空转存目录里的所有文件（包括不是本服务放进去的）？'
      : '立即清理超过保留时长的转存文件？';
    N.confirm(force ? '清空目录' : '清理过期', tip, { danger: true, okText: force ? '清空' : '清理' })
      .then((ok) => {
        if (!ok) return;
        window.API.cleanEd2k(account(), force).then((d) => {
          N.toast(d.removed ? '已清理 ' + d.removed + ' 个文件' : '没有需要清理的文件', 'ok');
          D.logs.add('warn', 'ed2k ' + (force ? '目录已清空' : '清理过期') + '：' + (d.removed || 0) + ' 个文件');
          loadData(account());
        }).catch((err) => N.toast(err.message, 'err'));
      });
  }

  window.ToolEd2k = { page };
})();
