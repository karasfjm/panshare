/* ==========================================================================
   main.js · 应用外壳（路由 / 渲染 / 重新加载）+ 启动
   ========================================================================== */
(function () {
  'use strict';
  const N = window.NS, D = window.Data;

  const App = {
    init() {
      N.applyTheme(N.storeGet(N.K.theme, 'light'));
      this.bind();
      window.Shell.bindShell();
      window.Shell.boot();
    },

    bind() {
      // 登录页背景（纯 CSS 渐变，不用外部图片）
      const bg = N.$('#loginBg');
      if (bg) {
        bg.innerHTML = Array.from({ length: 12 }, (_, i) =>
          `<span class="lb-tile" style="--i:${i}"></span>`).join('');
      }
      // 顶栏 orb 图标
      const orbIcon = { logs: 'article', theme: 'sun', more: 'dots' };
      N.$$('.orb').forEach((b) => {
        b.innerHTML = N.icon(orbIcon[b.getAttribute('data-panel')] || 'dots', 16);
      });
      N.$('#logRefresh').innerHTML = N.icon('refresh', 15);
      N.$('#logClear').innerHTML = N.icon('trash', 15);
      N.$$('[data-close-drawer]').forEach((b) => { b.innerHTML = N.icon('x', 15); });
      N.$('#togglePw').innerHTML = N.icon('eye', 15);
      N.$('#navToggle').innerHTML = N.icon('list', 16);

      N.$$('.pop-ico').forEach((el, i) => {
        el.innerHTML = N.icon(['chart', 'refresh', 'zap', 'logout'][i] || 'dots', 15);
      });
    },

    /** 当前路由重绘 */
    rerender() {
      window.Shell.render();
    },

    /** 重新从服务端拉数据再重绘（保存类操作后用） */
    async reload() {
      try {
        await D.load();
      } catch (e) { /* 忽略：保留旧数据 */ }
      window.Shell.render();
    },

    /** 未登录 / 会话过期：回到登录页 */
    onUnauthorized() {
      N.modal.closeAll();
      window.Shell.showLogin('登录已失效，请重新登录');
    }
  };

  window.App = App;

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    if (!window.App) {
      console.error('App 未加载');
      return;
    }
    window.App.init();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
