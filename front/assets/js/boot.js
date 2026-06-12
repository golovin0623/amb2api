/* ============================================================
 * AMB2API Boot
 * 在所有模块就绪后做最终装饰：
 * 1. 替换旧主题按钮的 emoji 为 SVG 图标
 * 2. 替换登录页 emoji logo 为渐变 SVG
 * 3. 替换 fixed-header 中的标签按钮装入 SVG 前缀
 * 4. 给所有 .modal-close 按钮装上 X 图标
 * ============================================================ */
(function () {
  'use strict';

  const TAB_ICON_MAP = {
    account:     'user-circle',
    config:      'sliders-horizontal',
    ratelimit:   'gauge',
    usage:       'bar-chart-2',
    playground:  'terminal',
    performance: 'activity',
    logs:        'file-text',
    system:      'sliders',
  };

  function decorateTabs() {
    const tabs = document.querySelectorAll('#mainTabs .tab');
    tabs.forEach((tab) => {
      const handler = tab.getAttribute('onclick') || '';
      const m = handler.match(/switchTab\(['"](\w+)['"]\)/);
      const name = m && m[1];
      const icon = name && TAB_ICON_MAP[name];
      if (icon && !tab.querySelector('svg')) {
        tab.setAttribute('data-icon', icon);
        if (window.icons) window.icons.render(tab.parentNode);
      }
    });
  }

  function decorateModalCloses() {
    document.querySelectorAll('.modal-close').forEach((btn) => {
      if (btn.querySelector('svg')) return;
      // 旧标记内容是 &times;，替换为 SVG
      btn.textContent = '';
      if (window.icons) btn.appendChild(window.icons.create('x', { size: 18 }));
    });
  }

  function decorateLoginLogo() {
    /*
     * 登录页"层林尽染"原版要求保留：
     *   - .login-logo 内的 ⛵️ 帆船 emoji（蓝白渐变方框）
     *   - .login-subtitle .lock-icon 内的 ⛩️ 鸟居 emoji
     *   - .login-input-wrapper .input-icon 内的 👁️‍🗨️ 眼睛 emoji
     * 这些 emoji 是用户偏爱的视觉标识，V1 重构曾把它们替换为 SVG，
     * V2 阶段恢复原版体验，因此本函数置为 no-op。
     * 如果将来需要重新启用，恢复这段函数体即可。
     */
  }

  function setPasswordVisibility(input, visible, control) {
    input.type = visible ? 'text' : 'password';
    input.dataset.visible = visible ? '1' : '0';
    if (control) {
      control.setAttribute('aria-pressed', visible ? 'true' : 'false');
      control.setAttribute('title', visible ? '隐藏密码' : '显示密码');
      const svg = control.querySelector('svg');
      if (svg && window.icons) {
        svg.replaceWith(window.icons.create(visible ? 'eye-off' : 'eye', { size: 16 }));
      }
    }
  }

  function bindPasswordToggle(control, input) {
    if (!control || !input || control.dataset.passwordToggleBound) return;
    control.dataset.passwordToggleBound = '1';
    control.setAttribute('role', 'button');
    control.setAttribute('tabindex', '0');
    control.setAttribute('aria-label', '切换密码可见性');
    control.setAttribute('aria-pressed', input.type === 'text' ? 'true' : 'false');
    control.setAttribute('title', input.type === 'text' ? '隐藏密码' : '显示密码');
    const toggle = (event) => {
      if (event) event.preventDefault();
      setPasswordVisibility(input, input.type === 'password', control);
      input.focus({ preventScroll: true });
    };
    control.addEventListener('click', toggle);
    control.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') toggle(event);
    });
  }

  function decoratePasswordToggles() {
    document.querySelectorAll('.login-input-wrapper').forEach((wrapper) => {
      const input = wrapper.querySelector('input[type="password"], input[type="text"]');
      const control = wrapper.querySelector('.input-icon');
      bindPasswordToggle(control, input);
    });

    [
      'configApiPassword',
      'configPanelPassword',
      'configPassword',
      'accountPassword',
      'newAccountPassword',
    ].forEach((id) => {
      const input = document.getElementById(id);
      if (!input || input.dataset.passwordToggleReady) return;
      const group = input.closest('.form-group');
      if (!group) return;
      input.dataset.passwordToggleReady = '1';
      group.classList.add('has-password-toggle');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'password-toggle';
      btn.setAttribute('aria-label', '切换密码可见性');
      if (window.icons) btn.appendChild(window.icons.create('eye', { size: 16 }));
      else btn.textContent = '👁️';
      input.insertAdjacentElement('afterend', btn);
      bindPasswordToggle(btn, input);
    });
  }

  function decorateLogoutBtn() {
    const btns = document.querySelectorAll('.logout-btn');
    btns.forEach((btn) => {
      if (btn.querySelector('svg')) return;
      btn.setAttribute('data-icon', 'log-out');
      if (window.icons) window.icons.render(btn.parentNode);
    });
  }

  function decoratePaginationArrows() {
    document.querySelectorAll('.pagination-btn').forEach((btn) => {
      if (btn.querySelector('svg')) return;
      const txt = btn.textContent.trim();
      if (txt === '<' || txt === '‹' || txt === '&lt;') {
        btn.textContent = '';
        if (window.icons) btn.appendChild(window.icons.create('chevron-left', { size: 14 }));
      } else if (txt === '>' || txt === '›' || txt === '&gt;') {
        btn.textContent = '';
        if (window.icons) btn.appendChild(window.icons.create('chevron-right', { size: 14 }));
      }
    });
  }

  function decorateLogButtons() {
    const map = {
      'log-btn-connect':    'unplug',
      'log-btn-disconnect': 'wifi-off',
      'log-btn-download':   'download',
      'log-btn-clear':      'eraser',
    };
    Object.keys(map).forEach((cls) => {
      document.querySelectorAll('.' + cls).forEach((btn) => {
        const iconBox = btn.querySelector('.btn-icon');
        if (iconBox && !iconBox.querySelector('svg')) {
          iconBox.textContent = '';
          if (window.icons) iconBox.appendChild(window.icons.create(map[cls], { size: 16 }));
        }
      });
    });
    // 兼容写法：log-btn-connect 等情况下没有 .btn-icon span
    document.querySelectorAll('.log-btn:not([data-icon])').forEach((btn) => {
      const cls = Object.keys(map).find((c) => btn.classList.contains(c));
      if (cls && !btn.querySelector('svg')) {
        btn.setAttribute('data-icon', map[cls]);
      }
    });
  }

  function decorateConfigFloatingBtns() {
    const refresh = document.querySelector('.config-floating-btn.refresh');
    const save = document.querySelector('.config-floating-btn.save');
    if (refresh && !refresh.querySelector('svg')) {
      refresh.textContent = '';
      if (window.icons) refresh.appendChild(window.icons.create('refresh-cw', { size: 16 }));
      const span = document.createElement('span'); span.textContent = '重载'; refresh.appendChild(span);
    }
    if (save && !save.querySelector('svg')) {
      save.textContent = '';
      if (window.icons) save.appendChild(window.icons.create('save', { size: 16 }));
      const span = document.createElement('span'); span.textContent = '保存'; save.appendChild(span);
    }
  }

  /* 替换 emoji 标题为 SVG */
  const EMOJI_ICON_MAP = [
    { re: /^💬\s*/, icon: 'messages-square' },
    { re: /^🔧\s*/, icon: 'wrench' },
    { re: /^🛠️?\s*/, icon: 'wrench' },
    { re: /^📋\s*/, icon: 'clipboard-list' },
    { re: /^📊\s*/, icon: 'bar-chart-2' },
    { re: /^📈\s*/, icon: 'line-chart' },
    { re: /^⚙️?\s*/, icon: 'settings' },
    { re: /^🔍\s*/, icon: 'search' },
    { re: /^📥\s*/, icon: 'download' },
    { re: /^📤\s*/, icon: 'upload' },
    { re: /^📝\s*/, icon: 'edit-3' },
    { re: /^🗑️?\s*/, icon: 'trash-2' },
    { re: /^🔑\s*/, icon: 'key' },
    { re: /^📦\s*/, icon: 'package' },
    { re: /^🎯\s*/, icon: 'circle-dot' },
    { re: /^✨\s*/, icon: 'sparkles' },
    { re: /^🧪\s*/, icon: 'sparkles' },
    { re: /^🚀\s*/, icon: 'rocket' },
    { re: /^📡\s*/, icon: 'activity' },
    { re: /^🔄\s*/, icon: 'refresh-cw' },
    { re: /^💡\s*/, icon: 'info' },
    { re: /^📁\s*/, icon: 'folder' },
    { re: /^🔒\s*/, icon: 'lock' },
    { re: /^🔓\s*/, icon: 'unlock' },
    { re: /^🌍\s*/, icon: 'globe' },
    { re: /^💻\s*/, icon: 'monitor' },
  ];
  function decorateEmojiTitles() {
    if (!window.icons) return;
    /* 处理常见容器标题 */
    const sels = [
      '.playground-param-title',
      '#playgroundTab #requestPreviewSection > div > span:first-child',
      '#configTab .config-group h4',
      '#performanceTab .config-group h4',
      '#ratelimitTab .config-group h4',
      '#usageTab .config-group h4',
      '#playgroundTab .config-group h4',
      '#logsTab .config-group h4',
      '#accountTab .config-group h4',
    ];
    document.querySelectorAll(sels.join(',')).forEach((el) => {
      if (el.dataset.emojiDecorated) return;
      const text = el.textContent.trim();
      if (!text) return;
      for (const { re, icon } of EMOJI_ICON_MAP) {
        if (re.test(text)) {
          const cleaned = text.replace(re, '').trim();
          el.textContent = '';
          const svg = window.icons.create(icon, { size: 14 });
          if (svg) {
            svg.style.opacity = '0.7';
            svg.style.flexShrink = '0';
            el.appendChild(svg);
          }
          el.appendChild(document.createTextNode(' ' + cleaned));
          el.style.display = 'inline-flex';
          el.style.alignItems = 'center';
          el.style.gap = '8px';
          el.dataset.emojiDecorated = '1';
          break;
        }
      }
    });
  }

  /* 在每个 tab-content > h3 上方注入 eyebrow */
  const TAB_EYEBROW = {
    accountTab:    'ACCOUNT · KEYS',
    configTab:     'CONFIGURATION',
    ratelimitTab:  'RATE LIMITS · QUOTA',
    usageTab:      'USAGE · ANALYTICS',
    playgroundTab: 'PLAYGROUND · LLM TEST',
    performanceTab:'PERFORMANCE · TRACE',
    logsTab:       'LIVE LOGS · STREAM',
    uploadTab:     'UPLOAD',
    manageTab:     'MANAGE',
    systemTab:     'SYSTEM · SETTINGS',
  };
  function decorateTabHeaders() {
    Object.keys(TAB_EYEBROW).forEach((tabId) => {
      const tab = document.getElementById(tabId);
      if (!tab) return;
      const h3 = tab.querySelector(':scope > h3');
      if (!h3 || h3.dataset.eyebrowAdded) return;
      const eyebrow = document.createElement('div');
      eyebrow.className = 'ac-eyebrow';
      eyebrow.textContent = TAB_EYEBROW[tabId];
      h3.parentNode.insertBefore(eyebrow, h3);
      h3.dataset.eyebrowAdded = '1';
    });
  }

  /* 在 V2 + mobile (<=1024px) 下，把 .theme-toggle 节点从 <body> 提到 .header-title-row 内、
     紧贴 .logout-btn 左侧；切回桌面时还原回 <body>。
     这是为了让主题切换在 mobile 下「永远固定在退出按钮旁边」，不再 fixed 浮动。 */
  function relocateThemeToggle() {
    const theme = document.querySelector('.theme-toggle');
    const row = document.querySelector('.header-title-row');
    if (!theme || !row) return;

    const isMobile = window.matchMedia('(max-width: 1024px)').matches;
    const isV2 = document.body.getAttribute('data-shell') === 'v2';

    if (isMobile && isV2) {
      const logoutBtn = row.querySelector('.logout-btn');
      if (theme.parentElement !== row) {
        if (logoutBtn) row.insertBefore(theme, logoutBtn);
        else row.appendChild(theme);
        // 抹掉旧拖拽留下的 inline top/right/bottom/left，让 CSS 接管 static 布局
        ['top', 'right', 'bottom', 'left'].forEach(p => { theme.style[p] = ''; });
      }
      theme.dataset.docked = 'header';
    } else {
      if (theme.parentElement !== document.body) {
        document.body.insertBefore(theme, document.body.firstChild);
      }
      theme.dataset.docked = '';
    }
  }
  // 监听 V2 shell 切换 (登录后会从 hidden -> v2 mode) + 视口尺寸变化
  let _themeRelocateBound = false;
  function bindThemeRelocateListeners() {
    if (_themeRelocateBound) return;
    _themeRelocateBound = true;
    window.addEventListener('resize', relocateThemeToggle);
    new MutationObserver(relocateThemeToggle).observe(document.body, {
      attributes: true,
      attributeFilter: ['data-shell'],
    });
  }

  function decorateAll() {
    try { decorateTabs(); } catch (e) { console.warn('[boot] decorateTabs', e); }
    try { decorateModalCloses(); } catch (e) { console.warn('[boot] decorateModalCloses', e); }
    try { decorateLoginLogo(); } catch (e) { console.warn('[boot] decorateLoginLogo', e); }
    try { decorateLogoutBtn(); } catch (e) { console.warn('[boot] decorateLogoutBtn', e); }
    try { decoratePaginationArrows(); } catch (e) { console.warn('[boot] decoratePaginationArrows', e); }
    try { decorateLogButtons(); } catch (e) { console.warn('[boot] decorateLogButtons', e); }
    try { decorateConfigFloatingBtns(); } catch (e) { console.warn('[boot] decorateConfigFloatingBtns', e); }
    try { decorateEmojiTitles(); } catch (e) { console.warn('[boot] decorateEmojiTitles', e); }
    try { decorateTabHeaders(); } catch (e) { console.warn('[boot] decorateTabHeaders', e); }
    try { decoratePasswordToggles(); } catch (e) { console.warn('[boot] decoratePasswordToggles', e); }
    try { relocateThemeToggle(); } catch (e) { console.warn('[boot] relocateThemeToggle', e); }
    try { bindThemeRelocateListeners(); } catch (e) { console.warn('[boot] bindThemeRelocateListeners', e); }
  }

  function start() {
    decorateAll();
    // 业务 JS 可能在初次切换 tab / 打开 modal 时插入新元素，再装饰一次
    setTimeout(decorateAll, 200);
    setTimeout(decorateAll, 800);
    setTimeout(decorateAll, 2000);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start);
  }

  // 暴露 hook 供业务 JS 调用
  window.uiBoot = { decorateAll };
})();
