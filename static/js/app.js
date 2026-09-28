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

  async function initComments() {
    const root = document.querySelector('[data-comments]');
    if (!root) return;
    const galleryId = root.dataset.galleryId;
    const list = root.querySelector('[data-comment-list]');
    const form = root.querySelector('[data-comment-form]');
    let replying = null, busy = false;
    const replyBar = document.createElement('div'); replyBar.className = 'cas-reply-bar'; replyBar.hidden = true;
    const replyLabel = document.createElement('span');
    const cancel = document.createElement('button'); cancel.type='button';cancel.textContent='取消回复';
    replyBar.append(replyLabel,cancel);form.prepend(replyBar);
    cancel.onclick=()=>{replying=null;replyBar.hidden=true;};
    async function load() {
      const {data} = await api(`/api/galleries/${galleryId}/comments`);
      const byId = new Map(data.map(c=>[c.id,c]));
      const rootId = comment => {const seen=new Set();while(comment.parentId && byId.has(comment.parentId) && !seen.has(comment.id)){seen.add(comment.id);comment=byId.get(comment.parentId);}return comment.id;};
      list.replaceChildren();
      if(!data.length){const empty=document.createElement('div');empty.className='comment-empty';empty.textContent='还没有留言。你可以写下第一条补充。';list.append(empty);return;}
      function item(comment, nested=false){
        const article=document.createElement('article');article.id=`comment-${comment.id}`;article.className=`comment-item ${nested?'cas-comment-reply':''}`;
        const header=document.createElement('header'),author=document.createElement('strong'),time=document.createElement('time'),content=document.createElement('p');
        if(comment.authorAvatarUrl){const avatar=document.createElement('span');avatar.className='comment-author-avatar';const image=document.createElement('img');image.src=comment.authorAvatarUrl;image.alt='';image.onerror=()=>image.remove();avatar.append(image);header.append(avatar);}
        author.textContent=comment.author;time.textContent=formatDate(comment.createdAt);header.append(author,time);
        const parent=byId.get(comment.parentId);
        if(parent){const label=document.createElement('small');label.textContent=`回复 @${parent.author}`;header.append(label);}
        content.textContent=comment.status==='hidden'?'该留言正在复核':comment.status==='deleted'?'该留言已删除':comment.content;
        article.append(header,content);
        if(comment.status==='visible'){
          const reply=document.createElement('button');reply.type='button';reply.className='button button-ghost';reply.textContent='回复';
          reply.onclick=()=>{if(!account){window.location.assign(`/login?next=${encodeURIComponent(currentReturnPath())}`);return;}replying=comment.id;replyLabel.textContent=`回复 @${comment.author}`;replyBar.hidden=false;form.elements.content.focus();};
          article.append(reply);
        }
        if(comment.status!=='deleted' && account?.id===comment.userId){
          const remove=document.createElement('button');remove.type='button';remove.className='button button-ghost';remove.textContent='删除';
          remove.onclick=async()=>{if(busy || !window.confirm('确认删除自己的留言？正常回复会保留。'))return;busy=true;try{await api(`/api/comments/${comment.id}`,{method:'DELETE'});await load();}catch(error){toast(error.message,true);}finally{busy=false;}};
          article.append(remove);
        }
        return article;
      }
      const groups=new Map();data.forEach(c=>{const id=rootId(c);if(!groups.has(id))groups.set(id,[]);groups.get(id).push(c);});
      groups.forEach((comments,id)=>{const thread=document.createElement('section');thread.className='cas-comment-thread';const parent=byId.get(id);if(parent)thread.append(item(parent));comments.filter(c=>c.id!==id).forEach(c=>thread.append(item(c,true)));list.append(thread);});
      if(replying && (!byId.has(replying)||byId.get(replying).status!=='visible')){replyLabel.textContent='原留言正在复核或已删除；回复草稿已保留';}
    }
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy)return;
      if(!account){window.location.assign(`/login?next=${encodeURIComponent(currentReturnPath())}`);return;}
      const content=form.elements.content.value.trim();if(!content)return;
      const button=form.querySelector('button[type="submit"]');button.disabled=true;busy=true;
      try{
        const turnstileToken=form.querySelector('[name="cf-turnstile-response"]')?.value;
        if(!turnstileToken)throw new Error('请完成人机验证');
        await api(`/api/galleries/${galleryId}/comments`,{method:'POST',body:JSON.stringify({content,parentId:replying,turnstileToken})});
        form.reset();replying=null;replyBar.hidden=true;toast('留言已发布');await load();
      }catch(error){if(error.status===401)window.location.assign(`/login?next=${encodeURIComponent(currentReturnPath())}`);else toast(error.message,true);}
      finally{window.turnstile?.reset();button.disabled=false;busy=false;}
    });
    await load().catch(error=>toast(error.message,true));
    window.setInterval(async()=>{if(document.hidden||busy)return;busy=true;try{await load();}catch{/* Retain the composer on polling failure. */}finally{busy=false;}},10000);
  }

  async function refreshSystemBadge(){
    if(!account)return;
    const link=document.querySelector('[data-system-link]'),badge=document.querySelector('[data-system-badge]');
    link?.classList.remove('is-hidden');
    try{const counts=await api('/api/message-center/unread-count');if(badge){badge.textContent=counts.system>99?'99+':String(counts.system);badge.hidden=!counts.system;}}catch{/* Notifications remain accessible. */}
  }
  window.addEventListener('casSystemMessagesRead',refreshSystemBadge);
  window.setInterval(()=>{if(!document.hidden)refreshSystemBadge();},10000);

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
    initComments();
    refreshSystemBadge();
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
