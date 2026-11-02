/* ==========================================================================
   views.js · 运行状态 / 媒体服务器 / Emby 反代
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  /* ==========================================================================
     运行状态
     ========================================================================== */
  function status() {
    const st = D.db.status() || {};
    const stats = st.db || {};
    const watch = st.watch || {};
    const scan = st.scan || {};
    const api = st.api || {};
    const blocked = api.blocked_for || 0;

    const stat = (ico, cls, label, num, desc) => `
      <div class="strm-stat">
        <span class="ss-ico ${cls}">${icon(ico, 18)}</span>
        <div class="ss-main">
          <div class="ss-label">${esc(label)}</div>
          <div class="ss-num">${esc(num)}</div>
          ${desc ? `<div class="ss-desc">${esc(desc)}</div>` : ''}
        </div>
      </div>`;

    const rows = [
      ['运行模式', st.mode || '—'],
      ['strm 输出目录', st.strm_root || '—'],
      ['索引目录数', String(stats.dirs || 0)],
      ['索引文件数', String(stats.files || 0)],
      ['已生成 strm', String(stats.strm || 0)],
      ['上次扫描', scan.finished_at ? N.stamp(new Date(scan.finished_at * 1000)) : '—'],
      ['上次扫描文件数', scan.files === undefined ? '—' : String(scan.files)],
      ['115 接口', blocked > 0
        ? `限流中，还剩 ${blocked} 秒（WAF 405）`
        : `正常（${api.qps || 0} QPS）`],
      ['增量方式', {
        event: '生活事件（实时）',
        interval: '定时增量扫描',
        probing: '探测中'
      }[String(watch.mode)] || '—'],
      ['事件游标', watch.cursor || '—'],
      ['上次轮询', watch.last_poll || '—'],
      ['播放方式', st.play_mode || '—'],
      ['多账号', `${D.db.accounts().length} 个`],
      ['ed2k 任务', String((st.ed2k || {}).tasks || 0)],
      ['分享转存', String((st.share || {}).tasks || 0)],
      ['秒传记录', String((st.instant || {}).records || 0)],
      ['Emby 反代', (st.emby || {}).enabled
        ? `已启用（${(st.emby || {}).url || '—'}，挂载在 /emby）`
        : '未启用']
    ];

    return {
      html: `
        <div class="page-head">
          <div>
            <h1>运行状态</h1>
            <p>服务总览：索引、扫描、增量与各工具的当前状态。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-poll>${icon('refresh', 14)} 立即增量同步</button>
            <button class="btn btn-primary btn-sm" data-scan>${icon('play', 14)} 全量扫描</button>
          </div>
        </div>

        ${(st.login && !st.login.ok) ? `
        <div class="card" style="background:var(--warn-soft);border-color:transparent">
          <div class="card-head" style="margin-bottom:0">
            <div>
              <h2 style="color:var(--warn)">还没有可用的 115 账号</h2>
              <p class="card-sub">首次使用只需粘贴一次 cookies：不用改配置文件，也不用重启容器</p>
            </div>
            <div class="card-actions">
              <a class="btn btn-sm btn-primary" href="#/tools/p115">去「115 助手」添加</a>
            </div>
          </div>
        </div>` : ''}

        <div class="strm-stats">
          ${stat('layers', 'v1', '索引文件数', String(stats.files || 0))}
          ${stat('play', 'v2', '已生成 strm', String(stats.strm || 0))}
          ${stat('clock', 'v3', '索引目录数', String(stats.dirs || 0))}
          ${stat('scan', 'v4', '账号数', String(D.db.accounts().length))}
        </div>

        <div class="card">
          <div class="card-head"><h2>详细信息</h2></div>
          <div class="kv">
            ${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v)}</dd>`).join('')}
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>登录页背景（TMDB）</h2>
            <p class="card-sub">随机取一张 TMDB 热门影片剧照当登录页背景；保存后立即生效，不用重启容器</p>
          </div>
          <div class="form-grid two">
            <div class="form-row">
              <label>TMDB API Key（v3）</label>
              <input class="input mono" id="bdKey" placeholder="themoviedb.org → 设置 → API">
              <span class="form-hint" id="bdKeyHint"></span>
            </div>
            <div class="form-row">
              <label>TMDB 代理（可选）</label>
              <input class="input mono" id="bdProxy" placeholder="http://192.168.1.10:7890">
              <span class="form-hint">容器直连 api.themoviedb.org 常握手超时，填宿主机的代理；只作用于 TMDB 请求</span>
            </div>
          </div>
          <div class="form-row">
            <label>自定义图片直链（可选，逗号分隔）</label>
            <input class="input mono" id="bdCustom" placeholder="https://…/a.jpg, https://…/b.jpg">
            <span class="form-hint">没有 TMDB key 或 TMDB 拉不到时用这些图兜底</span>
          </div>
          <div class="inline">
            <button class="btn btn-primary btn-sm" data-bd-save>${icon('check', 13)} 保存</button>
            <button class="btn btn-sm" data-bd-test>${icon('zap', 13)} 测试拉取</button>
            <span class="form-hint" id="bdStatus">加载中…</span>
          </div>
        </div>`,
      mount(root) {
        root.querySelector('[data-scan]').onclick = async () => {
          N.toast('全量扫描已提交，后台执行中…');
          try {
            await window.API.scan(true);
            D.logs.add('info', '手动触发全量扫描');
          } catch (e) { N.toast(e.message, 'err'); }
          window.App.rerender();
        };
        root.querySelector('[data-poll]').onclick = async () => {
          N.toast('增量同步中…');
          try {
            const d = await window.API.poll();
            D.logs.add('info', d.mode === 'interval'
              ? '手动增量扫描完成' : '手动拉取事件完成，处理 ' + (d.handled || 0) + ' 条');
          } catch (e) { N.toast(e.message, 'err'); }
          window.App.rerender();
        };
        bindBackdrop(root);
      }
    };
  }

  /* ==========================================================================
     登录页背景（TMDB）：key / 代理 / 自定义直链，保存即生效
     ========================================================================== */
  /** 把后端状态翻成一句人话，直接贴在设置面板上 */
  function backdropText(st, env) {
    st = st || {};
    if (!st.available) {
      return '未配置：填一个 TMDB API Key，或至少给几张自定义图片直链（现在用渐变背景）';
    }
    if (st.error && !st.count) {
      return '拉取失败：' + st.error + '（网络不通时试试填代理）';
    }
    if (!st.count) {
      return '已配置，还没拉过片单（点「测试拉取」看看）';
    }
    return '就绪 · ' + st.count + ' 张剧照' + (st.proxy ? ' · 走代理' : '');
  }

  function bindBackdrop(root) {
    const keyEl = root.querySelector('#bdKey');
    const proxyEl = root.querySelector('#bdProxy');
    const customEl = root.querySelector('#bdCustom');
    const statusEl = root.querySelector('#bdStatus');
    const hintEl = root.querySelector('#bdKeyHint');
    if (!keyEl || !statusEl) return;

    window.API.backdropSettings().then((d) => {
      const s = (d && d.settings) || {};
      keyEl.value = s.api_key || '';
      proxyEl.value = s.proxy || '';
      customEl.value = (s.custom || []).join(', ');
      statusEl.textContent = backdropText((d && d.status) || {}, d && d.env);
      const env = (d && d.env) || {};
      if (hintEl && (env.api_key || env.read_token)) {
        hintEl.textContent = '环境变量里已经配了 key：这里留空则继续用环境变量';
      }
    }).catch((err) => { statusEl.textContent = '读取失败：' + err.message; });

    root.querySelector('[data-bd-save]').onclick = () => {
      window.API.saveBackdropSettings({
        api_key: keyEl.value.trim(),
        proxy: proxyEl.value.trim(),
        custom: customEl.value.trim()
      }).then((d) => {
        D.logs.add('info', '登录页背景配置已保存');
        N.toast('已保存，重新打开登录页就能看到新背景', 'ok');
        statusEl.textContent = backdropText((d && d.status) || {});
      }).catch((err) => N.toast(err.message, 'err'));
    };

    root.querySelector('[data-bd-test]').onclick = () => {
      statusEl.textContent = '正在拉取…';
      window.API.testBackdrop().then((d) => {
        if (d.ok) {
          statusEl.textContent = '拉取成功 · ' + d.count + ' 张剧照';
          N.toast('TMDB 正常，拿到 ' + d.count + ' 张剧照', 'ok');
          return;
        }
        statusEl.textContent = d.reason === 'no_tmdb_key'
          ? '还没配置 key（或自定义图片），背景保持渐变'
          : '拉取失败：' + (((d.status || {}).error) || '网络不通，试试填代理');
        N.toast('没拉到剧照，看下面的提示', 'err');
      }).catch((err) => { statusEl.textContent = '测试失败：' + err.message; });
    };
  }

  /* ==========================================================================
     媒体服务器（可添加多个 Emby：配置名称 / Emby 地址 / API Key）
     ========================================================================== */
  function mediaServer() {
    const list = D.db.mediaServers();
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>媒体服务器</h1>
            <p>添加多个 Emby 服务器，分别配置名称、地址与 API Key；反代默认使用第一个启用的服务器。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-primary btn-sm" data-add>${icon('plus', 14)} 添加服务器</button>
          </div>
        </div>

        <div class="card tight">
          <div class="row-list">
            ${list.length ? list.map((s) => `
              <div class="row-item" data-srv="${esc(s.id)}">
                <span class="stat-ico v4" style="width:40px;height:40px;border-radius:12px">${icon('server', 18)}</span>
                <div class="row-main">
                  <div class="row-title">${esc(s.name)}
                    <span class="tag ${s.enabled ? 'ok' : ''}">${s.enabled ? '已启用' : '已停用'}</span>
                  </div>
                  <div class="row-desc mono">${esc(s.host)}</div>
                  <div class="row-desc">API Key：${s.api_key
                    ? esc(String(s.api_key).slice(0, 6) + '••••••') : '未设置'}${s.note ? ' · ' + esc(s.note) : ''}</div>
                </div>
                <div class="row-actions">
                  <button class="btn btn-sm" data-edit>${icon('pen', 13)} 编辑</button>
                  <button class="btn btn-sm btn-danger" data-del>${icon('trash', 13)}</button>
                </div>
              </div>`).join('') :
              `<div class="empty"><span class="empty-ico">${icon('server', 20)}</span>还没有媒体服务器，点右上角添加</div>`}
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>说明</h2></div>
          <p class="form-hint" style="margin:0">
            Emby 反代默认使用第一个启用的服务器作为上游；端口、路径前缀与缓存策略
            请到「媒体与反代 → Emby 反代」页面手动调整。
          </p>
        </div>`,
      mount(root) {
        N.bindOnce(root, 'click', (e) => {
          if (e.target.closest('[data-add]')) { embyForm(null); return; }
          const row = e.target.closest('[data-srv]');
          if (!row) return;
          const srv = D.db.mediaServers().find((s) => s.id === row.getAttribute('data-srv'));
          if (!srv) return;
          if (e.target.closest('[data-edit]')) { embyForm(srv); return; }
          if (e.target.closest('[data-del]')) {
            N.confirm('删除媒体服务器', '确定删除「' + esc(srv.name) + '」？不影响 Emby 本身。',
              { danger: true, okText: '删除' }).then((ok) => {
                if (!ok) return;
                window.API.removeMediaServer(srv.id).then(() => {
                  D.logs.add('warn', '删除媒体服务器「' + esc(srv.name) + '」');
                  N.toast('已删除', 'ok');
                  window.App.reload();
                }).catch((err) => N.toast(err.message, 'err'));
              });
          }
        });
      }
    };
  }

  function embyForm(srv) {
    const box = N.modal.open({
      title: srv ? '编辑媒体服务器' : '添加媒体服务器',
      body: `
        <div class="form-row">
          <label>配置名称</label>
          <input class="input" id="msName" value="${esc(srv ? srv.name : '')}"
            placeholder="例如：客厅 Emby">
        </div>
        <div class="form-row">
          <label>Emby 地址</label>
          <input class="input" id="msHost" value="${esc(srv ? srv.host : '')}"
            placeholder="http://192.168.1.10:8096">
          <span class="form-hint">Emby 的访问地址，含端口</span>
        </div>
        <div class="form-row">
          <label>API Key</label>
          <input class="input" id="msKey" value="${esc(srv ? srv.api_key : '')}"
            placeholder="Emby 后台 → 高级 → API 密钥">
        </div>
        <div class="form-row">
          <label>备注</label>
          <input class="input" id="msNote" value="${esc(srv ? srv.note : '')}"
            placeholder="可选，例如：4K 客厅">
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">启用</div>
            <div class="sr-desc">停用后反代不会使用这个服务器</div></div>
          <label class="switch"><input type="checkbox" id="msEnabled"
            ${srv ? (srv.enabled ? 'checked' : '') : 'checked'}><span class="track"></span></label>
        </div>`,
      footer: '<button class="btn" data-close-modal>取消</button>' +
        '<button class="btn btn-primary" data-modal-ok>保存</button>',
      onOk() {
        const name = box.querySelector('#msName').value.trim();
        const host = box.querySelector('#msHost').value.trim();
        const apiKey = box.querySelector('#msKey').value.trim();
        if (!host) { N.toast('Emby 地址不能为空', 'err'); return false; }
        if (!/^https?:\/\//i.test(host)) {
          N.toast('Emby 地址要以 http:// 或 https:// 开头', 'err');
          return false;
        }
        window.API.saveMediaServer({
          sid: srv ? srv.id : '', name, host, api_key: apiKey,
          enabled: box.querySelector('#msEnabled').checked,
          note: box.querySelector('#msNote').value.trim()
        }).then(() => {
          D.logs.add('info', '媒体服务器已保存：<b>' + esc(name || host) + '</b>');
          N.toast('已保存', 'ok');
          N.modal.close();
          window.App.reload();
        }).catch((err) => N.toast(err.message, 'err'));
        return false;
      }
    });
  }

  /* ==========================================================================
     Emby 反代（全部可手动修改）
     ========================================================================== */
  function proxy() {
    const cfg = D.db.proxy() || {};
    const servers = D.db.mediaServers();
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>Emby 反代</h1>
            <p>改写 PlaybackInfo，把播放地址直接下发为网盘直链；天翼云盘的 strm 也走这里换发直链。端口、上游、缓存策略都可以手动修改。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-check>${icon('refresh', 14)} 测试连通性</button>
            <button class="btn btn-primary btn-sm" data-save>${icon('check', 14)} 保存</button>
          </div>
        </div>

        <div class="card">
          <div class="switch-row" style="padding-top:0">
            <div class="sr-main">
              <div class="sr-title">启用 Emby 反代</div>
              <div class="sr-desc">关闭后客户端直连 Emby，不再改写播放地址</div>
            </div>
            <label class="switch dark"><input type="checkbox" id="pxEnabled"
              ${cfg.enabled ? 'checked' : ''}><span class="track"></span></label>
          </div>

          <div class="form-grid two mt12">
            <div class="form-row">
              <label>上游 Emby</label>
              <select class="select" id="pxServer">
                <option value="">手动填写地址</option>
                ${servers.map((s) => `<option value="${esc(s.host)}"
                  ${cfg.upstream === s.host ? 'selected' : ''}>${esc(s.name)} · ${esc(s.host)}</option>`).join('')}
              </select>
              <span class="form-hint">从「媒体服务器」里选，或在下面手动填地址</span>
            </div>
            <div class="form-row">
              <label>上游地址</label>
              <input class="input" id="pxUpstream" value="${esc(cfg.upstream || '')}"
                placeholder="http://emby:8096">
            </div>
          </div>

          <div class="form-grid two">
            <div class="form-row">
              <label>监听端口</label>
              <input class="input" id="pxPort" type="number" min="0" max="65535"
                value="${cfg.proxy_port || 0}">
              <span class="form-hint">客户端服务器地址填 http://本机:${cfg.proxy_port || 9527}（不用加任何路径前缀）；0 = 用本服务端口 ${9527} 的 /emby。改完保存立即生效</span>
            </div>
            <div class="form-row">
              <label>strm 挂载前缀</label>
              <input class="input" id="pxPrefix" value="${esc(cfg.strm_prefix || '')}" placeholder="留空自动识别">
              <span class="form-hint">Emby 里 strm 目录的挂载路径（一般留空自动识别）；这不是客户端地址的前缀</span>
            </div>
          </div>

          <div class="form-grid two">
            <div class="form-row">
              <label>对外访问地址（写入 strm 的请求头）</label>
              <input class="input" id="pxBase" value="${esc(cfg.base_url || '')}"
                placeholder="http://172.17.0.1:9527">
            </div>
            <div class="form-row">
              <label>直链缓存时间（秒）</label>
              <input class="input" id="pxCache" type="number" min="0" value="${cfg.cache_seconds || 900}">
              <span class="form-hint">0 表示不缓存，每次播放都重新取链</span>
            </div>
          </div>

          <div class="form-row">
            <label>日志级别</label>
            <select class="select" id="pxLog" style="max-width:220px">
              ${['debug', 'info', 'warn', 'error'].map((l) =>
                `<option value="${l}" ${cfg.log_level === l ? 'selected' : ''}>${l}</option>`).join('')}
            </select>
          </div>

          <div class="form-row">
            <label>路径替换规则</label>
            <textarea class="input" id="pxRules" rows="4" spellcheck="false"
              placeholder="每行一条：正则=>模板">${esc((cfg.path_rules || []).join('\n'))}</textarea>
            <span class="form-hint">模板里可用 $1..$9 引用捕获组、{base} 是本服务地址</span>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>说明</h2></div>
          <div class="kv">
            <dt>反代入口</dt><dd class="mono">http://本机:${cfg.proxy_port || 9527}${cfg.proxy_port ? '' : '/emby'}</dd>
            <dt>路径前缀</dt><dd>不需要。独立端口直接用根路径，客户端地址不用加任何前缀</dd>
            <dt>改写规则</dt><dd>strm 路径 → pickcode → 直链（按播放设备 UA 取链）</dd>
            <dt>天翼云盘</dt><dd>天翼 strm 里的地址会换成<b>这个反代端口</b>（
              <span class="mono">/p189/play/…</span>）：播放器来这里现取直链，
              再 302 到天翼 CDN，视频流量不经过本机</dd>
            <dt>回退</dt><dd>直链取不到时退回本服务地址，播放器再 302 / 中转</dd>
          </div>
        </div>

        <div class="card" id="pxCheckCard" hidden>
          <div class="card-head"><h2>连通性检测</h2></div>
          <div class="kv" id="pxCheckResult">检测中…</div>
        </div>`,
      mount(root) {
        root.querySelector('#pxServer').addEventListener('change', (e) => {
          if (e.target.value) root.querySelector('#pxUpstream').value = e.target.value;
        });
        root.querySelector('[data-check]').onclick = () => {
          const card = root.querySelector('#pxCheckCard');
          const out = root.querySelector('#pxCheckResult');
          card.hidden = false;
          out.textContent = '检测中…';
          window.API.checkProxy().then((r) => {
            const up = r.upstream || {};
            const pt = r.port || {};
            const lines = [];
            lines.push('<dt>上游 Emby</dt><dd class="mono">' +
              esc(up.url || '(未配置)') +
              (up.error ? ' — <span class="tag err">' + esc(up.error) + '</span>'
                : ' — ' + esc(String(up.status))) + '</dd>');
            lines.push('<dt>独立端口</dt><dd class="mono">' + esc(pt.state || '未知') + '</dd>');
            if (r.ok && pt.listening) {
              lines.push('<dt>客户端地址</dt><dd class="mono">http://本机:' +
                esc(String(pt.listening)) + '</dd>');
            }
            out.innerHTML = lines.join('');
          }).catch((err) => { out.textContent = '检测失败：' + err.message; });
        };
        root.querySelector('[data-save]').onclick = () => {
          const payload = {
            enabled: root.querySelector('#pxEnabled').checked,
            upstream: root.querySelector('#pxUpstream').value.trim(),
            strm_prefix: root.querySelector('#pxPrefix').value.trim(),
            proxy_port: Number(root.querySelector('#pxPort').value) || 0,
            cache_seconds: Number(root.querySelector('#pxCache').value) || 0,
            log_level: root.querySelector('#pxLog').value,
            path_rules: root.querySelector('#pxRules').value,
            base_url: root.querySelector('#pxBase').value.trim()
          };
          window.API.saveProxy(payload).then(() => {
            D.logs.add('info', 'Emby 反代配置已更新');
            N.toast('反代配置已保存', 'ok');
            window.App.reload();
          }).catch((err) => N.toast(err.message, 'err'));
        };
      }
    };
  }

  window.Views = { status, mediaServer, proxy };
})();
