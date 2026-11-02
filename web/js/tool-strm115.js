/* ==========================================================================
   tool-strm115.js · 115 STRM：按「账号 + 网盘路径」生成 strm
   --------------------------------------------------------------------------
   需求 10：工具下添加 115 STRM，内有「添加配置」选项；
   添加配置生成卡片，进入卡片设置需要生成 strm 的账号详情，
   账号从 115 助手获取，需要设置网盘路径。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  const SAMPLE_PICKCODE = 'ecjqi1hcb4hl21vx';
  const SAMPLE_NAME = '【示例】影片.2024.2160p.mkv';
  const SAMPLE_UID = '1987654321';

  // 扩展名默认值（与后端 DEFAULT_MEDIA_EXTS / DEFAULT_META_EXTS 保持一致）：
  // 老配置里存的是空字符串时，界面也显示默认值，避免看着像没设置
  const DEFAULT_MEDIA_EXTS = 'mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts';
  const DEFAULT_META_EXTS = 'nfo,jpg,jpeg,png,srt,ass,sub,idx';

  /** strm 内容：请求头 + pickcode + name（URL 编码）+ 115 用户 ID（标识账号）。 */
  function strmContent(base, pickcode, name, uid) {
    const b = (base || 'http://127.0.0.1:9527').replace(/\/+$/, '');
    return b + '/p115/play/' + (pickcode || 'pickcode') + '/' +
      encodeURIComponent(name || 'example.mkv') + '/' +
      (uid || SAMPLE_UID);
  }

  function splitList(raw) {
    return String(raw || '').split(/[,，;；\s]+/).filter(Boolean);
  }

  /** 定时巡检间隔（分钟）：配置里存秒，0 = 用默认 1 小时 */
  function scanMinutes(t) {
    const sec = Number((t && t.scan_interval) || 0) || 3600;
    return Math.max(1, Math.round(sec / 60));
  }

  /** 一条配置的「网盘路径 ↔ 本地目录」对：新字段 path_dirs 优先，老字段兜底。 */
  function configPairs(t) {
    let rows = [];
    if (t.path_dirs) {
      try {
        const arr = typeof t.path_dirs === 'string' ? JSON.parse(t.path_dirs) : t.path_dirs;
        if (Array.isArray(arr)) {
          rows = arr.filter((x) => x && x.cid).map((x) => ({
            cid: String(x.cid), path: x.path || '', dir: x.dir || ''
          }));
        }
      } catch (e) { rows = []; }
    }
    if (!rows.length) {
      const cids = splitList(t.remote_cids || t.remote_cid);
      const paths = splitList(t.remote_paths || t.remote_path);
      rows = cids.map((cid, i) => ({
        cid, path: paths[i] || '', dir: t.local_dir || ''
      }));
    }
    return rows;
  }

  function taskCard(t) {
    const statusTag = t.status === 'running' ? '<span class="tag brand">运行中</span>' :
      t.status === 'error' ? '<span class="tag err">失败</span>' : '';
    const pairs = configPairs(t);
    const srcText = pairs.length ? pairs.map((x) => x.path || x.cid).join('、') : '—';
    const dirText = pairs.length
      ? pairs.map((x) => x.dir || '/strm').join('、')
      : (t.local_dir || '/strm');
    return `
      <div class="task-card" data-task="${esc(t.id)}">
        <div class="tc-head">
          <div class="tc-name">${esc(t.name)}</div>
          <button class="icon-btn" data-edit title="设置">${icon('pen', 14)}</button>
        </div>
        <div class="tc-row"><span class="tc-ico v1">${icon('user', 12)}</span>
          <span class="tc-key">账号</span><span class="tc-val">${esc(D.db.label(t.account))}</span></div>
        <div class="tc-row"><span class="tc-ico v3">${icon('folder', 12)}</span>
          <span class="tc-key">源</span><span class="tc-val mono">${esc(srcText)}</span></div>
        <div class="tc-row"><span class="tc-ico v1">${icon('layers', 12)}</span>
          <span class="tc-key">路径</span><span class="tc-val">${pairs.length} 个网盘路径（各绑定一个目录）</span></div>
        <div class="tc-row"><span class="tc-ico v2">${icon('drive', 12)}</span>
          <span class="tc-key">目标</span><span class="tc-val mono">${esc(dirText)}</span></div>
        <div class="tc-tags">
          ${t.api_pull ? '<span class="tag">API 拉取</span>' : ''}
          ${t.redirect ? '<span class="tag">Redirect STRM</span>' : ''}
          ${t.event_watch ? '<span class="tag">事件监控</span>' : ''}
          ${t.auto_scan ? '<span class="tag brand">定时 ' + scanMinutes(t) + ' 分钟</span>' : ''}
          ${statusTag}
        </div>
        <div class="tc-foot">
          <span class="tc-meta">${t.files || 0} 个文件${t.last_run ? ' · ' + N.stamp(new Date(t.last_run * 1000)) : ''}</span>
          <div class="tc-acts">
            <button class="btn btn-sm" data-inc>${icon('play', 11)} 增量</button>
            <button class="btn btn-sm" data-full title="清索引重扫，strm 全部重写一遍">${icon('refresh', 11)} 全量</button>
            <button class="btn btn-sm" data-rebuild title="先删掉目录里的旧 strm，再从头生成">${icon('broom', 11)} 全新</button>
            <button class="btn btn-sm btn-danger" data-del>${icon('trash', 11)}</button>
          </div>
        </div>
      </div>`;
  }

  function page() {
    const tasks = D.db.state.strmConfigs || [];

    const stat = (ico, cls, label, num) => `
      <div class="strm-stat">
        <span class="ss-ico ${cls}">${icon(ico, 18)}</span>
        <div class="ss-main"><div class="ss-label">${label}</div><div class="ss-num">${num}</div></div>
      </div>`;

    const running = tasks.filter((t) => t.status === 'running').length;
    const waiting = tasks.filter((t) => t.status === 'waiting').length;
    const watch = tasks.filter((t) => t.event_watch).length;
    const timed = tasks.filter((t) => t.auto_scan).length;

    return {
      html: `
        <div class="page-head">
          <div>
            <h1>115 STRM</h1>
            <p>按「账号 + 网盘路径」生成 strm：每个网盘路径配一个本地目录
              （默认 <span class="mono">/影音/国漫</span> → <span class="mono">/strm/国漫</span>，
              可改成映射根下的任意目录，映射根在「服务设置」里改），生成前自动建好目录；
              strm 内容 = 请求头 + pickcode + name + 115 用户 ID。<br>
              <b>增量</b>只补新增；<b>全量</b>清索引重扫、strm 全部重新写一遍；
              <b>全新</b>先删掉这条配置生成过的旧 strm（不碰别人的）再从头生成。<br>
              <b>定时巡检</b>可以在配置里单独打开并设置间隔，到点自动跑一次增量。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-service>${icon('settings', 14)} 服务设置</button>
            <button class="btn btn-primary btn-sm" data-add>${icon('plus', 14)} 添加配置</button>
          </div>
        </div>

        <div class="strm-stats">
          ${stat('layers', 'v1', '总任务数', tasks.length)}
          ${stat('play', 'v2', '运行中', running)}
          ${stat('clock', 'v3', '等待中', waiting)}
          ${stat('scan', 'v4', '监控 / 定时', watch + ' / ' + timed)}
        </div>

        ${tasks.length ? `<div class="task-grid">${tasks.map(taskCard).join('')}</div>` : `
          <div class="card"><div class="empty">
            <span class="empty-ico">${icon('layers', 20)}</span>
            还没有 STRM 配置，点右上角「添加配置」开始
          </div></div>`}

        <div class="card">
          <div class="card-head">
            <h2>事件日志</h2>
            <p class="card-sub">生成 / 重写 / 补写 / 失败都记在这里，刷新页面也不会丢</p>
          </div>
          <div class="inline" style="margin-bottom:8px">
            <button class="btn btn-sm" data-elog-refresh>${icon('refresh', 12)} 刷新</button>
            <button class="btn btn-sm" data-elog-clear>${icon('broom', 12)} 清空</button>
            <span class="form-hint" id="elogHint"></span>
          </div>
          <div class="elog" id="elogList"><span class="form-hint">加载中…</span></div>
        </div>`,
      mount(root) {
        bind(root);
        loadEvents(root);
        // 刷新页面时还有任务在跑：接着盯状态，跑完自动变回「空闲」
        (D.db.state.strmConfigs || []).forEach((t) => {
          if (t.status === 'running') watchStatus(t.id, 0);
        });
      }
    };
  }

  /* ------------------------------------------------------------ 配置弹窗 */
  function taskForm(task, presetAccount) {
    const accounts = D.db.selectable();
    const t = task || {
      id: '', account: presetAccount || D.db.defaultAccount('strm') || (accounts[0] || {}).name || '',
      name: '', remote_cid: '', remote_cids: '', remote_path: '', remote_paths: '', local_dir: '',
      path_dirs: '',
      base_url: '', media_exts: '', meta_exts: '',
      download_meta: true, event_watch: true, api_pull: true, redirect: true,
      // 新配置的巡检间隔用「服务设置」里的默认值（秒）
      auto_scan: false, scan_interval: (D.db.settings() || {}).interval || 3600
    };
    // 网盘路径 ↔ 本地目录：一个路径一行
    const picked = configPairs(t);

    const box = N.modal.open({
      title: task ? '配置 · ' + task.name : '添加 STRM 配置',
      wide: true,
      body: `
        <div class="form-grid two">
          <div class="form-row">
            <label>账号（来自 115 助手）</label>
            <select class="select" id="sAccount">
              ${accounts.length ? accounts.map((a) =>
                `<option value="${esc(a.name)}" ${t.account === a.name ? 'selected' : ''}>${esc(a.label)}</option>`).join('') :
                '<option value="">（请先在 115 助手里添加账号）</option>'}
            </select>
          </div>
          <div class="form-row">
            <label>任务名称</label>
            <input class="input" id="sName" value="${esc(t.name)}" placeholder="例如：影音 / 电影 / 剧集">
          </div>
        </div>

        <div class="form-row">
          <label>网盘路径 → 本地 STRM 目录</label>
          <div class="pair-list" id="pairList"></div>
          <div class="inline" style="margin-top:8px">
            <button class="btn" id="pickRemote-toggle">${icon('folder', 14)} 选择网盘目录</button>
            <span class="form-hint">
              本地目录默认按 <b>映射根 + 网盘目录名</b> 预填（网盘 <span class="mono">/影音/国漫</span>
              → <span class="mono" id="sMapSample"></span>）；点行里的「选」可以改成
              <span class="mono">映射根</span> 下的任意目录，也可以直接改
            </span>
          </div>
          <div class="dir-pick" id="pickRemote" hidden></div>
        </div>

        <div class="form-row">
          <label>STRM 请求头</label>
          <input class="input" id="sBase" value="${esc(t.base_url)}" placeholder="http://192.168.1.10:9527">
          <span class="form-hint">strm 内容的基地址，客户端必须能访问到；留空则用全局设置</span>
        </div>

        <div class="form-row">
          <label>STRM 内容预览</label>
          <div class="code-block" id="sPreview">${esc(strmContent(t.base_url, SAMPLE_PICKCODE, SAMPLE_NAME))}</div>
          <span class="form-hint">规则：请求头 + pickcode + name（URL 编码）+ 115 用户 ID（标识该 strm 属于哪个账号）</span>
        </div>

        <div class="divider"></div>

        <div class="form-grid two">
          <div class="form-row">
            <label>媒体文件扩展名</label>
            <input class="input" id="sMediaExts"
              value="${esc(t.media_exts || DEFAULT_MEDIA_EXTS)}"
              placeholder="${esc(DEFAULT_MEDIA_EXTS)}">
            <span class="form-hint">命中这些后缀才生成 strm，英文逗号分隔</span>
          </div>
          <div class="form-row">
            <label>元数据文件扩展名</label>
            <input class="input" id="sMetaExts"
              value="${esc(t.meta_exts || DEFAULT_META_EXTS)}"
              placeholder="${esc(DEFAULT_META_EXTS)}">
            <span class="form-hint">nfo / 图片 / 字幕等，可下载到本地</span>
          </div>
        </div>

        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">下载元数据文件</div>
            <div class="sr-desc">元数据后缀的文件直接下载到本地 strm 所在目录</div></div>
          <label class="switch"><input type="checkbox" id="sMeta" ${t.download_meta ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">事件监控</div>
            <div class="sr-desc">监听 115 目录变化，新增文件实时生成 strm</div></div>
          <label class="switch"><input type="checkbox" id="sWatch" ${t.event_watch ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">定时巡检</div>
            <div class="sr-desc">按下面的间隔自动跑一次<b>增量</b>生成：只补新增 / 改名的文件，
              不会重写已有 strm（全量、全新仍然手动点）</div></div>
          <label class="switch"><input type="checkbox" id="sAutoScan" ${t.auto_scan ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="form-row" id="sIntervalRow" ${t.auto_scan ? '' : 'hidden'}>
          <label>巡检间隔（分钟）</label>
          <input class="input" id="sScanInterval" type="number" min="1" max="10080"
            value="${scanMinutes(t)}">
          <span class="form-hint">最小 1 分钟；同一时间只会跑一条配置，多条同时到点会排队执行</span>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存配置</button>',
      onOk() {
        const account = box.querySelector('#sAccount').value;
        const name = box.querySelector('#sName').value.trim();
        const baseUrl = box.querySelector('#sBase').value.trim();
        // 本地目录留空的按默认规则补（映射根 + 网盘目录名），其它必须以用户填的为准
        picked.forEach((x) => { if (!x.dir) x.dir = defaultDir(x.path); });
        if (!account) { N.toast('请先在 115 助手里添加账号', 'err'); return false; }
        if (!name) { N.toast('请填写任务名称', 'err'); return false; }
        if (!picked.length) { N.toast('请至少添加一个网盘路径', 'err'); return false; }
        const outside = picked.find((x) => !inRoot(x.dir));
        if (outside) {
          N.toast('本地目录必须在映射根 ' + mapRoot() + ' 内：' + outside.dir, 'err');
          return false;
        }
        if (baseUrl && !/^https?:\/\//i.test(baseUrl)) {
          N.toast('请求头需要以 http:// 或 https:// 开头', 'err');
          return false;
        }
        const first = picked[0];
        const payload = {
          id: t.id, account, name,
          path_dirs: picked.map((x) => ({ cid: x.cid, path: x.path, dir: x.dir })),
          remote_cids: picked.map((x) => x.cid).join(','),
          remote_paths: picked.map((x) => x.path).join(','),
          // 老字段也带上（第一个），方便旧版前端/接口读
          remote_cid: first.cid,
          remote_path: first.path,
          local_dir: first.dir, base_url: baseUrl,
          media_exts: box.querySelector('#sMediaExts').value.trim(),
          meta_exts: box.querySelector('#sMetaExts').value.trim(),
          download_meta: box.querySelector('#sMeta').checked,
          event_watch: box.querySelector('#sWatch').checked,
          auto_scan: box.querySelector('#sAutoScan').checked,
          scan_interval: Math.max(1, Number(box.querySelector('#sScanInterval').value) || 60) * 60,
          api_pull: t.api_pull !== false,
          redirect: t.redirect !== false
        };
        window.API.saveStrmConfig(payload).then(() => {
          D.logs.add('info', task
            ? 'STRM 配置已更新：<b>' + esc(name) + '</b> · ' + esc(payload.remote_paths)
            : '新增 STRM 配置：<b>' + esc(name) + '</b> · ' + esc(payload.remote_paths));
          N.toast('配置已保存', 'ok');
          N.modal.close();          // 保存成功后关掉弹窗，别让用户以为没生效
          window.App.reload();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      },
      mount(m) {
        const pv = m.querySelector('#sPreview');
        const baseInput = m.querySelector('#sBase');
        baseInput.addEventListener('input', () => {
          pv.textContent = strmContent(baseInput.value.trim(), SAMPLE_PICKCODE, SAMPLE_NAME);
        });
        renderPairs(m, picked);
        updateMapSample(m);
        m.querySelector('#pairList').addEventListener('click', (e) => {
          // 「选」：从映射目录里给这一行挑一个本地目录
          const pick = e.target.closest('[data-pickdir]');
          if (pick) {
            const i = Number(pick.getAttribute('data-pickdir'));
            if (!picked[i]) return;
            // 从映射根开始选：当前值可能是个还不存在的目录，从根走最稳
            window.Shell.pickLocalDir(mapRoot()).then((abs) => {
              if (!abs) return;
              picked[i].dir = abs;
              renderPairs(m, picked);
              N.toast('本地目录 → ' + abs, 'ok');
            }).catch((err) => N.toast(err.message, 'err'));
            return;
          }
          const drop = e.target.closest('[data-drop]');
          if (!drop) return;
          picked.splice(Number(drop.getAttribute('data-drop')), 1);
          renderPairs(m, picked);
        });
        // 手动改输入框也要落到 picked 上（别到保存时才读 DOM）
        m.querySelector('#pairList').addEventListener('input', (e) => {
          const inp = e.target.closest('[data-dir]');
          if (!inp) return;
          const i = Number(inp.getAttribute('data-dir'));
          if (picked[i]) picked[i].dir = inp.value.trim();
        });
        m.querySelector('#pickRemote-toggle').onclick = () => {
          const picker = m.querySelector('#pickRemote');
          picker.hidden = !picker.hidden;
          if (!picker.hidden) openPicker(m, 0, []);
        };
        m.querySelector('#sAccount').addEventListener('change', () => {
          const picker = m.querySelector('#pickRemote');
          if (!picker.hidden) openPicker(m, 0, []);
        });
        // 定时巡检开关：关掉时把间隔输入框藏起来，省得看着像「设置了但没生效」
        const autoBox = m.querySelector('#sAutoScan');
        const intervalRow = m.querySelector('#sIntervalRow');
        autoBox.addEventListener('change', () => {
          intervalRow.hidden = !autoBox.checked;
        });
      }
    });

    /** STRM 映射根目录（本地目录只能落在这个目录里） */
    function mapRoot() {
      const set = D.db.settings() || {};
      return (set.default_local || '/strm').replace(/\/+$/, '') || '/strm';
    }

    /** 映射示例（弹窗顶部的提示文案用） */
    function updateMapSample(m) {
      const el = m.querySelector('#sMapSample');
      if (el) el.textContent = defaultDir('/影音/国漫');
    }

    /** 默认本地目录：映射根 + 网盘目录名（末级）。只是预填，用户可以改 */
    function defaultDir(path) {
      const base = mapRoot();
      const leaf = String(path || '').split('/').filter(Boolean).pop() || '';
      return leaf ? base + '/' + leaf : base;
    }

    /** 本地目录是否在映射根内（/strm 本身也算） */
    function inRoot(dir) {
      const root = mapRoot();
      const p = String(dir || '').replace(/\/+$/, '');
      return p === root || p.startsWith(root + '/');
    }

    /** 已选「网盘路径 → 本地目录」列表：默认预填，可点「选」改成映射根下任意目录 */
    function renderPairs(m, rows) {
      const boxEl = m.querySelector('#pairList');
      if (!boxEl) return;
      boxEl.innerHTML = rows.length ? rows.map((r, i) => {
        const want = defaultDir(r.path);
        const shown = r.dir || want;
        return `
        <div class="pair-row">
          <div class="pair-src mono" title="cid ${esc(r.cid)}">${icon('folder', 12)} ${esc(r.path || r.cid)}</div>
          <span class="pair-arrow">→</span>
          <input class="input mono" data-dir="${i}" value="${esc(shown)}"
            placeholder="${esc(want)}" title="本地 STRM 目录：默认 ${esc(want)}，可改成映射根下的任意目录">
          <button type="button" class="btn btn-sm" data-pickdir="${i}"
            title="从映射目录里选">${icon('folder', 12)} 选</button>
          <button type="button" class="icon-btn" data-drop="${i}" aria-label="移除">×</button>
        </div>`;
      }).join('')
        : '<span class="form-hint">还没有添加网盘路径</span>';
    }

    /** 目录选择器：点目录进入下一层，点「选中当前目录」把它加进列表 */
    function openPicker(m, cid, trail) {
      const picker = m.querySelector('#pickRemote');
      const account = m.querySelector('#sAccount').value;
      const here = '/' + trail.map((x) => x.name).join('/');
      picker.hidden = false;
      picker.innerHTML = '<span class="form-hint">加载中…</span>';
      window.API.dirs(cid || 0, account).then((d) => {
        const dirs = d.dirs || [];
        const head = `
          <div class="dir-head">
            <button type="button" class="dir-opt" data-up ${trail.length ? '' : 'disabled'}>
              ${icon('folder', 12)} 上级</button>
            <span class="dir-path mono">${esc(trail.length ? here : '/')}</span>
            <button type="button" class="dir-opt dir-take" data-take>
              ${icon('plus', 12)} 选中当前目录</button>
          </div>`;
        picker.innerHTML = head + (dirs.length ? dirs.map((x) =>
          `<button type="button" class="dir-opt" data-enter="${esc(x.id)}" data-name="${esc(x.name)}">
            ${icon('folder', 12)} ${esc(x.name)}</button>`).join('') :
          '<span class="form-hint">这一层没有子目录</span>');
        picker.querySelector('[data-up]').onclick = () => {
          if (!trail.length) return;
          const rest = trail.slice(0, -1);
          const parent = rest.length ? rest[rest.length - 1].cid : 0;
          openPicker(m, parent, rest);
        };
        picker.querySelector('[data-take]').onclick = () => {
          if (!cid) { N.toast('先点进要添加的目录', 'err'); return; }
          if (picked.some((x) => x.cid === String(cid))) {
            N.toast('这个目录已经添加过了', 'err');
            return;
          }
          picked.push({ cid: String(cid), path: here, dir: defaultDir(here) });
          renderPairs(m, picked);
          picker.hidden = true;
          N.toast('已添加：' + here + ' → ' + defaultDir(here), 'ok');
        };
        picker.querySelectorAll('[data-enter]').forEach((b) => {
          b.onclick = () => openPicker(
            m, b.getAttribute('data-enter'),
            trail.concat([{ cid: b.getAttribute('data-enter'), name: b.getAttribute('data-name') }]),
          );
        });
      }).catch((err) => {
        picker.innerHTML = '<span class="form-hint">读取失败：' + esc(err.message) + '</span>';
      });
    }
  }

  /* ------------------------------------------------------------ 服务设置 */
  function serviceForm() {
    const set = D.db.settings() || {};
    const box = N.modal.open({
      title: 'STRM 服务设置',
      body: `
        <div class="form-row">
          <label>默认 STRM 请求头</label>
          <input class="input" id="ssBase" value="${esc(set.default_base || '')}"
            placeholder="http://172.17.0.1:9527">
        </div>
        <div class="form-row">
          <label>STRM 映射根目录</label>
          <input class="input mono" id="ssLocal" value="${esc(set.default_local || '/strm')}"
            placeholder="/strm">
          <span class="form-hint">本地目录由网盘目录自动预填到这里：网盘 <span class="mono">/影音/国漫</span>
            → 本地 <span class="mono">/strm/国漫</span>；配置里可以改成映射根下的任意目录</span>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>扫描并发</label>
            <input class="input" id="ssWorkers" type="number" min="1" max="16"
              value="${set.workers || 2}">
          </div>
          <div class="form-row">
            <label>定时巡检间隔（秒）</label>
            <input class="input" id="ssInterval" type="number" min="30" value="${set.interval || 600}">
          </div>
        </div>
        <p class="form-hint">新建配置时会自动带上这里的默认值；已有配置不受影响。</p>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存</button>',
      onOk() {
        const patch = Object.assign({}, D.db.settings(), {
          default_base: box.querySelector('#ssBase').value.trim(),
          default_local: box.querySelector('#ssLocal').value.trim(),
          workers: Number(box.querySelector('#ssWorkers').value) || 2,
          interval: Number(box.querySelector('#ssInterval').value) || 600
        });
        window.API.saveSettings(patch).then(() => {
          D.logs.add('info', 'STRM 服务设置已保存');
          N.toast('服务设置已保存', 'ok');
          N.modal.close();
          window.App.reload();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      }
    });
  }

  /* ------------------------------------------------------------ 事件日志 */
  const ELOG_LABEL = {
    run_start: '开始', run_done: '完成', run_warn: '警告', run_error: '失败',
    config_save: '配置', config_delete: '删除',
    scan_full: '全量扫描', scan_inc: '增量扫描',
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

  /** 拉一次事件日志。后端已经落库，刷新页面也能看到历史。 */
  function loadEvents(root) {
    const box = root.querySelector && root.querySelector('#elogList');
    const hint = root.querySelector && root.querySelector('#elogHint');
    if (!box) return;
    window.API.strmEvents(80).then((d) => {
      const list = (d && d.events) || [];
      box.innerHTML = list.length ? list.map(eventRow).join('')
        : '<span class="form-hint">还没有事件（跑一次生成就会记录下来）</span>';
      if (hint) hint.textContent = '最近 ' + list.length + ' 条';
    }).catch((err) => {
      box.innerHTML = '<span class="form-hint">读取失败：' + esc(err.message) + '</span>';
    });
  }

  function clearEvents(root) {
    N.confirm('清空事件日志', '只删除记录，不影响已生成的 strm。',
      { danger: true, okText: '清空' }).then((ok) => {
        if (!ok) return;
        window.API.clearStrmEvents().then(() => {
          N.toast('事件日志已清空', 'ok');
          loadEvents(root);
        }).catch((err) => N.toast(err.message, 'err'));
      });
  }

  /* ------------------------------------------------------------ 运行（增量 / 全量） */
  // 卡片状态是后端给的（idle/running/error）。提交后必须自己盯着，
  // 否则后端跑完了前端还挂着「运行中」，非要手动刷新页面才变回来。
  const watchers = {};
  function watchStatus(id, tries) {
    const round = tries || 0;
    if (watchers[id]) clearTimeout(watchers[id]);
    if (round > 100) { delete watchers[id]; return; }   // 最多盯 5 分钟
    watchers[id] = setTimeout(() => {
      window.API.strmConfigs().then((d) => {
        const list = (d && d.configs) || [];
        const row = list.find((x) => x.id === id);
        const before = (D.db.state.strmConfigs || []).find((x) => x.id === id) || {};
        D.db.state.strmConfigs = list;
        if (row && row.status !== before.status) window.App.rerender();
        if (row && row.status === 'running') {
          watchStatus(id, round + 1);
        } else {
          delete watchers[id];
          window.App.rerender();
        }
      }).catch(() => watchStatus(id, round + 1));
    }, 3000);
  }

  const MODE_TEXT = { inc: '增量', full: '全量', rebuild: '全新' };

  function runTask(id, mode) {
    const tasks = D.db.state.strmConfigs || [];
    const t = tasks.find((x) => x.id === id);
    if (!t) return;
    if (t.status === 'running') { N.toast('该任务正在运行', 'err'); return; }
    const text = MODE_TEXT[mode] || '增量';
    const go = () => {
      t.status = 'running';
      D.db.state.strmConfigs = tasks;
      D.logs.add('info', 'STRM ' + text + '生成开始：<b>' + esc(t.name) + '</b>（' +
        esc(t.remote_paths || t.remote_path || t.remote_cid) + '）');
      N.toast(text + '生成已开始');
      window.App.rerender();
      window.API.runStrmConfig(id, mode).then(() => {
        N.toast(text + '生成已提交，完成后卡片会自动更新', 'ok');
        watchStatus(id, 0);
      }).catch((err) => {
        N.toast(err.message, 'err');
        t.status = 'idle';
        window.App.rerender();
      });
    };
    if (mode === 'rebuild') {
      // 全新生成会先删掉这条配置生成过的 .strm，必须让用户明确知道
      N.confirm('全新生成',
        '会先删除「' + esc(t.name) + '」生成过的**所有 .strm**' +
        '（只删这条配置记录过的那些，别的配置 / 别的账号的 strm 不会动；' +
        'nfo / 字幕 / 海报等其它文件也保留），然后再从头生成一遍。' +
        '适用于「strm 被删了一部分、全量补不回来」的情况。',
        { danger: true, okText: '开始全新生成' }).then((ok) => { if (ok) go(); });
      return;
    }
    go();
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-add]')) { taskForm(null); return; }
      if (e.target.closest('[data-service]')) { serviceForm(); return; }
      if (e.target.closest('[data-elog-refresh]')) { loadEvents(root); return; }
      if (e.target.closest('[data-elog-clear]')) { clearEvents(root); return; }
      // 卡片不再按账号分组，data-add-for 暂时用不到；留着方便以后恢复分组视图
      const addFor = e.target.closest('[data-add-for]');
      if (addFor) {
        taskForm(null, addFor.getAttribute('data-add-for'));
        return;
      }
      const card = e.target.closest('[data-task]');
      if (!card) return;
      const id = card.getAttribute('data-task');
      const task = (D.db.state.strmConfigs || []).find((x) => x.id === id);
      if (!task) return;
      if (e.target.closest('[data-edit]')) { taskForm(task); return; }
      if (e.target.closest('[data-inc]')) { runTask(id, 'inc'); return; }
      if (e.target.closest('[data-full]')) { runTask(id, 'full'); return; }
      if (e.target.closest('[data-rebuild]')) { runTask(id, 'rebuild'); return; }
      if (e.target.closest('[data-del]')) {
        N.confirm('删除配置', '删除「' + esc(task.name) + '」？已生成的 strm 文件不会自动清理。',
          { danger: true, okText: '删除' }).then((ok) => {
            if (!ok) return;
            window.API.removeStrmConfig(id).then(() => {
              D.logs.add('warn', '删除 STRM 配置「' + esc(task.name) + '」');
              N.toast('已删除', 'ok');
              window.App.reload();
            }).catch((err) => N.toast(err.message, 'err'));
          });
      }
    });
  }

  window.ToolStrm115 = { page };
})();
