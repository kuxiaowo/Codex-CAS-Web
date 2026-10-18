"""Optional real-browser integration check: python -m tests.browser_gallery_smoke.

Requires Playwright and its Chromium runtime; production credentials are never used.
"""
from pathlib import Path
import os
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright

from tests.test_app import AppTest, image_bytes


def run():
    os.environ['OIDC_REDIRECT_URI'] = 'http://127.0.0.1:3317/api/auth/callback'
    AppTest.setUpClass()
    server = None
    try:
        from app.auth import SESSION_COOKIE
        from app.database import utc_now
        with AppTest.database.transaction() as db:
            db.execute('UPDATE users SET created_at=?', (utc_now(),))
        for route in AppTest.main.app.routes:
            if getattr(route, 'path', None) == '/resources':
                route.app.directory = str(AppTest.resource_dir)
                route.app.all_directories = [str(AppTest.resource_dir)]
        server = uvicorn.Server(uvicorn.Config(AppTest.main.app, host='127.0.0.1', port=3317, log_level='error'))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=os.environ.get('CAS_BROWSER_CHANNEL') or None)
            context = browser.new_context(viewport={'width': 1365, 'height': 960})
            context.add_cookies([{'name': SESSION_COOKIE, 'value': AppTest.client.cookies.get(SESSION_COOKIE), 'url': 'http://127.0.0.1:3317'}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:3317/admin')
            page.wait_for_function("document.querySelector('[data-admin-workspace]').classList.contains('is-hidden') === false || document.querySelector('[data-admin-gate] h2').textContent === '无法进入管理后台' || !!window.__browserError", timeout=10000)
            assert page.locator('[data-admin-workspace]').is_visible(), (page.locator('[data-admin-gate]').inner_text(), errors)
            page.locator('[data-admin-view="galleries"]').click()
            page.locator('[data-create-gallery]').click()
            dialog = page.locator('[data-admin-dialog]')
            dialog.locator('[name="title"]').fill('浏览器上传验收')
            dialog.locator('[name="status"]').select_option('published')
            dialog.locator('[data-page-picker="files"]').set_input_files([
                {'name': name, 'mimeType': 'image/jpeg', 'buffer': image_bytes()}
                for name in ('10.jpg', '2.jpg', '1.jpg')
            ])
            def names():
                return dialog.locator('[data-page-name]').evaluate_all('(items) => items.map(item => item.dataset.pageName)')
            assert names() == ['1.jpg', '2.jpg', '10.jpg']
            dialog.get_by_role('button', name='前移 10.jpg', exact=True).click()
            dialog.get_by_role('button', name='前移 10.jpg', exact=True).click()
            assert names() == ['10.jpg', '1.jpg', '2.jpg']
            # Interrupt one file; a second Save retries only unfinished pages.
            interrupted = [False]
            posts = []
            def upload_route(route):
                request = route.request
                if request.method == 'POST':
                    body = request.post_data_buffer
                    posts.append(body)
                    if b'filename="1.jpg"' in body and not interrupted[0]:
                        interrupted[0] = True
                        route.fulfill(status=503, content_type='application/json', body='{"detail":"模拟网络中断"}')
                        return
                route.continue_()
            page.route('**/api/admin/galleries/*/images', upload_route)
            dialog.locator('[data-dialog-save]').click()
            page.wait_for_function("!document.querySelector('[data-dialog-save]').disabled", timeout=10000)
            assert '上传中断' in dialog.locator('.page-upload-status').inner_text(), (dialog.locator('.page-upload-status').inner_text(), page.locator('[data-toast-region]').inner_text(), len(posts), errors)
            assert dialog.is_visible()
            records = context.request.get('http://127.0.0.1:3317/api/admin/galleries').json()['data']
            record = next(item for item in records if item['title'] == '浏览器上传验收')
            assert record['status'] == 'draft' and record['imageCount'] == 1
            dialog.locator('[data-dialog-save]').click()
            dialog.wait_for(state='hidden')
            assert sum(b'filename="10.jpg"' in body for body in posts) == 1
            records = context.request.get('http://127.0.0.1:3317/api/admin/galleries').json()['data']
            record = next(item for item in records if item['title'] == '浏览器上传验收')
            assert record['status'] == 'published'
            assert record['imageOrder'] == ['10.jpg', '1.jpg', '2.jpg']
            # Reopen at mobile width, append, and drag a page to the front.
            page.locator('[data-galleries-list] .admin-row').filter(has_text='浏览器上传验收').get_by_role('button', name='编辑', exact=True).click()
            dialog.locator('[data-page-name]').first.wait_for()
            page.set_viewport_size({'width': 390, 'height': 844})
            dialog.locator('[data-page-picker="files"]').set_input_files([{'name': 'new.jpg', 'mimeType': 'image/jpeg', 'buffer': image_bytes(color='red')}])
            dialog.locator('[data-page-name="new.jpg"]').drag_to(dialog.locator('[data-page-name="10.jpg"]'))
            assert names() == ['new.jpg', '10.jpg', '1.jpg', '2.jpg']
            box = dialog.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= 391
            screenshot = Path(tempfile.gettempdir()) / 'cas-gallery-editor-mobile.png'
            page.screenshot(path=str(screenshot))
            dialog.locator('[data-dialog-save]').click()
            dialog.wait_for(state='hidden')
            page.goto(f"http://127.0.0.1:3317/galleries/{record['id']}")
            assert page.locator('[data-gallery-image]').first.locator('img').get_attribute('data-original-src').endswith('/new.jpg')
            page.locator('[data-gallery-image]').first.click()
            page.locator('[data-gallery-lightbox][open]').wait_for()
            assert page.locator('[data-gallery-lightbox] img').get_attribute('src').endswith('/new.jpg')
            page.goto('http://127.0.0.1:3317/admin')
            page.locator('[data-admin-view="galleries"]').click()
            page.locator('[data-create-gallery]').click()
            dialog.locator('[name="title"]').fill('文件夹上传验收')
            folder = AppTest.root / 'incoming-folder'
            folder.mkdir()
            for name in ('20.jpg', '3.jpg'):
                (folder / name).write_bytes(image_bytes())
            dialog.locator('[data-page-picker="folder"]').set_input_files(str(folder))
            assert names() == ['3.jpg', '20.jpg']
            dialog.locator('[data-dialog-save]').click()
            dialog.wait_for(state='hidden')
            context.request.post('http://127.0.0.1:3317/api/admin/files/folders', data={'name': 'existing-unbound'})
            upload = context.request.post('http://127.0.0.1:3317/api/admin/uploads', multipart={
                'targetPath': 'existing-unbound',
                'file': {'name': 'existing.jpg', 'mimeType': 'image/jpeg', 'buffer': image_bytes()},
            })
            assert upload.status == 201
            page.locator('[data-create-gallery]').click()
            dialog.locator('[name="title"]').fill('已有目录验收')
            dialog.locator('[name="imageSource"]').select_option('existing')
            dialog.get_by_role('button', name='浏览', exact=True).click()
            page.locator('[data-picker-list] .admin-row').filter(has_text='existing-unbound').get_by_role('button', name='选择', exact=True).click()
            dialog.locator('[data-page-name="existing.jpg"]').wait_for()
            dialog.locator('[data-dialog-save]').click()
            dialog.wait_for(state='hidden')
            assert not errors, errors
            browser.close()
            print('Browser integration passed: selection, folder, existing directory, natural order, move, retry, draft, append, drag, mobile, reading, lightbox')
            print(f'Mobile screenshot: {screenshot}')
    finally:
        if server:
            server.should_exit = True
            worker.join(5)
        AppTest.tearDownClass()


if __name__ == '__main__':
    run()
