(() => {
  'use strict';

  const TOKEN_KEY = 'casNotesAccessToken';

  function token() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token()) headers.set('Authorization', `Bearer ${token()}`);
    if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 204) return null;
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
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
    if (!value) return '';
    return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value));
  }

  async function refreshAccount() {
    const name = document.querySelector('[data-account-name]');
    const role = document.querySelector('[data-account-role]');
    const avatar = document.querySelector('[data-account-avatar]');
    const action = document.querySelector('[data-account-action]');
    const adminLink = document.querySelector('[data-admin-link]');
    if (!token() || !name || !role || !avatar || !action) return null;
    try {
      const { data } = await api('/api/auth/me');
      name.textContent = data.displayName;
      role.textContent = data.role === 'admin' ? '管理员账户' : `@${data.username}`;
      avatar.textContent = data.displayName.trim().slice(0, 1).toUpperCase();
      adminLink?.classList.toggle('is-hidden', data.role !== 'admin');
      action.href = '#logout';
      action.setAttribute('aria-label', '退出登录');
      action.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M14 8l4 4-4 4M18 12H7M10 4H4v16h6"/></svg>';
      action.addEventListener('click', (event) => {
        event.preventDefault();
        localStorage.removeItem(TOKEN_KEY);
        window.location.reload();
      }, { once: true });
      return data;
    } catch {
      localStorage.removeItem(TOKEN_KEY);
      return null;
    }
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
      if (event.key === '/' && !editing) {
        event.preventDefault();
        document.querySelector('.side-search input')?.focus();
      }
      if (event.key === 'Escape') setOpen(false);
    });
  }

  function initAuth() {
    const tabs = document.querySelectorAll('[data-auth-tab]');
    const forms = document.querySelectorAll('[data-auth-form]');
    if (!forms.length) return;
    tabs.forEach((tab) => tab.addEventListener('click', () => {
      tabs.forEach((item) => item.classList.toggle('is-active', item === tab));
      forms.forEach((form) => form.classList.toggle('is-hidden', form.dataset.authForm !== tab.dataset.authTab));
    }));
    forms.forEach((form) => form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const error = form.querySelector('[data-form-error]');
      const button = form.querySelector('button[type="submit"]');
      error.textContent = '';
      const values = Object.fromEntries(new FormData(form));
      if (form.dataset.authForm === 'register' && values.password !== values.confirmPassword) {
        error.textContent = '两次输入的密码不一致';
        form.elements.confirmPassword.focus();
        return;
      }
      button.disabled = true;
      try {
        const endpoint = form.dataset.authForm === 'login' ? '/api/auth/login' : '/api/auth/register';
        const payload = await api(endpoint, { method: 'POST', body: JSON.stringify(values) });
        localStorage.setItem(TOKEN_KEY, payload.accessToken);
        const me = await api('/api/auth/me');
        window.location.href = me.data.role === 'admin' ? '/admin' : '/';
      } catch (requestError) {
        error.textContent = requestError.message;
      } finally {
        button.disabled = false;
      }
    }));
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
          const empty = document.createElement('div');
          empty.className = 'comment-empty';
          empty.textContent = '还没有留言。你可以写下第一条补充。';
          list.append(empty);
          return;
        }
        data.forEach((comment) => {
          const item = document.createElement('article');
          item.className = 'comment-item';
          const header = document.createElement('header');
          const author = document.createElement('strong');
          const time = document.createElement('time');
          const content = document.createElement('p');
          author.textContent = comment.author;
          time.textContent = formatDate(comment.createdAt);
          content.textContent = comment.content;
          header.append(author, time);
          item.append(header, content);
          list.append(item);
        });
      } catch (error) {
        toast(error.message, true);
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!token()) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const content = form.elements.content.value.trim();
      if (!content) return;
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        await api(`/api/galleries/${galleryId}/comments`, { method: 'POST', body: JSON.stringify({ content }) });
        form.reset();
        toast('留言已发布');
        await load();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
    await load();
  }

  function initGalleryViewer() {
    const page = document.querySelector('[data-gallery-page]');
    const dialog = document.querySelector('[data-gallery-lightbox]');
    if (!page || !dialog) return;
    const buttons = [...page.querySelectorAll('[data-gallery-image]')];
    const image = dialog.querySelector('img');
    const count = dialog.querySelector('[data-lightbox-count]');
    let activeIndex = 0;
    let touchStartX = null;

    const show = (index) => {
      activeIndex = (index + buttons.length) % buttons.length;
      const source = buttons[activeIndex].querySelector('img');
      image.src = source.dataset.originalSrc;
      image.alt = source.alt;
      count.textContent = `${activeIndex + 1} / ${buttons.length}`;
    };
    buttons.forEach((button, index) => button.addEventListener('click', () => {
      show(index);
      dialog.showModal();
    }));
    dialog.querySelector('[data-lightbox-close]').addEventListener('click', () => dialog.close());
    dialog.querySelector('[data-lightbox-prev]').addEventListener('click', () => show(activeIndex - 1));
    dialog.querySelector('[data-lightbox-next]').addEventListener('click', () => show(activeIndex + 1));
    dialog.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowLeft') show(activeIndex - 1);
      if (event.key === 'ArrowRight') show(activeIndex + 1);
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener('close', () => image.removeAttribute('src'));
    dialog.addEventListener('touchstart', (event) => {
      touchStartX = event.changedTouches[0]?.clientX ?? null;
    }, { passive: true });
    dialog.addEventListener('touchend', (event) => {
      if (touchStartX === null) return;
      const delta = (event.changedTouches[0]?.clientX ?? touchStartX) - touchStartX;
      if (Math.abs(delta) > 50) show(activeIndex + (delta < 0 ? 1 : -1));
      touchStartX = null;
    }, { passive: true });
  }

  window.CASNotes = Object.freeze({ api, formatDate, refreshAccount, toast, token, TOKEN_KEY });
  initNavigation();
  initAuth();
  refreshAccount();
  initComments();
  initGalleryViewer();
})();
