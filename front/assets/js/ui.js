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
    toastRegion = document.querySelector('.ac-toast-region, .toast-region');
    if (!toastRegion) {
      toastRegion = document.createElement('div');
      toastRegion.className = 'ac-toast-region toast-region';
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
    node.className = 'ac-toast toast toast--' + kind + ' ac-toast--' + kind;
    node.setAttribute('role', kind === 'error' || kind === 'danger' ? 'alert' : 'status');

    const iconName = opts.icon || (
      kind === 'success' ? 'check-circle' :
      kind === 'warning' ? 'alert-triangle' :
      kind === 'error' || kind === 'danger' ? 'x-circle' :
      'info'
    );
    const iconBox = document.createElement('span');
    iconBox.className = 'ac-toast__icon toast__icon';
    if (window.icons) iconBox.appendChild(window.icons.create(iconName, { size: 18 }));

    const body = document.createElement('div');
    body.className = 'ac-toast__body toast__body';
    if (opts.title) {
      const t = document.createElement('div');
      t.className = 'ac-toast__title toast__title';
      t.textContent = opts.title;
      body.appendChild(t);
    }
    if (opts.description) {
      const d = document.createElement('div');
      d.className = 'ac-toast__desc toast__desc';
      d.textContent = opts.description;
      body.appendChild(d);
    }
    if (!opts.title && !opts.description) {
      const d = document.createElement('div');
      d.className = 'ac-toast__title toast__title';
      d.textContent = String(opts.message || '');
      body.appendChild(d);
    }

    const close = document.createElement('button');
    close.className = 'ac-toast__close toast__close';
    close.setAttribute('aria-label', '关闭');
    if (window.icons) close.appendChild(window.icons.create('x', { size: 14 }));
    else close.textContent = '×';

    function dismiss() {
      node.classList.add('ac-toast--out', 'toast--out');
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

  const notify = {
    success(message, options) { return toast({ ...(options || {}), kind: 'success', title: String(message || '') }); },
    error(message, options) { return toast({ ...(options || {}), kind: 'error', title: String(message || ''), duration: 5200 }); },
    warn(message, options) { return toast({ ...(options || {}), kind: 'warning', title: String(message || '') }); },
    warning(message, options) { return this.warn(message, options); },
    info(message, options) { return toast({ ...(options || {}), kind: 'info', title: String(message || '') }); },
  };

  let lastInteractionTarget = null;
  function rememberInteractionTarget(event) {
    const target = event.target;
    if (target instanceof HTMLElement) {
      lastInteractionTarget = target.closest('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])') || target;
    }
  }
  document.addEventListener('pointerdown', rememberInteractionTarget, true);
  document.addEventListener('click', rememberInteractionTarget, true);

  function splitDialogBody(body) {
    return String(body || '')
      .split(/\n{2,}|\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function confirmDialog(options) {
    options = typeof options === 'string' ? { body: options } : (options || {});
    return new Promise((resolve) => {
      const active = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
        ? document.activeElement
        : null;
      const previousFocus = active || (lastInteractionTarget && document.contains(lastInteractionTarget) ? lastInteractionTarget : null);
      const modal = document.createElement('div');
      modal.className = 'modal ac-confirm-modal';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      modal.style.display = 'flex';

      const dialogId = 'confirm-title-' + Math.random().toString(36).slice(2);
      const confirmText = options.confirmText || '确定';
      const cancelText = options.cancelText || '取消';
      const danger = !!options.danger;
      const required = options.requireInput ? String(options.requireInput) : '';

      const content = document.createElement('div');
      content.className = 'modal-content ac-confirm-content';
      content.innerHTML = `
        <div class="modal-header">
          <h3 class="modal-title" id="${dialogId}"></h3>
          <button type="button" class="modal-close" aria-label="关闭"></button>
        </div>
        <div class="modal-body">
          <div class="ac-confirm-copy"></div>
        </div>
        <div class="modal-footer ac-confirm-footer">
          <button type="button" class="btn btn-outline-secondary ac-confirm-cancel"></button>
          <button type="button" class="btn ac-confirm-ok"></button>
        </div>
      `;
      modal.setAttribute('aria-labelledby', dialogId);
      modal.appendChild(content);

      const titleEl = content.querySelector('.modal-title');
      const closeBtn = content.querySelector('.modal-close');
      const bodyEl = content.querySelector('.ac-confirm-copy');
      const okBtn = content.querySelector('.ac-confirm-ok');
      const cancelBtn = content.querySelector('.ac-confirm-cancel');
      titleEl.textContent = options.title || (danger ? '确认危险操作' : '确认操作');
      cancelBtn.textContent = cancelText;
      okBtn.textContent = confirmText;
      if (danger) {
        okBtn.classList.add('ac-confirm-ok--danger');
      }
      if (window.icons) closeBtn.appendChild(window.icons.create('x', { size: 18 }));
      else closeBtn.textContent = '×';

      splitDialogBody(options.body || options.message || '确定继续吗？').forEach((line) => {
        const p = document.createElement('p');
        p.textContent = line;
        bodyEl.appendChild(p);
      });

      let requiredInput = null;
      if (required) {
        const label = document.createElement('label');
        label.className = 'ac-confirm-require';
        label.innerHTML = `<span>请输入 <strong></strong> 以确认</span>`;
        label.querySelector('strong').textContent = required;
        requiredInput = document.createElement('input');
        requiredInput.type = 'text';
        requiredInput.className = 'config-input';
        requiredInput.autocomplete = 'off';
        label.appendChild(requiredInput);
        bodyEl.appendChild(label);
        okBtn.disabled = true;
        requiredInput.addEventListener('input', () => {
          okBtn.disabled = requiredInput.value.trim() !== required;
        });
      }

      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        modal.remove();
        syncModalOpenState();
        document.removeEventListener('keydown', onKeydown, true);
        if (previousFocus && document.contains(previousFocus)) {
          previousFocus.focus({ preventScroll: true });
        }
        resolve(value);
      }
      function onKeydown(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          finish(false);
        }
        if (event.key === 'Tab') {
          trapFocus(modal, event);
        }
      }

      cancelBtn.addEventListener('click', () => finish(false));
      closeBtn.addEventListener('click', () => finish(false));
      okBtn.addEventListener('click', () => finish(true));
      modal.addEventListener('click', (event) => {
        if (event.target === modal) finish(false);
      });
      document.addEventListener('keydown', onKeydown, true);

      document.body.appendChild(modal);
      syncModalOpenState();
      requestAnimationFrame(() => (requiredInput || okBtn).focus({ preventScroll: true }));
    });
  }

  function getFocusable(container) {
    return Array.from(container.querySelectorAll(
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((el) => el.offsetParent !== null || el === document.activeElement);
  }

  function trapFocus(modal, event) {
    const focusable = getFocusable(modal);
    if (!focusable.length) {
      event.preventDefault();
      modal.focus({ preventScroll: true });
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }

  function isVisibleModal(modal) {
    if (!modal || !document.body.contains(modal)) return false;
    const style = window.getComputedStyle(modal);
    return style.display !== 'none' && style.visibility !== 'hidden' && modal.getClientRects().length > 0;
  }

  function syncModalOpenState() {
    const hasVisibleModal = Array.from(document.querySelectorAll('.modal')).some(isVisibleModal);
    document.body.classList.toggle('modal-open', hasVisibleModal);
  }

  const modalFocus = new WeakMap();
  function prepareVisibleModal(modal) {
    if (!modal || modal.classList.contains('ac-confirm-modal')) return;
    if (!modal.hasAttribute('role')) modal.setAttribute('role', 'dialog');
    if (!modal.hasAttribute('aria-modal')) modal.setAttribute('aria-modal', 'true');
    if (!modalFocus.has(modal)) {
      const active = document.activeElement;
      if (active instanceof HTMLElement && !modal.contains(active)) {
        modalFocus.set(modal, active === document.body && lastInteractionTarget ? lastInteractionTarget : active);
      } else if (lastInteractionTarget && document.contains(lastInteractionTarget) && !modal.contains(lastInteractionTarget)) {
        modalFocus.set(modal, lastInteractionTarget);
      }
    }
    requestAnimationFrame(() => {
      if (!isVisibleModal(modal) || modal.contains(document.activeElement)) return;
      const target = getFocusable(modal)[0] || modal.querySelector('.modal-content') || modal;
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    });
  }

  function restoreModalFocus(modal) {
    const previous = modalFocus.get(modal);
    modalFocus.delete(modal);
    if (previous && document.contains(previous)) {
      previous.focus({ preventScroll: true });
    }
  }

  function getTopVisibleModal() {
    return Array.from(document.querySelectorAll('.modal')).filter(isVisibleModal).pop();
  }

  function closeManagedModal(modal) {
    if (!modal || modal.classList.contains('ac-confirm-modal')) return;
    modal.classList.remove('show', 'active');
    modal.removeAttribute('open');
    modal.style.display = 'none';
    syncModalOpenState();
    restoreModalFocus(modal);
  }

  function initModalInteractions() {
    document.addEventListener('click', (event) => {
      const target = event.target;
      if (target instanceof HTMLElement && target.classList.contains('modal')) {
        closeManagedModal(target);
      }
    });
    document.addEventListener('keydown', (event) => {
      const modal = getTopVisibleModal();
      if (!modal || modal.classList.contains('ac-confirm-modal')) return;
      if (event.key === 'Escape') {
        const target = event.target;
        if (target instanceof HTMLElement && target.closest('.custom-select-wrapper.open')) return;
        event.preventDefault();
        closeManagedModal(modal);
      } else if (event.key === 'Tab') {
        trapFocus(modal, event);
      }
    }, true);
    const observer = new MutationObserver((records) => {
      const seen = new Set();
      records.forEach((record) => {
        const target = record.target;
        if (target instanceof HTMLElement && target.classList.contains('modal') && !seen.has(target)) {
          seen.add(target);
          if (isVisibleModal(target)) prepareVisibleModal(target);
          else restoreModalFocus(target);
        }
      });
      syncModalOpenState();
    });
    const observeModal = (modal) => {
      if (!(modal instanceof HTMLElement) || !modal.classList.contains('modal') || modal.dataset.modalObserved) return;
      modal.dataset.modalObserved = '1';
      observer.observe(modal, { attributes: true, attributeFilter: ['style', 'class', 'open'] });
      if (isVisibleModal(modal)) prepareVisibleModal(modal);
    };
    const observeAll = () => {
      document.querySelectorAll('.modal').forEach(observeModal);
      syncModalOpenState();
    };
    const observeAddedModals = (records) => {
      let changed = false;
      records.forEach((record) => {
        record.addedNodes.forEach((node) => {
          if (!(node instanceof HTMLElement)) return;
          if (node.classList.contains('modal')) {
            observeModal(node);
            changed = true;
          }
        });
      });
      if (changed) syncModalOpenState();
    };
    observeAll();
    new MutationObserver(observeAddedModals).observe(document.body, { childList: true });
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initModalInteractions, { once: true });
  } else {
    initModalInteractions();
  }

  /* -------- 暴露 -------- */
  window.ui = window.ui || {};
  window.ui.toast = toast;
  window.ui.notify = notify;
  window.ui.confirmDialog = confirmDialog;
  window.ui.closeModal = closeManagedModal;
  window.ui.syncModalOpenState = syncModalOpenState;
  window.toast = toast;
  window.notify = notify;
  window.confirmDialog = confirmDialog;
})();
