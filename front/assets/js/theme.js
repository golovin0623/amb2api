/* ============================================================
 * AMB2API Theme manager
 * 1. 与旧 [data-theme] 切换逻辑兼容
 * 2. 提供 View Transitions 圆形扩散动画
 * 3. 持久化到 localStorage
 * 兼容旧 themeIcon / themeText 元素
 * ============================================================ */
(function () {
  'use strict';

  const STORAGE_KEY = 'amb2api-theme';

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }

  function setTheme(theme, opts) {
    opts = opts || {};
    const root = document.documentElement;
    const before = currentTheme();
    if (before === theme) return;

    const apply = () => {
      root.setAttribute('data-theme', theme);
      try {
        localStorage.setItem(STORAGE_KEY, theme);
        localStorage.setItem('theme', theme);
      } catch (e) {}
      updateLegacyToggle(theme);
      window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: theme } }));
    };

    // View Transitions
    if (document.startViewTransition && !opts.noAnim) {
      const x = (opts.x !== undefined) ? opts.x : window.innerWidth - 32;
      const y = (opts.y !== undefined) ? opts.y : 32;
      root.style.setProperty('--theme-x', x + 'px');
      root.style.setProperty('--theme-y', y + 'px');
      const transition = document.startViewTransition(() => apply());
      // wait — but ignore promise rejections
      transition.finished.catch(() => {});
    } else {
      apply();
    }
  }

  function toggleTheme(evt) {
    let x, y;
    if (evt && (evt.clientX !== undefined)) { x = evt.clientX; y = evt.clientY; }
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark', { x, y });
  }

  function updateLegacyToggle(theme) {
    const isDark = theme === 'dark';
    const iconEl = document.getElementById('themeIcon');
    const textEl = document.getElementById('themeText');
    if (textEl) textEl.textContent = isDark ? '亮色' : '暗黑';
    if (iconEl) {
      iconEl.innerHTML = '';
      if (window.icons) {
        iconEl.appendChild(window.icons.create(isDark ? 'sun' : 'moon', { size: 16 }));
      } else {
        iconEl.textContent = isDark ? '☀' : '☾';
      }
    }
  }

  function init() {
    let stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY) || localStorage.getItem('theme'); } catch (e) {}
    const initial = stored || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', initial);
    try {
      localStorage.setItem(STORAGE_KEY, initial);
      localStorage.setItem('theme', initial);
    } catch (e) {}
    updateLegacyToggle(initial);

    // 兼容旧 .theme-toggle 元素
    document.addEventListener('click', (evt) => {
      if (evt.defaultPrevented) return;
      const tgt = evt.target.closest('.theme-toggle, [data-action="toggle-theme"]');
      if (tgt) {
        evt.preventDefault();
        toggleTheme(evt);
      }
    });

    // 监听系统偏好变化
    if (window.matchMedia) {
      const mql = window.matchMedia('(prefers-color-scheme: dark)');
      const handler = (e) => {
        // 仅在用户从未手动选择时跟随系统
        try {
          if (localStorage.getItem(STORAGE_KEY)) return;
        } catch (err) { /* ignore */ }
        setTheme(e.matches ? 'dark' : 'light', { noAnim: true });
      };
      try { mql.addEventListener('change', handler); }
      catch (e) { mql.addListener && mql.addListener(handler); }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.theme = {
    set: setTheme,
    toggle: toggleTheme,
    current: currentTheme,
  };
})();
