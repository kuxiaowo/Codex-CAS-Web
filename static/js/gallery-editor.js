(() => {
  'use strict';

  window.createGalleryPages = ({ container, gallery, api, field, select, directoryField, button, text }) => {
    let id = gallery?.id;
    let directory = gallery?.resourceDir || '';
    let pages = [];
    let loading = false;
    let loadError = null;
    let loadVersion = 0;
    let dragged = null;
    const urls = new Set();
    const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
    const panel = document.createElement('section');
    panel.className = 'gallery-page-editor';
    const source = select('imageSource', [
      { value: 'upload', label: '从电脑上传图片' },
      { value: 'existing', label: '选择已有资源文件夹' },
    ], gallery ? 'existing' : 'upload');
    if (!gallery) container.append(field('图片来源', source));
    const directoryControl = directoryField(directory);
    const directoryInput = directoryControl.querySelector('input');
    directoryInput.required = false;
    container.append(directoryControl);
    const controls = document.createElement('div');
    controls.className = 'page-upload-controls';
    const makePicker = (label, folder) => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = 'image/jpeg,image/png,image/webp,image/gif';
      input.multiple = true;
      input.dataset.pagePicker = folder ? 'folder' : 'files';
      if (folder) input.setAttribute('webkitdirectory', '');
      input.addEventListener('change', () => {
        try {
          const incoming = [...input.files].sort((a, b) => collator.compare(a.name, b.name));
          const names = new Set(pages.map((page) => page.name.toLowerCase()));
          for (const file of incoming) {
            const name = file.name.normalize('NFC').trim();
            if (!/\.(jpe?g|png|webp|gif)$/i.test(name)) throw new Error(`不支持的图片格式：${file.name}`);
            if (file.size > 50 * 1024 * 1024) throw new Error(`图片超过 50 MiB：${file.name}`);
            if (names.has(name.toLowerCase())) throw new Error(`存在同名图片，请先修改文件名：${file.name}`);
            names.add(name.toLowerCase());
          }
          for (const file of incoming) {
            const url = URL.createObjectURL(file);
            urls.add(url);
            pages.push({ name: file.name.normalize('NFC').trim(), file, thumbSrc: url, uploaded: false });
          }
          status.textContent = '已添加图片，可拖拽或使用前移、后移调整页序。';
          render();
        } catch (error) { status.textContent = error.message; }
        input.value = '';
      });
      return field(label, input);
    };
    controls.append(makePicker('选择多张图片', false), makePicker('选择图片文件夹', true));
    const status = text('p', '', 'page-upload-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const progress = document.createElement('progress');
    progress.hidden = true;
    const list = document.createElement('ol');
    list.className = 'gallery-page-list';
    list.setAttribute('aria-label', '笔记图片页序');
    const sort = button('按文件名重新排序', 'button-ghost', () => {
      pages.sort((a, b) => collator.compare(a.name, b.name));
      render();
    });
    panel.append(controls, text('p', '默认按文件名自然排序；可拖拽或使用按钮调整。追加图片放在末尾，保存后生效。', 'page-editor-help'), sort, status, progress, list);
    container.append(panel);

    function move(from, to) {
      if (to < 0 || to >= pages.length || from === to) return;
      pages.splice(to, 0, pages.splice(from, 1)[0]);
      render();
      list.children[to]?.querySelector('button')?.focus();
    }

    function render() {
      list.replaceChildren(...pages.map((page, index) => {
        const item = document.createElement('li');
        item.draggable = true;
        item.dataset.pageName = page.name;
        const image = document.createElement('img');
        image.src = page.thumbSrc;
        image.alt = '';
        image.loading = 'lazy';
        const info = text('div', '', 'page-item-info');
        info.append(text('strong', `第 ${index + 1} 页 · ${page.name}`), text('small', page.error || (page.uploaded ? '已上传' : '待上传')));
        const actions = text('div', '', 'page-item-actions');
        const previous = button('前移', 'button-ghost', () => move(index, index - 1));
        previous.disabled = index === 0;
        previous.setAttribute('aria-label', `前移 ${page.name}`);
        const next = button('后移', 'button-ghost', () => move(index, index + 1));
        next.disabled = index === pages.length - 1;
        next.setAttribute('aria-label', `后移 ${page.name}`);
        actions.append(previous, next);
        if (!page.uploaded) actions.append(button('移除', 'button-ghost', () => {
          urls.delete(page.thumbSrc);
          URL.revokeObjectURL(page.thumbSrc);
          pages.splice(index, 1);
          render();
        }));
        item.append(image, info, actions);
        item.addEventListener('dragstart', (event) => {
          dragged = page;
          event.dataTransfer.setData('text/plain', page.name);
          event.dataTransfer.effectAllowed = 'move';
        });
        item.addEventListener('dragover', (event) => event.preventDefault());
        item.addEventListener('drop', (event) => {
          event.preventDefault();
          if (dragged) move(pages.indexOf(dragged), pages.indexOf(page));
          dragged = null;
        });
        item.addEventListener('dragend', () => { dragged = null; });
        return item;
      }));
    }

    async function loadDirectory() {
      const version = ++loadVersion;
      loading = true;
      loadError = null;
      pages = [];
      render();
      const path = directoryInput.value;
      try {
        if (path) {
          const result = gallery && path === gallery.resourceDir
            ? await api(`/api/admin/galleries/${gallery.id}/images`)
            : await api(`/api/admin/files/images?path=${encodeURIComponent(path)}`);
          if (version !== loadVersion) return;
          pages = result.data.map((page) => ({ ...page, uploaded: true }));
          status.textContent = `已载入 ${pages.length} 张图片。`;
        }
      } catch (error) {
        if (version === loadVersion) { loadError = error; status.textContent = error.message; }
      } finally {
        if (version === loadVersion) { loading = false; render(); }
      }
    }
    directoryInput.addEventListener('change', loadDirectory);
    function updateSource() {
      directoryControl.hidden = source.value !== 'existing';
      panel.hidden = source.value === 'existing' && !directoryInput.value;
      if (source.value === 'existing') loadDirectory();
      else { ++loadVersion; loading = false; loadError = null; pages = []; render(); }
    }
    source.addEventListener('change', updateSource);
    directoryInput.addEventListener('change', () => { panel.hidden = false; });
    updateSource();

    return {
      dispose() { ++loadVersion; urls.forEach((url) => URL.revokeObjectURL(url)); },
      async save(payload) {
        if (loading) throw new Error('图片列表正在加载，请稍候保存');
        if (loadError) throw loadError;
        if (source.value === 'existing' && !directoryInput.value) throw new Error('请选择资源文件夹');
        if (payload.status === 'published' && !pages.length) throw new Error('发布前请至少添加一张图片');
        if (gallery && directoryInput.value !== directory && pages.some((page) => !page.uploaded)) {
          throw new Error('请先保存资源目录变更，再追加图片');
        }
        if (!id) {
          if (source.value === 'upload') {
            const result = await api('/api/admin/galleries/drafts', { method: 'POST', body: JSON.stringify(payload) });
            id = result.data.id;
            directory = result.data.resourceDir;
          } else {
            directory = directoryInput.value;
            const result = await api('/api/admin/galleries', { method: 'POST', body: JSON.stringify({ ...payload, status: 'draft', resource_dir: directory }) });
            id = result.data.id;
          }
          source.disabled = true;
          directoryControl.querySelector('button').disabled = true;
        }
        const pending = pages.filter((page) => !page.uploaded);
        progress.max = pending.length || 1;
        progress.value = 0;
        progress.hidden = pending.length === 0;
        for (const [index, page] of pending.entries()) {
          status.textContent = `正在上传 ${index + 1}/${pending.length}：${page.name}。请勿关闭窗口。`;
          page.error = '';
          const body = new FormData();
          body.append('file', page.file, page.name);
          try {
            await api(`/api/admin/galleries/${id}/images`, { method: 'POST', body });
            page.uploaded = true;
            progress.value = index + 1;
          } catch (error) {
            page.error = '上传失败，可重试';
            render();
            status.textContent = `上传中断：${page.name}。已完成的图片保存在笔记中，点击保存可重试。`;
            throw new Error(`${page.name}：${error.message}`);
          }
          render();
        }
        status.textContent = '正在保存笔记与页序…';
        await api(`/api/admin/galleries/${id}`, { method: 'PATCH', body: JSON.stringify({
          ...payload, resource_dir: source.value === 'existing' ? directoryInput.value : directory,
          image_order: pages.map((page) => page.name),
        }) });
        status.textContent = '已保存';
      },
    };
  };
})();
