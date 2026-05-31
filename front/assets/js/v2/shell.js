/* ============================================================
 * AMB2API · V2 AppShell · 引导脚本
 *
 *   职责：
 *   1. 在登录后给 <body> 加 data-shell="v2" 标记，注入 Sidebar + AppBar
 *   2. 把旧 #mainTabs 的 7 个 .tab onclick 镜像到新 sidebar nav-item，
 *      点击转发到全局 switchTab(name)
 *   3. 监听旧 #mainTabs .tab.active 变化（switchTab 修改 class），
 *      同步更新 sidebar 与 AppBar Eyebrow / 标题
 *   4. 提供折叠 / 展开（持久化到 localStorage）
 *   5. 主题切换 / 登出 按钮代理到旧 .theme-toggle / .logout-btn
 * ============================================================ */
(function () {
  'use strict';

  const TAB_META = [
    { id: 'account',     label: '账户管理', eyebrow: 'Accounts',     icon: 'user',          shortcut: 'g a' },
    { id: 'config',      label: '配置管理', eyebrow: 'Config',       icon: 'settings',      shortcut: 'g c' },
    { id: 'ratelimit',   label: '速率限制', eyebrow: 'Rate Limits',  icon: 'gauge',         shortcut: 'g r' },
    { id: 'usage',       label: '使用统计', eyebrow: 'Usage',        icon: 'line-chart',    shortcut: 'g u' },
    { id: 'playground',  label: '操练场',   eyebrow: 'Playground',   icon: 'terminal',      shortcut: 'g p' },
    { id: 'performance', label: '性能分析', eyebrow: 'Performance',  icon: 'activity',      shortcut: 'g f' },
    { id: 'logs',        label: '实时日志', eyebrow: 'Logs',         icon: 'file-text',     shortcut: 'g l' },
  ];

  const SIDEBAR_KEY = 'amb2api.v2.sidebar';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function ensureSvg(name, size) {
    if (window.icons && typeof window.icons.create === 'function') {
      return window.icons.create(name, { size: size || 18 });
    }
    const span = document.createElement('span');
    span.setAttribute('data-icon', name);
    return span;
  }

  function syncThemeIcon(theme) {
    const host = $('.v2-theme-icon');
    if (!host) return;
    host.innerHTML = '';
    host.appendChild(ensureSvg(theme === 'dark' ? 'sun' : 'moon', 16));
  }

  function buildSidebar() {
    if ($('.v2-sidebar')) return;
    const aside = document.createElement('aside');
    aside.className = 'v2-sidebar';
    aside.setAttribute('aria-label', '主导航');

    aside.innerHTML = `
      <div class="v2-sidebar__brand">
        <span class="v2-sidebar__brand-logo" aria-hidden="true">
          <svg class="v2-brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" focusable="false">
            <path d="M9.5 7 5 12l4.5 5" />
            <path d="M14.5 7 19 12l-4.5 5" />
          </svg>
        </span>
        <span class="v2-sidebar__brand-text">
          <span class="v2-sidebar__brand-name">AMB2API</span>
          <span class="v2-sidebar__brand-tag">Console</span>
        </span>
      </div>
      <nav class="v2-sidebar__nav">
        <div class="v2-sidebar__section-label">主菜单</div>
        <div class="v2-sidebar__nav-list" id="v2NavList"></div>
      </nav>
      <div class="v2-sidebar__footer">
        <button type="button" class="v2-sidebar__collapse" id="v2SidebarCollapse" aria-label="折叠侧栏">
        </button>
      </div>
    `;
    document.body.appendChild(aside);

    const list = $('#v2NavList', aside);
    TAB_META.forEach(meta => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'v2-nav-item';
      btn.setAttribute('data-tab', meta.id);
      btn.setAttribute('data-label', meta.label);
      btn.setAttribute('title', meta.label);
      btn.innerHTML = `
        <span class="v2-nav-item__icon"></span>
        <span class="v2-nav-item__label">${meta.label}</span>
        <span class="v2-nav-item__shortcut">${meta.shortcut}</span>
      `;
      btn.querySelector('.v2-nav-item__icon').appendChild(ensureSvg(meta.icon, 18));
      btn.addEventListener('click', () => {
        if (typeof window.switchTab === 'function') {
          window.switchTab(meta.id);
        }
      });
      list.appendChild(btn);
    });

    // 折叠切换
    const collapseBtn = $('#v2SidebarCollapse', aside);
    collapseBtn.appendChild(ensureSvg('chevron-left', 14));
    collapseBtn.addEventListener('click', toggleSidebar);
  }

  function buildAppBar() {
    if ($('.v2-appbar')) return;
    const bar = document.createElement('header');
    bar.className = 'v2-appbar';
    bar.innerHTML = `
      <div class="v2-appbar__crumb">
        <span class="v2-appbar__eyebrow" id="v2AppbarEyebrow">CONSOLE</span>
        <span class="v2-appbar__sep">/</span>
        <span class="v2-appbar__title" id="v2AppbarTitle">控制台</span>
      </div>
      <div class="v2-appbar__actions">
        <button type="button" class="v2-appbar__cmdk" id="v2OpenCmdk" title="命令面板（Cmd/Ctrl+K）" aria-label="命令面板">
          <span class="v2-cmdk-icon"></span>
          <span class="v2-appbar__cmdk-text">搜索 / 命令…</span>
          <span class="v2-appbar__cmdk-kbd">⌘K</span>
        </button>
        <button type="button" class="v2-appbar__btn v2-appbar__btn--icon" id="v2ThemeToggle" title="切换主题" aria-label="切换主题">
          <span class="v2-theme-icon"></span>
        </button>
        <button type="button" class="v2-appbar__btn" id="v2LogoutBtn" title="退出登录">
          <span class="v2-logout-icon"></span>
          <span>退出</span>
        </button>
      </div>
    `;
    document.body.appendChild(bar);

    bar.querySelector('.v2-cmdk-icon').appendChild(ensureSvg('search', 14));
    bar.querySelector('.v2-theme-icon').appendChild(ensureSvg('moon', 16));
    bar.querySelector('.v2-logout-icon').appendChild(ensureSvg('log-out', 14));
    syncThemeIcon(document.documentElement.getAttribute('data-theme') || 'light');

    $('#v2ThemeToggle', bar).addEventListener('click', (e) => {
      // 优先调 window.theme.toggle —— 旧 .theme-toggle 在 V2 模式下被隐藏，
      // 浏览器对 display:none 元素的 programmatic click 行为不一致。
      if (window.theme && typeof window.theme.toggle === 'function') {
        window.theme.toggle(e);
      } else {
        const legacy = document.querySelector('.theme-toggle');
        if (legacy) legacy.click();
      }
    });
    $('#v2LogoutBtn', bar).addEventListener('click', () => {
      if (typeof window.logout === 'function') window.logout();
    });
    $('#v2OpenCmdk', bar).addEventListener('click', () => {
      // 占位：命令面板未实现
      if (window.toast) {
        window.toast({ kind: 'info', title: '命令面板', description: 'Cmd+K 命令面板即将上线（V2 阶段 11）' });
      }
    });
  }

  function syncActiveFromLegacy() {
    const active = $('#mainTabs .tab.active');
    if (!active) return;
    const onclick = active.getAttribute('onclick') || '';
    const m = onclick.match(/switchTab\('([^']+)'\)/);
    if (!m) return;
    const tabId = m[1];
    const meta = TAB_META.find(t => t.id === tabId);

    $all('.v2-nav-item').forEach(item => {
      item.setAttribute('data-active', item.getAttribute('data-tab') === tabId ? 'true' : 'false');
    });
    if (meta) {
      const eb = $('#v2AppbarEyebrow');
      const ti = $('#v2AppbarTitle');
      if (eb) eb.textContent = meta.eyebrow;
      if (ti) ti.textContent = meta.label;
    }
  }

  function observeLegacyTabs() {
    const host = $('#mainTabs');
    if (!host) return;
    const obs = new MutationObserver(syncActiveFromLegacy);
    obs.observe(host, {
      attributes: true,
      attributeFilter: ['class'],
      subtree: true,
    });
    syncActiveFromLegacy();
  }

  function applySidebarPref() {
    let pref = 'expanded';
    try { pref = localStorage.getItem(SIDEBAR_KEY) || 'expanded'; } catch (_) {}
    document.body.setAttribute('data-sidebar', pref);
  }

  function toggleSidebar() {
    const cur = document.body.getAttribute('data-sidebar') || 'expanded';
    const next = cur === 'mini' ? 'expanded' : 'mini';
    document.body.setAttribute('data-sidebar', next);
    try { localStorage.setItem(SIDEBAR_KEY, next); } catch (_) {}
  }

  function activateShell() {
    document.body.setAttribute('data-shell', 'v2');
    applySidebarPref();
    buildSidebar();
    buildAppBar();
    observeLegacyTabs();
  }
  function deactivateShell() {
    document.body.removeAttribute('data-shell');
    document.body.removeAttribute('data-sidebar');
    const a = $('.v2-sidebar'); if (a) a.remove();
    const b = $('.v2-appbar'); if (b) b.remove();
  }

  function watchMainSection() {
    const main = $('#mainSection');
    if (!main) return;
    const evaluate = () => {
      const visible = !main.classList.contains('hidden');
      if (visible) activateShell();
      else deactivateShell();
    };
    const obs = new MutationObserver(evaluate);
    obs.observe(main, { attributes: true, attributeFilter: ['class'] });
    evaluate();
  }

  function bindHotkeys() {
    let lastG = 0;
    document.addEventListener('keydown', (e) => {
      // 输入态忽略
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

      // Cmd/Ctrl + B 折叠侧栏
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        if (document.body.getAttribute('data-shell') === 'v2') toggleSidebar();
        return;
      }

      // g x 跳转
      if (e.key === 'g') { lastG = Date.now(); return; }
      if (lastG && Date.now() - lastG < 1200) {
        const map = { a: 'account', c: 'config', r: 'ratelimit', u: 'usage', p: 'playground', f: 'performance', l: 'logs' };
        const tab = map[e.key.toLowerCase()];
        if (tab && typeof window.switchTab === 'function') {
          e.preventDefault();
          window.switchTab(tab);
        }
        lastG = 0;
      }
    });
  }

  window.addEventListener('themechange', (event) => {
    syncThemeIcon((event.detail && event.detail.theme) || document.documentElement.getAttribute('data-theme') || 'light');
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      watchMainSection();
      bindHotkeys();
    });
  } else {
    watchMainSection();
    bindHotkeys();
  }
})();
