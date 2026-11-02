/* ==========================================================================
   tool-notify.js · 通知（企业微信 / TG）与双向联动
   --------------------------------------------------------------------------
   通知：推送任务状态，并支持在企微 / TG 里发指令（/status /run /find …）。
   联动：本地删 strm → 删网盘原文件；网盘删文件 → 清本地 strm + 刮削文件。
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;
  const icon = N.icon, esc = N.esc;

  let nState = null;      // /api/notify
  let lState = null;      // /api/link
  let pending = [];       // /api/link/pending
  let busy = false;

  const MASK = '••••••';

  function page() {
    return {
      html: `
        <div class="page-head">
          <div>
            <h1>通知与联动</h1>
            <p>任务开始 / 完成 / 失败推送到企业微信、Telegram，并可直接在聊天窗口里发指令；
              本地 strm 与网盘文件可以双向联动删除（默认只做「网盘 → 本地」）。</p>
          </div>
          <div class="head-actions">
            <button class="btn btn-sm" data-refresh>${icon('refresh', 14)} 刷新</button>
            <button class="btn btn-primary btn-sm" data-save>${icon('check', 14)} 保存配置</button>
          </div>
        </div>
        <div id="ntBody"><div class="card"><div class="empty">加载中…</div></div></div>`,
      mount(root) {
        bind(root);
        load(root);
      }
    };
  }

  /* ------------------------------------------------------------ 渲染 */
  function renderBody(root) {
    const box = root.querySelector('#ntBody');
    if (!box) return;
    if (!nState || !lState) {
      box.innerHTML = '<div class="card"><div class="empty">加载中…</div></div>';
      return;
    }
    const n = nState.settings || {};
    const wc = n.wecom || {};
    const tg = n.tg || {};
    const wh = n.webhook || {};
    const ev = n.events || {};
    const lk = lState.settings || {};
    const last = lState.last || {};

    box.innerHTML = `
      <div class="card">
        <div class="card-head">
          <h2>通知渠道</h2>
          <p class="card-sub">推送任务状态；也可以直接在聊天里发指令做互动</p>
        </div>

        <div class="switch-row" style="padding-top:0">
          <div class="sr-main"><div class="sr-title">启用通知</div>
            <div class="sr-desc">总开关：关闭后所有渠道都不发消息（指令仍然可用）</div></div>
          <label class="switch"><input type="checkbox" id="ntEnabled" ${n.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>

        <div class="divider"></div>

        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">企业微信</div>
            <div class="sr-desc">群机器人（只推送）或自建应用（可接收指令，需要公网回调地址）</div></div>
          <label class="switch"><input type="checkbox" id="wcEnabled" ${wc.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="form-row">
          <label>群机器人 Webhook</label>
          <input class="input mono" id="wcWebhook" value="${esc(wc.webhook || '')}"
            placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...">
          <span class="form-hint">企微群 → 群机器人 → 添加，复制 Webhook；填了就不再用下面的应用消息</span>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>企业 ID（corp_id）</label>
            <input class="input mono" id="wcCorp" value="${esc(wc.corp_id || '')}">
          </div>
          <div class="form-row">
            <label>应用 AgentId</label>
            <input class="input mono" id="wcAgent" value="${esc(wc.agent_id || '')}">
          </div>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>应用 Secret</label>
            <input class="input mono" id="wcSecret" value="${esc(wc.secret || '')}"
              placeholder="${wc.secret ? '已保存，留空/不改则保持' : '应用详情里的 Secret'}">
          </div>
          <div class="form-row">
            <label>接收人</label>
            <input class="input mono" id="wcTo" value="${esc(wc.to_user || '@all')}">
          </div>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>回调 Token</label>
            <input class="input mono" id="wcToken" value="${esc(wc.token || '')}"
              placeholder="应用 → 接收消息 → Token">
          </div>
          <div class="form-row">
            <label>EncodingAESKey</label>
            <input class="input mono" id="wcAes" value="${esc(wc.aes_key || '')}"
              placeholder="43 位，接收消息 → EncodingAESKey">
          </div>
        </div>
        ${nState.wecom_callback ? `<div class="form-row">
          <label>接收消息回调地址（填到企微应用里）</label>
          <div class="code-block">${esc(nState.wecom_callback)}</div>
          <span class="form-hint">企微服务器要能访问到这个地址（内网需要端口映射 / 内网穿透）；
            回调是明文 HTTP 也可以，但 Token 与 AESKey 要填对</span>
        </div>` : ''}

        <div class="divider"></div>

        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">Telegram Bot</div>
            <div class="sr-desc">Bot 推送 + 长轮询接收指令，不需要公网地址</div></div>
          <label class="switch"><input type="checkbox" id="tgEnabled" ${tg.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>Bot Token</label>
            <input class="input mono" id="tgToken" value="${esc(tg.token || '')}"
              placeholder="abcdef:ABC-DEF...">
          </div>
          <div class="form-row">
            <label>Chat ID</label>
            <input class="input mono" id="tgChat" value="${esc(tg.chat_id || '')}"
              placeholder="给 bot 发一条消息后 /status 会提示">
          </div>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>代理（可选）</label>
            <input class="input mono" id="tgProxy" value="${esc(tg.proxy || '')}"
              placeholder="http://192.168.2.1:7890">
            <span class="form-hint">国内直连 api.telegram.org 不通时填</span>
          </div>
          <div class="form-row">
            <label>允许发指令的用户</label>
            <input class="input mono" id="tgAllow" value="${esc(tg.allow || '')}"
              placeholder="用户 id 或 @username，逗号分隔；留空 = 只认上面的 chat_id">
          </div>
        </div>
        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">接收指令（长轮询）</div>
            <div class="sr-desc">关闭后只推送，不响应 /status /run 等指令</div></div>
          <label class="switch"><input type="checkbox" id="tgPoll" ${tg.poll ? 'checked' : ''}><span class="track"></span></label>
        </div>

        <div class="divider"></div>

        <div class="switch-row">
          <div class="sr-main"><div class="sr-title">Webhook / Emby 通知</div>
            <div class="sr-desc">POST 一段 JSON 给任意接收端。选了媒体服务器会自动带上 Emby 的
              api_key（<span class="mono">X-Emby-Token</span>），地址留空时默认推到 Emby 消息中心
              <span class="mono">/emby/Notifications/Admin</span></div></div>
          <label class="switch"><input type="checkbox" id="whEnabled" ${wh.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>用媒体服务器（可选）</label>
            <select class="select" id="whServer">
              <option value="">— 不用，纯自定义地址 —</option>
              ${(D.db.mediaServers() || []).map((sv) =>
                `<option value="${esc(sv.id)}" ${String(wh.server || '') === String(sv.id) ? 'selected' : ''}>${esc(sv.name || sv.host)}</option>`).join('')}
            </select>
            <span class="form-hint">在「媒体服务器」里配好 Emby 就能直接选</span>
          </div>
          <div class="form-row">
            <label>Webhook 地址</label>
            <input class="input mono" id="whUrl" value="${esc(wh.url || '')}"
              placeholder="http://192.168.1.10:8096/emby/Notifications/Admin">
          </div>
        </div>
        <div class="form-row">
          <label>自定义 Header（每行一个，可留空）</label>
          <textarea class="input mono" id="whHeaders" rows="2"
            placeholder="Authorization: Bearer xxx">${esc(wh.headers || '')}</textarea>
        </div>
        <div class="form-row">
          <label>JSON 模板（留空用默认）</label>
          <textarea class="input mono" id="whTemplate" rows="3"
            placeholder='{"Event":"panshare.{{event}}","Name":"{{title}}","Description":"{{text}}","Level":"{{level}}"}'>${esc(wh.template || '')}</textarea>
          <span class="form-hint">占位符：<span class="mono">{{title}}</span>
            <span class="mono">{{text}}</span> <span class="mono">{{event}}</span>
            <span class="mono">{{level}}</span> <span class="mono">{{date}}</span>
            <span class="mono">{{version}}</span>（level 会换成 info / warning / error）</span>
        </div>

        <div class="divider"></div>

        <div class="form-row">
          <label>推送哪些事件</label>
          <div class="inline" style="flex-wrap:wrap;gap:12px">
            ${eventBox('evRunStart', '任务开始', ev.run_start)}
            ${eventBox('evRunDone', '任务完成', ev.run_done)}
            ${eventBox('evRunError', '任务失败', ev.run_error)}
            ${eventBox('evLink', '联动删除', ev.link)}
            ${eventBox('evScan', '扫描异常', ev.scan)}
          </div>
        </div>
        <div class="inline">
          <button class="btn btn-sm" data-test-notify>${icon('upload', 13)} 发送测试消息</button>
          <span class="form-hint" id="ntMsg">${
            nState.last_error ? '上次错误：' + esc(nState.last_error)
              : (nState.sent ? '已发送 ' + nState.sent + ' 条' : '')}</span>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>双向联动</h2>
          <p class="card-sub">本地 strm 与网盘文件保持一致；删除网盘文件是不可逆操作（会进 115 回收站）</p>
        </div>

        <div class="stat-strip">
          <div class="ss"><div class="ss-label">本轮检查</div>
            <div class="ss-val">${last.at ? N.stamp(new Date(last.at * 1000)) : '还没跑过'}</div></div>
          <div class="ss"><div class="ss-label">索引内 strm</div>
            <div class="ss-val">${lState.indexed || 0} 个</div></div>
          <div class="ss"><div class="ss-label">缺失 / 核对</div>
            <div class="ss-val">${last.missing || 0} / ${last.checked || 0}</div></div>
          <div class="ss"><div class="ss-label">待删除</div>
            <div class="ss-val">${lState.pending || 0} 个</div></div>
          <div class="ss"><div class="ss-label">累计已删</div>
            <div class="ss-val">${last.deleted || 0} 个</div></div>
        </div>

        <div class="switch-row">
          <div class="sr-main">
            <div class="sr-title">本地删 strm → 删除网盘原文件</div>
            <div class="sr-desc"><b>危险</b>：strm 消失（超过宽限期）就删掉 115 上对应的文件。
              默认关闭；建议先开「试运行」看一遍待删列表</div>
          </div>
          <label class="switch dark"><input type="checkbox" id="lkToRemote" ${lk.local_to_remote ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main">
            <div class="sr-title">网盘删文件 → 同步删本地</div>
            <div class="sr-desc">115 上删除后，清掉本地 strm（关掉则保留本地文件，播放会 404）</div>
          </div>
          <label class="switch"><input type="checkbox" id="lkToLocal" ${lk.remote_to_local !== false ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main">
            <div class="sr-title">同时清理刮削文件</div>
            <div class="sr-desc">删 strm 时一并删掉同名的 nfo / 海报 / 字幕、影片-poster.jpg 等</div>
          </div>
          <label class="switch"><input type="checkbox" id="lkScrape" ${lk.scrape_clean !== false ? 'checked' : ''}><span class="track"></span></label>
        </div>
        <div class="switch-row">
          <div class="sr-main">
            <div class="sr-title">试运行（只看不删）</div>
            <div class="sr-desc">只把「将要删除」记进日志和待删列表，不真正调用 115 删除</div>
          </div>
          <label class="switch"><input type="checkbox" id="lkDry" ${lk.dry_run ? 'checked' : ''}><span class="track"></span></label>
        </div>

        <div class="form-grid two">
          <div class="form-row">
            <label>宽限期（秒）</label>
            <input class="input" id="lkGrace" type="number" min="0" step="30" value="${lk.grace_seconds || 0}">
            <span class="form-hint">strm 消失后等这么久才认定是删除（挪文件/重生成都在这个窗口内恢复）</span>
          </div>
          <div class="form-row">
            <label>检查间隔（秒）</label>
            <input class="input" id="lkInterval" type="number" min="30" step="30" value="${lk.interval || 120}">
          </div>
        </div>
        <div class="form-grid two">
          <div class="form-row">
            <label>熔断比例（%）</label>
            <input class="input" id="lkRatio" type="number" min="1" max="100" value="${Math.round((lk.max_ratio || 0.2) * 100)}">
            <span class="form-hint">一轮里缺失超过这个比例就整体跳过（防挂载掉线时误删全部）</span>
          </div>
          <div class="form-row">
            <label>最少缺失个数</label>
            <input class="input" id="lkMin" type="number" min="1" value="${lk.min_missing || 5}">
          </div>
        </div>

        <div class="inline" style="flex-wrap:wrap">
          <button class="btn btn-sm" data-link-check>${icon('refresh', 13)} 立即检查一轮</button>
          <button class="btn btn-sm" data-link-pending>${icon('list', 13)} 查看待删除</button>
          <button class="btn btn-sm btn-danger" data-link-commit>${icon('trash', 13)} 确认删除待删项</button>
          <button class="btn btn-sm btn-danger" data-link-force>${icon('zap', 13)} 强制检查（忽略熔断）</button>
          <span class="form-hint">${last.aborted ? '上一轮：' + esc(last.aborted) : ''}</span>
        </div>
        <div id="ntPending" style="margin-top:10px">${renderPending()}</div>
        ${lState.strm_root_ok ? '' : `<p class="form-hint" style="color:#d33">注意：STRM_ROOT（${esc(lState.strm_root)}）当前不可访问，联动已自动跳过</p>`}
      </div>

      <div class="card">
        <div class="card-head">
          <h2>可用指令</h2>
          <p class="card-sub">企业微信 / Telegram 里直接发，任何人都需要先通过上面的白名单</p>
        </div>
        <div class="code-block" style="white-space:pre-wrap">/status   运行状态（索引、扫描、联动）
/ls       115 STRM 配置列表
/run &lt;配置名|all&gt; [inc|full|rebuild]  触发生成
/find &lt;关键词&gt;   搜索已索引的文件
/pending  联动待删除列表
/link     联动状态；/link on|off 开关「本地删 strm → 删网盘」
/notify on|off   通知开关
/help     帮助</div>
      </div>`;
  }

  function eventBox(id, label, checked) {
    return `<label class="inline" style="gap:6px;cursor:pointer">
      <input type="checkbox" id="${id}" ${checked ? 'checked' : ''}> ${esc(label)}</label>`;
  }

  function renderPending() {
    if (!pending.length) {
      return '<span class="form-hint">没有待删除项（strm 都还在，或还在宽限期内）</span>';
    }
    return `<div class="row-list">${pending.map((p) => `
      <div class="row-item">
        <div class="row-main">
          <div class="row-title">${esc(p.name || p.path)}</div>
          <div class="row-desc">${esc(p.account)} · 缺失 ${p.age}s · ${esc(p.path)}</div>
        </div>
      </div>`).join('')}</div>`;
  }

  /* ------------------------------------------------------------ 数据 */
  async function load(root) {
    try {
      const [n, l] = await Promise.all([window.API.notify(), window.API.link()]);
      nState = n;
      lState = l;
      pending = [];
      renderBody(root);
    } catch (err) {
      const box = root.querySelector('#ntBody');
      if (box) {
        box.innerHTML = `<div class="card"><div class="empty">
          <span class="empty-ico">${icon('warn', 20)}</span>
          加载失败：${esc(err.message)}</div></div>`;
      }
    }
  }

  async function loadPending(root) {
    try {
      const d = await window.API.linkPending();
      pending = d.items || [];
      const box = root.querySelector('#ntPending');
      if (box) box.innerHTML = renderPending();
      N.toast(pending.length ? '待删除 ' + pending.length + ' 个' : '没有待删除项', 'ok');
    } catch (err) { N.toast(err.message, 'err'); }
  }

  /** 打码字段原样提交会把真实密钥覆盖掉，这里直接丢掉没改的字段 */
  function stripMasked(value) {
    const v = String(value == null ? '' : value);
    return v.indexOf(MASK) >= 0 ? undefined : v;
  }

  function collect(root) {
    const val = (id) => (root.querySelector('#' + id) || {}).value || '';
    const chk = (id) => !!(root.querySelector('#' + id) || {}).checked;
    const wecom = {
      enabled: chk('wcEnabled'),
      webhook: val('wcWebhook').trim(),
      corp_id: val('wcCorp').trim(),
      agent_id: val('wcAgent').trim(),
      to_user: val('wcTo').trim() || '@all'
    };
    const secret = stripMasked(val('wcSecret'));
    const aesKey = stripMasked(val('wcAes'));
    if (secret !== undefined) wecom.secret = secret.trim();
    if (aesKey !== undefined) wecom.aes_key = aesKey.trim();
    wecom.token = val('wcToken').trim();

    const tg = {
      enabled: chk('tgEnabled'),
      chat_id: val('tgChat').trim(),
      proxy: val('tgProxy').trim(),
      allow: val('tgAllow').trim(),
      poll: chk('tgPoll')
    };
    const tgToken = stripMasked(val('tgToken'));
    if (tgToken !== undefined) tg.token = tgToken.trim();

    const webhook = {
      enabled: chk('whEnabled'),
      url: val('whUrl').trim(),
      server: val('whServer').trim(),
      template: val('whTemplate'),
      headers: val('whHeaders')
    };

    return {
      notify: {
        enabled: chk('ntEnabled'),
        events: {
          run_start: chk('evRunStart'),
          run_done: chk('evRunDone'),
          run_error: chk('evRunError'),
          link: chk('evLink'),
          scan: chk('evScan')
        },
        wecom,
        tg,
        webhook
      },
      link: {
        local_to_remote: chk('lkToRemote'),
        remote_to_local: chk('lkToLocal'),
        scrape_clean: chk('lkScrape'),
        dry_run: chk('lkDry'),
        grace_seconds: Math.max(0, Number(val('lkGrace')) || 0),
        interval: Math.max(30, Number(val('lkInterval')) || 120),
        max_ratio: Math.min(1, Math.max(0.01, (Number(val('lkRatio')) || 20) / 100)),
        min_missing: Math.max(1, Number(val('lkMin')) || 5)
      }
    };
  }

  async function save(root) {
    if (busy) return;
    const data = collect(root);
    if (data.link.local_to_remote) {
      const ok = await N.confirm('开启「本地删 strm → 删网盘」',
        '开启后，本地 strm 被删除并超过宽限期（' + data.link.grace_seconds +
        ' 秒），对应 115 网盘文件会被一起删除（进回收站）。建议先开「试运行」观察一段时间。',
        { danger: true, okText: '我已了解，开启' });
      if (!ok) return;
    }
    busy = true;
    try {
      const [n, l] = await Promise.all([
        window.API.saveNotify(data.notify),
        window.API.saveLink(data.link)
      ]);
      nState = n.status || n;
      lState = Object.assign({}, lState, { settings: l.settings });
      N.toast('配置已保存', 'ok');
      D.logs.add('info', '通知与联动配置已更新');
      renderBody(root);
    } catch (err) {
      N.toast(err.message, 'err');
    } finally {
      busy = false;
    }
  }

  function bind(root) {
    N.bindOnce(root, 'click', (e) => {
      if (e.target.closest('[data-refresh]')) { load(root); N.toast('已刷新'); return; }
      if (e.target.closest('[data-save]')) { save(root); return; }
      if (e.target.closest('[data-test-notify]')) { testNotify(root); return; }
      if (e.target.closest('[data-link-check]')) { checkLink(root, false); return; }
      if (e.target.closest('[data-link-force]')) { checkLink(root, true); return; }
      if (e.target.closest('[data-link-pending]')) { loadPending(root); return; }
      if (e.target.closest('[data-link-commit]')) { commitLink(root); return; }
    });
  }

  async function testNotify(root) {
    const msg = root.querySelector('#ntMsg');
    if (msg) msg.textContent = '发送中…';
    try {
      const d = await window.API.testNotify();
      const ch = d.channels || {};
      const detail = Object.keys(ch).map((k) => k + ': ' + (ch[k].ok ? '成功' : ch[k].detail)).join(' | ');
      if (msg) msg.textContent = d.ok ? '已发送（' + detail + '）' : (d.skipped || detail || '发送失败');
      N.toast(d.ok ? '测试消息已发送' : (d.skipped || '发送失败'), d.ok ? 'ok' : 'err');
    } catch (err) {
      if (msg) msg.textContent = err.message;
      N.toast(err.message, 'err');
    }
  }

  async function checkLink(root, force) {
    if (force) {
      const ok = await N.confirm('强制检查',
        '会忽略「缺失比例熔断」，本地 strm 不存在且过了宽限期的文件都会按配置处理' +
        '（如果开着试运行就只记录；没开就真的删网盘文件）。确定继续？',
        { danger: true, okText: '强制检查' });
      if (!ok) return;
    }
    try {
      await window.API.checkLink(!!force);
      N.toast(force ? '强制检查已提交（忽略熔断）' : '检查已提交，几秒后刷新看结果');
      setTimeout(() => load(root), 3000);
    } catch (err) { N.toast(err.message, 'err'); }
  }

  async function commitLink(root) {
    const n = lState ? (lState.pending || 0) : 0;
    const ok = await N.confirm('确认删除待删项',
      (n ? '当前有 ' + n + ' 个待删除文件，' : '会删除所有「本地 strm 已不存在」的网盘文件，') +
      '确定要立即执行吗？删除的文件会进 115 回收站，可以恢复。',
      { danger: true, okText: '立即删除' });
    if (!ok) return;
    try {
      await window.API.commitLink();
      N.toast('删除已提交');
      D.logs.add('warn', '手动执行联动删除');
      setTimeout(() => load(root), 4000);
    } catch (err) { N.toast(err.message, 'err'); }
  }

  window.ToolNotify = { page };

  // 插件自注册：卸载「通知与联动」后这份脚本不再加载，侧栏入口一起消失
  if (window.Shell && window.Shell.registerPlugin) {
    window.Shell.registerPlugin({
      id: 'notify', hash: '#/notify', name: '通知与联动',
      ico: 'shield', section: '系统', view: page
    });
  }
})();
