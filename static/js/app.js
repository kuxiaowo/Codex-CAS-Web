(() => {
  'use strict';

  let account = null;

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    if (response.status === 204) return null;
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const error = new Error(payload?.detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, isError = false) {
    const region = document.querySelector('[data-toast-region]');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast${isError ? ' is-error' : ''}`;
    item.textContent = message;
    region.append(item);
    window.setTimeout(() => item.remove(), 3200);
  }

  function formatDate(value) {
    return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value)) : '';
  }

  async function refreshAccount() {
    const name = document.querySelector('[data-account-name]');
    const role = document.querySelector('[data-account-role]');
    const avatar = document.querySelector('[data-account-avatar]');
    const action = document.querySelector('[data-account-action]');
    const adminLink = document.querySelector('[data-admin-link]');
    if (!name || !role || !avatar || !action) return null;
    try {
      const { data } = await api('/api/auth/me');
      account = data;
      name.textContent = data.displayName;
      role.textContent = data.role === 'admin' ? '管理员账户' : `@${data.username}`;
      avatar.textContent = data.displayName.trim().slice(0, 1).toUpperCase();
      if (data.avatarUrl) {
        const image = document.createElement('img');
        image.src = data.avatarUrl;
        image.alt = '';
        image.addEventListener('error', () => image.remove(), { once: true });
        avatar.append(image);
      }
      adminLink?.classList.toggle('is-hidden', data.role !== 'admin');
      action.href = '#logout';
      action.setAttribute('aria-label', '退出本站');
      action.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M14 8l4 4-4 4M18 12H7M10 4H4v16h6"/></svg>';
      action.addEventListener('click', async (event) => {
        event.preventDefault();
        try { await api('/api/auth/logout', { method: 'POST' }); } finally {
          window.localStorage.setItem('cas-sso-suppressed-until', String(Date.now() + 10 * 60 * 1000));
          window.location.assign('/');
        }
      }, { once: true });
      return data;
    } catch { return null; }
  }

  function initNavigation() {
    const toggle = document.querySelector('[data-menu-toggle]');
    const sidebar = document.querySelector('[data-sidebar]');
    const scrim = document.querySelector('[data-sidebar-scrim]');
    const setOpen = (open) => {
      sidebar?.classList.toggle('is-open', open);
      scrim?.classList.toggle('is-visible', open);
      toggle?.setAttribute('aria-expanded', String(open));
    };
    toggle?.addEventListener('click', () => setOpen(!sidebar?.classList.contains('is-open')));
    scrim?.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (event) => {
      const target = event.target;
      const editing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
      if (event.key === '/' && !editing) { event.preventDefault(); document.querySelector('.side-search input')?.focus(); }
      if (event.key === 'Escape') setOpen(false);
    });
  }

  async function initComments() {
    const root = document.querySelector('[data-comments]');
    if (!root) return;
    const galleryId = root.dataset.galleryId;
    const list = root.querySelector('[data-comment-list]');
    const form = root.querySelector('[data-comment-form]');
    async function load() {
      try {
        const { data } = await api(`/api/galleries/${galleryId}/comments`);
        list.replaceChildren();
        if (!data.length) {
          const empty = document.createElement('div'); empty.className = 'comment-empty'; empty.textContent = '还没有留言。你可以写下第一条补充。'; list.append(empty); return;
        }
        data.forEach((comment) => {
          const item = document.createElement('article'); item.className = 'comment-item';
          const header = document.createElement('header'); const author = document.createElement('strong'); const time = document.createElement('time'); const content = document.createElement('p');
          if (comment.authorAvatarUrl) {
            const avatar = document.createElement('span'); avatar.className = 'comment-author-avatar'; avatar.textContent = comment.author.trim().slice(0, 1).toUpperCase();
            const image = document.createElement('img'); image.src = comment.authorAvatarUrl; image.alt = ''; image.addEventListener('error', () => image.remove(), { once: true });
            avatar.append(image); header.append(avatar);
          }
          author.textContent = comment.author; time.textContent = formatDate(comment.createdAt); content.textContent = comment.content;
          header.append(author, time); item.append(header, content); list.append(item);
        });
      } catch (error) { toast(error.message, true); }
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!account) { window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`); return; }
      const content = form.elements.content.value.trim(); if (!content) return;
      const button = form.querySelector('button[type="submit"]'); button.disabled = true;
      try { await api(`/api/galleries/${galleryId}/comments`, { method: 'POST', body: JSON.stringify({ content }) }); form.reset(); toast('留言已发布'); await load(); }
      catch (error) { if (error.status === 401) window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`); else toast(error.message, true); }
      finally { button.disabled = false; }
    });
    await load();
  }

  function initGalleryViewer() {
    const page = document.querySelector('[data-gallery-page]'); const dialog = document.querySelector('[data-gallery-lightbox]');
    if (!page || !dialog) return;
    const buttons = [...page.querySelectorAll('[data-gallery-image]')]; const image = dialog.querySelector('img'); const count = dialog.querySelector('[data-lightbox-count]'); let activeIndex = 0; let touchStartX = null;
    const show = (index) => { activeIndex = (index + buttons.length) % buttons.length; const source = buttons[activeIndex].querySelector('img'); image.src = source.dataset.originalSrc; image.alt = source.alt; count.textContent = `${activeIndex + 1} / ${buttons.length}`; };
    buttons.forEach((button, index) => button.addEventListener('click', () => { show(index); dialog.showModal(); }));
    dialog.querySelector('[data-lightbox-close]').addEventListener('click', () => dialog.close()); dialog.querySelector('[data-lightbox-prev]').addEventListener('click', () => show(activeIndex - 1)); dialog.querySelector('[data-lightbox-next]').addEventListener('click', () => show(activeIndex + 1));
    dialog.addEventListener('keydown', (event) => { if (event.key === 'ArrowLeft') show(activeIndex - 1); if (event.key === 'ArrowRight') show(activeIndex + 1); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }); dialog.addEventListener('close', () => image.removeAttribute('src'));
    dialog.addEventListener('touchstart', (event) => { touchStartX = event.changedTouches[0]?.clientX ?? null; }, { passive: true }); dialog.addEventListener('touchend', (event) => { if (touchStartX === null) return; const delta = (event.changedTouches[0]?.clientX ?? touchStartX) - touchStartX; if (Math.abs(delta) > 50) show(activeIndex + (delta < 0 ? 1 : -1)); touchStartX = null; }, { passive: true });
  }

  window.CASNotes = Object.freeze({ api, formatDate, refreshAccount, toast, currentAccount: () => account });
  initNavigation();

  refreshAccount().then((user) => {
    initComments();
    if (window.location.pathname === '/login') return;
    if (user) {
      window.sessionStorage.removeItem('cas-sso-probe');
      window.localStorage.removeItem('cas-sso-suppressed-until');
      return;
    }
    const alreadyProbed = window.sessionStorage.getItem('cas-sso-probe') === '1';
    const suppressedUntil = Number(window.localStorage.getItem('cas-sso-suppressed-until') || 0);
    if (!alreadyProbed && Date.now() >= suppressedUntil) {
      window.sessionStorage.setItem('cas-sso-probe', '1');
      const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      window.location.assign(`/auth/login?prompt=none&next=${encodeURIComponent(next)}`);
    }
  });
  initGalleryViewer();
})();
