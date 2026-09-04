(() => {
  'use strict';

  const root = document.querySelector('[data-admin-app]');
  if (!root || !window.CASNotes) return;

  const { api, formatDate, toast } = window.CASNotes;
  const state = {
    categories: [],
    galleries: [],
    announcements: [],
    comments: [],
    users: [],
    settings: null,
    filePath: '',
    pickerPath: '',
    pickerTarget: null,
  };
  let dialogSave = null;

  function text(tag, value, className = '') {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value ?? '';
    return element;
  }

  function empty(container, message) {
    container.replaceChildren(text('div', message, 'empty-admin'));
  }

  function button(label, className, handler) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = `button ${className}`;
    element.textContent = label;
    element.addEventListener('click', handler);
    return element;
  }

  function listen(selector, eventName, handler) {
    root.querySelector(selector)?.addEventListener(eventName, handler);
  }

  function row(titleValue, metaValues, actions = []) {
    const item = document.createElement('article');
    item.className = 'admin-row';
    const main = document.createElement('div');
    main.className = 'admin-row-main';
    main.append(text('strong', titleValue));
    const meta = document.createElement('div');
    meta.className = 'admin-row-meta';
    metaValues.filter(Boolean).forEach((value, index) => {
      meta.append(text('span', value, index === 0 ? 'status' : ''));
    });
    main.append(meta);
    const actionArea = document.createElement('div');
    actionArea.className = 'admin-row-actions';
    actions.forEach((action) => actionArea.append(action));
    item.append(main, actionArea);
    return item;
  }

  async function removeWithConfirmation(message, path, reload) {
    if (!window.confirm(message)) return;
    try {
      await api(path, { method: 'DELETE' });
      toast('已删除');
      await reload();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function activateView(name) {
    root.querySelectorAll('[data-admin-view]').forEach((tab) => {
      tab.classList.toggle('is-active', tab.dataset.adminView === name);
    });
    root.querySelectorAll('[data-view-panel]').forEach((panel) => {
      panel.classList.toggle('is-active', panel.dataset.viewPanel === name);
    });
  }

  function openDialog({ title: titleValue, eyebrow, body, onSave }) {
    const dialog = root.querySelector('[data-admin-dialog]');
    root.querySelector('[data-dialog-title]').textContent = titleValue;
    root.querySelector('[data-dialog-eyebrow]').textContent = eyebrow;
    const dialogBody = root.querySelector('[data-dialog-body]');
    dialogBody.replaceChildren();
    body(dialogBody);
    dialogSave = onSave;
    dialog.showModal();
    dialogBody.querySelector('input, textarea, select')?.focus();
  }

  function field(labelText, input) {
    const label = document.createElement('label');
    label.append(text('span', labelText), input);
    return label;
  }

  function directoryField(value = '') {
    const wrap = document.createElement('div');
    wrap.className = 'directory-field';
    const control = input('resourceDir', value, { required: true, maxLength: 500 });
    control.readOnly = true;
    control.placeholder = '从 resources 中选择文件夹';
    wrap.append(control, button('浏览', 'button-ghost', () => openDirectoryPicker(control)));
    return field('资源文件夹', wrap);
  }

  function input(name, value = '', options = {}) {
    const element = document.createElement(options.multiline ? 'textarea' : 'input');
    element.name = name;
    element.value = value ?? '';
    if (options.type) element.type = options.type;
    if (options.required) element.required = true;
    if (options.min !== undefined) element.min = options.min;
    if (options.max !== undefined) element.max = options.max;
    if (options.maxLength) element.maxLength = options.maxLength;
    if (options.placeholder) element.placeholder = options.placeholder;
    return element;
  }

  function select(name, options, selectedValue) {
    const element = document.createElement('select');
    element.name = name;
    options.forEach(({ value, label }) => {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = label;
      option.selected = String(value) === String(selectedValue);
      element.append(option);
    });
    return element;
  }

  function checkbox(name, labelText, checked) {
    const label = document.createElement('label');
    label.className = 'inline-check';
    const element = document.createElement('input');
    element.type = 'checkbox';
    element.name = name;
    element.checked = Boolean(checked);
    label.append(element, text('span', labelText));
    return label;
  }

  function formData(body) {
    return Object.fromEntries(new FormData(body.closest('form')));
  }

  async function loadDashboard() {
    const { data } = await api('/api/admin/dashboard');
    const stats = root.querySelector('[data-dashboard-stats]');
    const items = [
      ['全部图集', data.counts.galleries],
      ['已发布', data.counts.publishedGalleries],
      ['注册用户', data.counts.users],
      ['留言', data.counts.comments],
      ['累计查看', data.counts.views],
    ];
    stats.replaceChildren(...items.map(([label, value]) => {
      const card = document.createElement('article');
      card.className = 'stat-card';
      card.append(text('small', label), text('strong', String(value)));
      return card;
    }));
    const recent = root.querySelector('[data-recent-galleries]');
    recent.replaceChildren(...data.recentGalleries.map((gallery) => row(
      gallery.title,
      [gallery.status, gallery.categoryName, `${gallery.imageCount} 张图片`, `更新于 ${formatDate(gallery.updatedAt)}`],
      [button('编辑', 'button-ghost', () => editGallery(gallery))],
    )));
  }

  async function loadCategories() {
    const { data } = await api('/api/admin/categories');
    state.categories = data;
    const container = root.querySelector('[data-categories-list]');
    if (!data.length) return empty(container, '还没有栏目。');
    container.replaceChildren(...data.map((category) => row(
      category.name,
      [category.isActive ? '启用' : '停用', category.slug, `${category.galleryCount} 个图集`, `排序 ${category.sortOrder}`],
      [
        button('编辑', 'button-ghost', () => editCategory(category)),
        button('删除', 'button-danger', () => removeWithConfirmation(
          `确定删除栏目“${category.name}”吗？`,
          `/api/admin/categories/${category.id}`,
          loadCategories,
        )),
      ],
    )));
  }

  function editCategory(category = null) {
    openDialog({
      title: category ? '编辑栏目' : '新建栏目',
      eyebrow: 'NAVIGATION',
      body(container) {
        container.append(
          field('栏目名称', input('name', category?.name, { required: true, maxLength: 50 })),
          field('URL 标识', input('slug', category?.slug, { required: true, maxLength: 50, placeholder: 'photo-archive' })),
          field('栏目说明', input('description', category?.description, { multiline: true, maxLength: 200 })),
          field('强调色', input('accent', category?.accent || '#8b7cff', { type: 'color' })),
          field('排序权重', input('sortOrder', category?.sortOrder ?? 10, { type: 'number', min: 0, max: 10000, required: true })),
          checkbox('isActive', '在前台显示该栏目', category?.isActive ?? true),
        );
      },
      async onSave(container) {
        const values = formData(container);
        const payload = {
          name: values.name,
          slug: values.slug,
          description: values.description,
          accent: values.accent,
          sort_order: Number(values.sortOrder),
          is_active: container.querySelector('[name="isActive"]').checked,
        };
        await api(category ? `/api/admin/categories/${category.id}` : '/api/admin/categories', {
          method: category ? 'PATCH' : 'POST', body: JSON.stringify(payload),
        });
        await Promise.all([loadCategories(), loadGalleries()]);
      },
    });
  }

  async function loadGalleries() {
    const { data } = await api('/api/admin/galleries');
    state.galleries = data;
    const container = root.querySelector('[data-galleries-list]');
    if (!data.length) return empty(container, '还没有图集。先在资源文件中准备图片目录。');
    container.replaceChildren(...data.map((gallery) => row(
      gallery.title,
      [gallery.status, gallery.categoryName, gallery.resourceDir, `${gallery.imageCount} 张图片`, `${gallery.views} 次查看`, formatDate(gallery.updatedAt)],
      [
        button('预览', 'button-ghost', () => window.open(`/galleries/${gallery.id}`, '_blank', 'noopener')),
        button('编辑', 'button-ghost', () => editGallery(gallery)),
        button('删除', 'button-danger', () => removeWithConfirmation(
          `确定删除图集“${gallery.title}”及其留言吗？资源文件不会删除。`,
          `/api/admin/galleries/${gallery.id}`,
          async () => { await Promise.all([loadGalleries(), loadDashboard()]); },
        )),
      ],
    )));
  }

  function editGallery(gallery = null) {
    if (!state.categories.length) {
      toast('请先创建栏目', true);
      activateView('categories');
      return;
    }
    openDialog({
      title: gallery ? '编辑图集' : '新建图集',
      eyebrow: 'COLLECTION',
      body(container) {
        container.append(
          field('简短标题', input('title', gallery?.title, { required: true, maxLength: 60 })),
          field('栏目', select('categoryId', state.categories.map((item) => ({ value: item.id, label: item.name })), gallery?.categoryId || state.categories[0].id)),
          directoryField(gallery?.resourceDir),
          field('状态', select('status', [
            { value: 'draft', label: '草稿' }, { value: 'published', label: '已发布' },
            { value: 'archived', label: '已归档' },
          ], gallery?.status || 'draft')),
          checkbox('isFeatured', '作为精选图集优先展示', gallery?.isFeatured ?? false),
        );
      },
      async onSave(container) {
        const values = formData(container);
        const payload = {
          category_id: Number(values.categoryId), title: values.title,
          resource_dir: values.resourceDir, status: values.status,
          is_featured: container.querySelector('[name="isFeatured"]').checked,
        };
        await api(gallery ? `/api/admin/galleries/${gallery.id}` : '/api/admin/galleries', {
          method: gallery ? 'PATCH' : 'POST', body: JSON.stringify(payload),
        });
        await Promise.all([loadGalleries(), loadCategories(), loadDashboard()]);
      },
    });
  }

  async function loadAnnouncements() {
    const { data } = await api('/api/admin/announcements');
    state.announcements = data;
    const container = root.querySelector('[data-announcements-list]');
    if (!data.length) return empty(container, '还没有公告。');
    container.replaceChildren(...data.map((announcement) => row(
      announcement.title,
      [announcement.status, announcement.isPinned ? '已置顶' : '普通', formatDate(announcement.updatedAt)],
      [
        button('编辑', 'button-ghost', () => editAnnouncement(announcement)),
        button('删除', 'button-danger', () => removeWithConfirmation(
          `确定永久删除公告“${announcement.title}”吗？`,
          `/api/admin/announcements/${announcement.id}`,
          loadAnnouncements,
        )),
      ],
    )));
  }

  function editAnnouncement(announcement = null) {
    openDialog({
      title: announcement ? '编辑公告' : '新建公告',
      eyebrow: 'BROADCAST',
      body(container) {
        container.append(
          field('公告标题', input('title', announcement?.title, { required: true, maxLength: 120 })),
          field('公告内容', input('content', announcement?.content, { multiline: true, required: true, maxLength: 5000 })),
          field('状态', select('status', [
            { value: 'published', label: '已发布' }, { value: 'archived', label: '已归档' },
          ], announcement?.status || 'published')),
          checkbox('isPinned', '置顶显示', announcement?.isPinned ?? false),
        );
      },
      async onSave(container) {
        const values = formData(container);
        await api(announcement ? `/api/admin/announcements/${announcement.id}` : '/api/admin/announcements', {
          method: announcement ? 'PATCH' : 'POST',
          body: JSON.stringify({
            title: values.title, content: values.content, status: values.status,
            is_pinned: container.querySelector('[name="isPinned"]').checked,
          }),
        });
        await loadAnnouncements();
      },
    });
  }

  async function loadComments() {
    const { data } = await api('/api/admin/comments');
    state.comments = data;
    const container = root.querySelector('[data-comments-list]');
    if (!data.length) return empty(container, '还没有留言。');
    container.replaceChildren(...data.map((comment) => row(
      comment.content,
      [comment.status, comment.author, comment.galleryTitle, formatDate(comment.createdAt)],
      [
        button(comment.status === 'visible' ? '隐藏' : '恢复', 'button-ghost', async () => {
          try {
            await api(`/api/admin/comments/${comment.id}`, {
              method: 'PATCH', body: JSON.stringify({ status: comment.status === 'visible' ? 'hidden' : 'visible' }),
            });
            toast('留言状态已更新');
            await loadComments();
          } catch (error) { toast(error.message, true); }
        }),
        button('删除', 'button-danger', () => removeWithConfirmation(
          '确定永久删除这条留言及其回复吗？',
          `/api/admin/comments/${comment.id}`,
          async () => { await Promise.all([loadComments(), loadDashboard()]); },
        )),
      ],
    )));
  }

  async function loadUsers() {
    const { data } = await api('/api/admin/users');
    state.users = data;
    const container = root.querySelector('[data-users-list]');
    container.replaceChildren(...data.map((user) => row(
      user.displayName,
      [user.isActive ? '启用' : '停用', `@${user.username}`, user.role, formatDate(user.createdAt)],
      [
        button('编辑', 'button-ghost', () => editUser(user)),
        button('删除', 'button-danger', () => removeWithConfirmation(
          `确定删除用户“${user.displayName}”及其留言吗？`,
          `/api/admin/users/${user.id}`,
          async () => { await Promise.all([loadUsers(), loadDashboard()]); },
        )),
      ],
    )));
  }

  function editUser(user) {
    openDialog({
      title: '管理本站成员',
      eyebrow: 'ACCOUNT',
      body(container) {
        container.append(
          field('显示名称', input('displayName', user?.displayName, { required: true, maxLength: 50 })),
          field('角色', select('role', [{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }], user?.role || 'user')),
        );
        container.append(checkbox('isActive', '允许该账号使用本站', user.isActive));
      },
      async onSave(container) {
        const values = formData(container);
        const payload = { display_name: values.displayName, role: values.role,
          is_active: container.querySelector('[name="isActive"]').checked };
        await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
        await Promise.all([loadUsers(), loadDashboard()]);
      },
    });
  }

  async function loadSettings() {
    const { data } = await api('/api/admin/settings');
    state.settings = data;
    const form = root.querySelector('[data-settings-form]');
    Object.entries(data).forEach(([key, value]) => {
      const control = form.elements[key];
      if (!control) return;
      if (control.type === 'checkbox') control.checked = Boolean(value);
      else control.value = value;
    });
  }

  function parentPath(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
  }

  function renderFileList(container, items, { picker = false } = {}) {
    if (!items.length) return empty(container, '当前目录为空。');
    container.replaceChildren(...items.map((item) => {
      const actions = [];
      if (item.type === 'folder') {
        actions.push(button('打开', 'button-ghost', () => {
          if (picker) loadPickerFiles(item.path);
          else loadFiles(item.path);
        }));
        if (picker) actions.push(button('选择', 'button-primary', () => chooseDirectory(item.path)));
      }
      const size = item.type === 'file' ? `${Math.ceil(item.size / 1024)} KB` : '文件夹';
      return row(item.name, [size, item.url], actions);
    }));
  }

  async function loadFiles(path = state.filePath) {
    const { data, path: current, url } = await api(`/api/admin/files/tree?path=${encodeURIComponent(path)}`);
    state.filePath = current;
    root.querySelector('[data-files-path]').textContent = url;
    root.querySelector('[data-upload-target]').textContent = url;
    renderFileList(root.querySelector('[data-files-list]'), data);
  }

  async function createFolder() {
    const control = root.querySelector('[data-folder-name]');
    const name = control.value.trim();
    if (!name) return toast('请输入文件夹名称', true);
    await api('/api/admin/files/folders', {
      method: 'POST', body: JSON.stringify({ parentPath: state.filePath, name }),
    });
    control.value = '';
    await loadFiles();
    toast('文件夹已创建');
  }

  async function uploadFile() {
    const control = root.querySelector('[data-upload-file]');
    const file = control.files?.[0];
    if (!file) return toast('请选择图片', true);
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('targetPath', state.filePath);
    await api('/api/admin/uploads', { method: 'POST', body });
    control.value = '';
    await loadFiles();
    toast('图片已上传');
  }

  async function uploadFolder() {
    const control = root.querySelector('[data-upload-folder]');
    const files = [...(control.files || [])];
    if (!files.length) return toast('请选择包含图片的文件夹', true);
    const paths = files.map((file) => file.webkitRelativePath || '');
    if (paths.some((path) => !path.includes('/'))) {
      return toast('当前浏览器未提供文件夹相对路径，请使用 Chrome 或 Edge', true);
    }
    const body = new FormData();
    files.forEach((file, index) => {
      body.append('files', file, file.name);
      body.append('relativePaths', paths[index]);
    });
    body.append('targetPath', state.filePath);
    const result = await api('/api/admin/files/folder-upload', { method: 'POST', body });
    control.value = '';
    await loadFiles();
    toast(`已上传 ${result.fileCount} 张图片`);
  }

  async function openDirectoryPicker(target) {
    state.pickerTarget = target;
    state.pickerPath = target.value || '';
    const dialog = root.querySelector('[data-file-picker]');
    dialog.showModal();
    try {
      await loadPickerFiles(state.pickerPath);
    } catch (error) {
      state.pickerPath = '';
      await loadPickerFiles('');
      toast(error.message, true);
    }
  }

  async function loadPickerFiles(path = state.pickerPath) {
    const result = await api(`/api/admin/files/tree?path=${encodeURIComponent(path)}`);
    state.pickerPath = result.path;
    root.querySelector('[data-picker-path]').textContent = result.url;
    root.querySelector('[data-picker-current]').disabled = !result.path;
    renderFileList(root.querySelector('[data-picker-list]'), result.data, { picker: true });
  }

  function chooseDirectory(path = state.pickerPath) {
    if (!path) return;
    if (state.pickerTarget) state.pickerTarget.value = path;
    state.pickerTarget = null;
    root.querySelector('[data-file-picker]').close();
  }

  function initEvents() {
    root.querySelectorAll('[data-admin-view]').forEach((tab) => {
      tab.addEventListener('click', () => activateView(tab.dataset.adminView));
    });
    listen('[data-create-gallery]', 'click', () => editGallery());
    listen('[data-create-category]', 'click', () => editCategory());
    listen('[data-create-announcement]', 'click', () => editAnnouncement());
    listen('[data-create-user]', 'click', () => editUser());
    listen('[data-files-up]', 'click', () => loadFiles(parentPath(state.filePath)).catch((error) => toast(error.message, true)));
    listen('[data-create-folder]', 'click', () => createFolder().catch((error) => toast(error.message, true)));
    listen('[data-upload-button]', 'click', () => uploadFile().catch((error) => toast(error.message, true)));
    listen('[data-upload-folder-button]', 'click', () => uploadFolder().catch((error) => toast(error.message, true)));
    listen('[data-picker-up]', 'click', () => loadPickerFiles(parentPath(state.pickerPath)).catch((error) => toast(error.message, true)));
    listen('[data-picker-current]', 'click', () => chooseDirectory());
    listen('[data-picker-close]', 'click', () => root.querySelector('[data-file-picker]')?.close());
    root.querySelectorAll('[data-dialog-close]').forEach((control) => {
      control.addEventListener('click', (event) => {
        event.preventDefault();
        dialogSave = null;
        root.querySelector('[data-admin-dialog]')?.close();
      });
    });

    const dialogForm = root.querySelector('[data-dialog-form]');
    dialogForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const save = root.querySelector('[data-dialog-save]');
      save.disabled = true;
      try {
        await dialogSave?.(root.querySelector('[data-dialog-body]'));
        root.querySelector('[data-admin-dialog]').close();
        toast('已保存');
      } catch (error) {
        toast(error.message, true);
      } finally {
        save.disabled = false;
      }
    });

    const settingsForm = root.querySelector('[data-settings-form]');
    settingsForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(settingsForm));
      try {
        await api('/api/admin/settings', {
          method: 'PATCH', body: JSON.stringify({
            site_name: values.siteName, site_tagline: values.siteTagline,
            comment_per_minute: Number(values.commentPerMinute),
          }),
        });
        toast('设置已保存，刷新页面后更新站点名称');
      } catch (error) { toast(error.message, true); }
    });

    listen('[data-export]', 'click', async () => {
      try {
        const response = await fetch('/api/admin/export', { credentials: 'same-origin' });
        if (!response.ok) throw new Error((await response.json()).detail || '导出失败');
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement('a');
        link.href = url;
        link.download = 'cas-gallery-export.json';
        link.click();
        URL.revokeObjectURL(url);
        toast('数据已导出');
      } catch (error) { toast(error.message, true); }
    });

    listen('[data-import-file]', 'change', async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const payload = JSON.parse(await file.text());
        const { data } = await api('/api/admin/import', { method: 'POST', body: JSON.stringify(payload) });
        toast(`已导入：${data.categories} 个栏目、${data.galleries} 个图集、${data.announcements} 条公告`);
        await Promise.all([loadCategories(), loadGalleries(), loadAnnouncements(), loadDashboard()]);
      } catch (error) {
        toast(error instanceof SyntaxError ? 'JSON 文件格式不正确' : error.message, true);
      } finally {
        event.target.value = '';
      }
    });
  }

  async function initialize() {
    try {
      const { data: user } = await api('/api/auth/me');
      if (user.role !== 'admin') throw new Error('当前账号不是管理员');
      initEvents();
      await Promise.all([
        loadDashboard(), loadCategories(), loadGalleries(), loadAnnouncements(),
        loadComments(), loadUsers(), loadSettings(), loadFiles(),
      ]);
      root.querySelector('[data-admin-gate]').classList.add('is-hidden');
      root.querySelector('[data-admin-workspace]').classList.remove('is-hidden');
    } catch (error) {
      root.querySelector('[data-admin-gate] h2').textContent = '无法进入管理后台';
      root.querySelector('[data-admin-gate] p').textContent = error.message;
      if (error.status === 401) window.setTimeout(() => window.location.replace('/login?next=/admin'), 1000);
    }
  }

  initialize();
})();
