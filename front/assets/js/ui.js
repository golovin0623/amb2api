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
    const kind = opts.kind || 'info';

    // 所有消息都归档到消息中心，便于点开消息图标查看历史
    recordNotification({
      kind: kind,
      title: opts.title || String(opts.message || ''),
      desc: opts.description || '',
    });

    // 策略：仅 error / warning / danger 弹出顶部 toast；success / info 静默进入消息中心
    if (!(opts.force === true || kind === 'error' || kind === 'danger' || kind === 'warning')) {
      return { dismiss() {} };
    }

    const region = ensureToastRegion();
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

  /* -------- 消息中心 (Notification Center) -------- */
  const NOTI_MAX = 60;
  const notiStore = [];          // { id, ts, kind, title, desc, read }
  let notiSeq = 0;
  let notiBtn = null;
  let notiBadge = null;
  let notiPanel = null;
  let notiListEl = null;
  let notiOpen = false;

  const NOTI_KIND_META = {
    success: { icon: 'check-circle' },
    error:   { icon: 'x-circle' },
    danger:  { icon: 'x-circle' },
    warning: { icon: 'alert-triangle' },
    info:    { icon: 'info' },
  };

  function notiKindMeta(kind) { return NOTI_KIND_META[kind] || NOTI_KIND_META.info; }

  function formatNotiTime(ts) {
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function unreadCount() {
    let n = 0;
    for (const e of notiStore) if (!e.read) n++;
    return n;
  }

  function updateNotiBadge() {
    if (!notiBadge) return;
    const n = unreadCount();
    if (n > 0) {
      notiBadge.textContent = n > 99 ? '99+' : String(n);
      notiBadge.hidden = false;
    } else {
      notiBadge.textContent = '';
      notiBadge.hidden = true;
    }
  }

  function recordNotification(entry) {
    entry = entry || {};
    const title = String(entry.title || '').trim();
    const desc = String(entry.desc || '').trim();
    if (!title && !desc) return null;
    const e = {
      id: ++notiSeq,
      ts: Date.now(),
      kind: entry.kind || 'info',
      title: title || desc,
      desc: title ? desc : '',
      read: false,
    };
    notiStore.unshift(e);
    if (notiStore.length > NOTI_MAX) notiStore.length = NOTI_MAX;
    updateNotiBadge();
    if (notiOpen) renderNotiList();
    return e;
  }

  function renderNotiList() {
    if (!notiListEl) return;
    notiListEl.textContent = '';
    if (!notiStore.length) {
      const empty = document.createElement('div');
      empty.className = 'noti-empty';
      if (window.icons) empty.appendChild(window.icons.create('inbox', { size: 28 }));
      const p = document.createElement('p');
      p.textContent = '暂无消息';
      empty.appendChild(p);
      notiListEl.appendChild(empty);
      return;
    }
    for (const e of notiStore) {
      const meta = notiKindMeta(e.kind);
      const item = document.createElement('div');
      item.className = 'noti-item noti-item--' + e.kind + (e.read ? '' : ' noti-item--unread');

      const icon = document.createElement('span');
      icon.className = 'noti-item__icon';
      if (window.icons) icon.appendChild(window.icons.create(meta.icon, { size: 16 }));
      item.appendChild(icon);

      const body = document.createElement('div');
      body.className = 'noti-item__body';

      const titleEl = document.createElement('div');
      titleEl.className = 'noti-item__title';
      titleEl.textContent = e.title;
      body.appendChild(titleEl);

      if (e.desc) {
        const descEl = document.createElement('div');
        descEl.className = 'noti-item__desc';
        descEl.textContent = e.desc;
        body.appendChild(descEl);
      }

      const timeEl = document.createElement('div');
      timeEl.className = 'noti-item__time';
      timeEl.textContent = formatNotiTime(e.ts);
      body.appendChild(timeEl);

      item.appendChild(body);

      // 较长内容可点击展开 / 收起
      if (e.desc || e.title.length > 42) {
        item.classList.add('noti-item--expandable');
        item.addEventListener('click', () => item.classList.toggle('noti-item--open'));
      }

      notiListEl.appendChild(item);
    }
  }

  function openNotiPanel() {
    if (!notiPanel) return;
    notiOpen = true;
    renderNotiList();              // 先按当前已读状态渲染（保留未读高亮）
    notiPanel.hidden = false;
    if (notiBtn) notiBtn.setAttribute('aria-expanded', 'true');
    // 打开即视为已读：清空角标，但本次查看仍保留高亮
    for (const e of notiStore) e.read = true;
    updateNotiBadge();
    document.addEventListener('pointerdown', onNotiOutside, true);
    document.addEventListener('keydown', onNotiKeydown, true);
  }

  function closeNotiPanel() {
    if (!notiPanel) return;
    notiOpen = false;
    notiPanel.hidden = true;
    if (notiBtn) notiBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onNotiOutside, true);
    document.removeEventListener('keydown', onNotiKeydown, true);
  }

  function toggleNotiPanel() { if (notiOpen) closeNotiPanel(); else openNotiPanel(); }

  function onNotiOutside(ev) {
    if (!notiOpen) return;
    if (notiPanel.contains(ev.target) || (notiBtn && notiBtn.contains(ev.target))) return;
    closeNotiPanel();
  }

  function onNotiKeydown(ev) { if (ev.key === 'Escape') closeNotiPanel(); }

  function markAllRead() {
    for (const e of notiStore) e.read = true;
    updateNotiBadge();
    if (notiOpen) renderNotiList();
  }

  function clearNoti() {
    notiStore.length = 0;
    updateNotiBadge();
    renderNotiList();
  }

  function buildNotiCenter() {
    if (notiBtn) return;
    const row = document.querySelector('.header-title-row');
    if (!row) return;

    const wrap = document.createElement('div');
    wrap.className = 'noti-center';

    notiBtn = document.createElement('button');
    notiBtn.type = 'button';
    notiBtn.className = 'noti-bell';
    notiBtn.title = '消息中心';
    notiBtn.setAttribute('aria-label', '消息中心');
    notiBtn.setAttribute('aria-haspopup', 'true');
    notiBtn.setAttribute('aria-expanded', 'false');
    if (window.icons) notiBtn.appendChild(window.icons.create('bell', { size: 18 }));

    notiBadge = document.createElement('span');
    notiBadge.className = 'noti-bell__badge';
    notiBadge.hidden = true;
    notiBtn.appendChild(notiBadge);
    notiBtn.addEventListener('click', (ev) => { ev.stopPropagation(); toggleNotiPanel(); });

    notiPanel = document.createElement('div');
    notiPanel.className = 'noti-panel';
    notiPanel.hidden = true;
    notiPanel.setAttribute('role', 'dialog');
    notiPanel.setAttribute('aria-label', '消息中心');

    const head = document.createElement('div');
    head.className = 'noti-panel__head';
    const headTitle = document.createElement('span');
    headTitle.className = 'noti-panel__title';
    headTitle.textContent = '消息中心';
    head.appendChild(headTitle);

    const actions = document.createElement('div');
    actions.className = 'noti-panel__actions';
    const readBtn = document.createElement('button');
    readBtn.type = 'button';
    readBtn.className = 'noti-panel__action';
    readBtn.textContent = '全部已读';
    readBtn.addEventListener('click', (ev) => { ev.stopPropagation(); markAllRead(); });
    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'noti-panel__clear';
    clearBtn.textContent = '清空';
    clearBtn.addEventListener('click', (ev) => { ev.stopPropagation(); clearNoti(); });
    actions.appendChild(readBtn);
    actions.appendChild(clearBtn);
    head.appendChild(actions);
    notiPanel.appendChild(head);

    notiListEl = document.createElement('div');
    notiListEl.className = 'noti-list';
    notiPanel.appendChild(notiListEl);

    wrap.appendChild(notiBtn);
    wrap.appendChild(notiPanel);

    const logout = row.querySelector('.logout-btn');
    if (logout) row.insertBefore(wrap, logout);
    else row.appendChild(wrap);

    updateNotiBadge();
  }

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
   * type: 'info' | 'success' | 'error' | 'warning'
   * 另支持伪类型 'progress'/'loading'：进行中的提示，强制弹窗（不受
   * “仅 error/warning 弹窗”策略静默），用 info 样式展示。 */
  if (!window.__originalShowStatus) {
    const origShowStatus = window.showStatus;
    window.__originalShowStatus = origShowStatus;
    window.showStatus = function (message, type) {
      try {
        let kind = type || inferKind(message);
        let force = false;
        if (kind === 'progress' || kind === 'loading') { kind = 'info'; force = true; }
        toast({
          kind: kind,
          title: String(message || ''),
          duration: 4500,
          force: force,
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

  function initUiEnhancements() {
    initModalInteractions();
    buildNotiCenter();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUiEnhancements, { once: true });
  } else {
    initUiEnhancements();
  }

  /* -------- 暴露 -------- */
  window.ui = window.ui || {};
  window.ui.toast = toast;
  window.ui.notify = notify;
  window.ui.notiCenter = {
    record: recordNotification,
    open: openNotiPanel,
    close: closeNotiPanel,
    markAllRead: markAllRead,
    clear: clearNoti,
  };
  window.ui.confirmDialog = confirmDialog;
  window.ui.closeModal = closeManagedModal;
  window.ui.syncModalOpenState = syncModalOpenState;
  window.toast = toast;
  window.notify = notify;
  window.confirmDialog = confirmDialog;
})();
