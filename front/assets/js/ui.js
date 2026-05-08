/* ============================================================
 * AMB2API UI Helpers
 * Toast / 全局确认框 / Tooltip / Skeleton
 * 兼容旧 alert() 与 showStatus()
 * ============================================================ */
(function () {
  'use strict';

  /* -------- Toast -------- */
  let toastRegion = null;
  function ensureToastRegion() {
    if (toastRegion && document.body.contains(toastRegion)) return toastRegion;
    toastRegion = document.querySelector('.toast-region');
    if (!toastRegion) {
      toastRegion = document.createElement('div');
      toastRegion.className = 'toast-region';
      toastRegion.setAttribute('role', 'region');
      toastRegion.setAttribute('aria-label', '通知');
      document.body.appendChild(toastRegion);
    }
    return toastRegion;
  }

  /**
   * 显示 toast。
   * opts: { kind: 'success'|'error'|'warning'|'info'|'danger',
   *         title, description, duration (ms, default 4000),
   *         icon (optional override), persistent (boolean) }
   */
  function toast(opts) {
    opts = opts || {};
    const region = ensureToastRegion();
    const kind = opts.kind || 'info';
    const node = document.createElement('div');
    node.className = 'toast toast--' + kind;
    node.setAttribute('role', kind === 'error' || kind === 'danger' ? 'alert' : 'status');

    const iconName = opts.icon || (
      kind === 'success' ? 'check-circle' :
      kind === 'warning' ? 'alert-triangle' :
      kind === 'error' || kind === 'danger' ? 'x-circle' :
      'info'
    );
    const iconBox = document.createElement('span');
    iconBox.className = 'toast__icon';
    iconBox.appendChild(window.icons.create(iconName, { size: 18 }));

    const body = document.createElement('div');
    body.className = 'toast__body';
    if (opts.title) {
      const t = document.createElement('div');
      t.className = 'toast__title';
      t.textContent = opts.title;
      body.appendChild(t);
    }
    if (opts.description) {
      const d = document.createElement('div');
      d.className = 'toast__desc';
      d.textContent = opts.description;
      body.appendChild(d);
    }
    if (!opts.title && !opts.description) {
      const d = document.createElement('div');
      d.className = 'toast__title';
      d.textContent = String(opts.message || '');
      body.appendChild(d);
    }

    const close = document.createElement('button');
    close.className = 'toast__close';
    close.setAttribute('aria-label', '关闭');
    close.appendChild(window.icons.create('x', { size: 14 }));

    function dismiss() {
      node.classList.add('toast--out');
      setTimeout(() => node.remove(), 250);
    }
    close.addEventListener('click', dismiss);

    node.appendChild(iconBox);
    node.appendChild(body);
    node.appendChild(close);
    region.appendChild(node);

    if (!opts.persistent) {
      const ms = typeof opts.duration === 'number' ? opts.duration : 4000;
      setTimeout(dismiss, Math.max(800, ms));
    }
    return { dismiss };
  }

  /* -------- 兼容旧 alert / showStatus -------- */
  function inferKind(text) {
    const t = String(text || '').toLowerCase();
    if (t.includes('成功') || t.includes('success') || t.includes('保存')) return 'success';
    if (t.includes('失败') || t.includes('错误') || t.includes('error') || t.includes('fail')) return 'error';
    if (t.includes('警告') || t.includes('warning') || t.includes('注意')) return 'warning';
    return 'info';
  }

  /* showStatus 兼容：原签名 showStatus(message, type)
   * type 可能是 'info' | 'success' | 'error' | 'warning' */
  if (!window.__originalShowStatus) {
    const origShowStatus = window.showStatus;
    window.__originalShowStatus = origShowStatus;
    window.showStatus = function (message, type) {
      try {
        toast({
          kind: type || inferKind(message),
          title: String(message || ''),
          duration: 4500,
        });
      } catch (e) {
        if (typeof origShowStatus === 'function') {
          origShowStatus(message, type);
        } else {
          console.log('[status]', type, message);
        }
      }
    };
  }

  /* -------- 简单 confirm 替换（保留原生 confirm 作为兜底） -------- */
  // 此版本暂不重写 window.confirm，因业务逻辑同步依赖返回值。
  // 后续阶段提供 ui.confirm() 异步版本。

  /* -------- 暴露 -------- */
  window.ui = window.ui || {};
  window.ui.toast = toast;
  window.toast = toast;
})();
