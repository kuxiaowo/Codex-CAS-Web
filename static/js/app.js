(() => {
  'use strict';

  let account = null;

  function currentReturnPath() {
    return `${window.location.pathname}${window.location.search}${window.location.hash}`;
  }

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

  function initGalleryViewer() {
    const page = document.querySelector('[data-gallery-page]'); const dialog = document.querySelector('[data-gallery-lightbox]');
    if (!page || !dialog) return;
    const list = page.querySelector('.gallery-reading-list'); const sentinel = page.querySelector('[data-gallery-load-sentinel]');
    const buttons = [...page.querySelectorAll('[data-gallery-image]')]; const image = dialog.querySelector('img'); const count = dialog.querySelector('[data-lightbox-count]'); let activeIndex = 0; let touchStartX = null; let loading = false;
    const show = (index) => { activeIndex = (index + buttons.length) % buttons.length; const source = buttons[activeIndex].querySelector('img'); image.src = source.dataset.originalSrc; image.alt = source.alt; count.textContent = `${activeIndex + 1} / ${buttons.length}`; };
    const bind = (button) => button.addEventListener('click', () => { show(buttons.indexOf(button)); dialog.showModal(); });
    buttons.forEach(bind);
    const appendImage = (item) => {
      const index = buttons.length; const figure = document.createElement('figure'); figure.className = 'gallery-page';
      const button = document.createElement('button'); button.type = 'button'; button.dataset.galleryImage = String(index); button.setAttribute('aria-label', `放大第 ${index + 1} 张图片`);
      const preview = document.createElement('img'); preview.src = item.thumbSrc; preview.dataset.originalSrc = item.src; preview.alt = `${document.querySelector('.gallery-reading-header h1')?.textContent || '图集'}，第 ${index + 1} 张`; preview.loading = 'lazy'; preview.decoding = 'async';
      const caption = document.createElement('figcaption'); caption.textContent = `${String(index + 1).padStart(2, '0')} / ${String(page.dataset.imageCount || buttons.length + 1).padStart(2, '0')}`;
      button.append(preview); figure.append(button, caption); list.append(figure); buttons.push(button); bind(button);
    };
    const loadMore = async () => {
      if (loading || page.dataset.hasMore !== 'true') return;
      loading = true;
      try {
        const query = new URLSearchParams({ limit: '30' });
        if (page.dataset.nextCursor) query.set('cursor', page.dataset.nextCursor);
        const result = await api(`/api/galleries/${page.dataset.galleryId}/images?${query}`);
        result.data.forEach(appendImage); page.dataset.nextCursor = result.nextCursor || ''; page.dataset.hasMore = String(result.hasMore); sentinel.hidden = !result.hasMore;
      } catch (error) { sentinel.textContent = '更多图片加载失败，滚动后重试'; toast(error.message, true); }
      finally { loading = false; }
    };
    if (sentinel && 'IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) loadMore(); }, { rootMargin: '600px' }); observer.observe(sentinel);
    } else if (sentinel) { sentinel.addEventListener('click', loadMore); }
    dialog.querySelector('[data-lightbox-close]').addEventListener('click', () => dialog.close()); dialog.querySelector('[data-lightbox-prev]').addEventListener('click', () => show(activeIndex - 1)); dialog.querySelector('[data-lightbox-next]').addEventListener('click', () => show(activeIndex + 1));
    dialog.addEventListener('keydown', (event) => { if (event.key === 'ArrowLeft') show(activeIndex - 1); if (event.key === 'ArrowRight') show(activeIndex + 1); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }); dialog.addEventListener('close', () => image.removeAttribute('src'));
    dialog.addEventListener('touchstart', (event) => { touchStartX = event.changedTouches[0]?.clientX ?? null; }, { passive: true }); dialog.addEventListener('touchend', (event) => { if (touchStartX === null) return; const delta = (event.changedTouches[0]?.clientX ?? touchStartX) - touchStartX; if (Math.abs(delta) > 50) show(activeIndex + (delta < 0 ? 1 : -1)); touchStartX = null; }, { passive: true });
  }

  window.CASNotes = Object.freeze({ api, formatDate, refreshAccount, toast, currentAccount: () => account });
  initNavigation();

  refreshAccount().then((user) => {
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
