# CAS Gallery

一个以图片为主要内容的前后端一体化图集站。首页按栏目展示真实图片封面，详情页以单列方式阅读扫描图和长图；管理员可管理图集、浏览受控资源目录并上传图片或整个文件夹。

## 技术栈与目录

- FastAPI + Jinja2：页面和 JSON API
- SQLite：账号、栏目、图集目录、公告和留言
- Pillow：按需生成 WebP 缩略图
- 原生 HTML / CSS / JavaScript：无需 Node 构建

```text
app/                 FastAPI、认证、数据库、资源扫描与缩略图
templates/           Jinja2 页面模板
static/              前端样式、脚本和站点静态资源
resources/           用户图片资源（内容不纳入 Git）
tests/               接口、迁移、上传和缩略图测试
```

## 本地运行

```powershell
conda create -n Codex-CAS-Web python=3.12 pip -y
conda activate Codex-CAS-Web
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
# 将输出写入 .env 的 AUTH_SECRET_KEY
python -m app.bootstrap_admin --username admin --display-name "管理员"
python -m app.main
```

默认地址为 `http://127.0.0.1:3300/`，管理后台为 `/admin`。测试或临时预览必须通过 `APP_PORT` 使用其他端口，例如 `3311`。

## 图集与资源文件

- 图集只保存简短标题、栏目、资源目录、发布状态和精选状态。
- 图片放在项目根目录 `resources/` 的子文件夹中；每个子文件夹最多绑定一个图集。
- 支持 JPG/JPEG、PNG、WebP、GIF。图库只扫描所选目录当前层，并按文件名自然排序。
- 首页封面自动使用第一张图片；详情页展示全部缩略图，点击后灯箱加载原图。
- 缩略图按需写入源图旁的 `.thumbs/`，源图更新后自动重建。
- 后台可浏览目录、新建文件夹、上传单图或上传整个文件夹；不提供删除与重命名，删除图集也不会删除实体图片。
- 发布图集前，目录必须存在并包含至少一张可识别图片；草稿允许使用空目录。

上传大小和缩略图参数可在 `.env` 调整：

```dotenv
UPLOAD_MAX_MB=50
THUMBNAIL_MAX_WIDTH=1600
THUMBNAIL_MAX_HEIGHT=4000
THUMBNAIL_WEBP_QUALITY=82
THUMBNAIL_WEBP_METHOD=6
```

## 数据迁移与导入导出

数据库结构版本为 v2。旧 v1 数据库首次启动时会删除旧笔记及其留言，保留账号、栏目、公告、站点设置和访问限制，再建立图集表与新的留言关联。

JSON 导入导出格式为 v2，只包含资源相对路径，不包含图片文件。迁移站点时需要另外复制 `resources/`。v1 笔记数据包不会被兼容导入。

## 测试

```powershell
python -m pytest -q
```

测试使用临时数据库和临时资源目录，不启动默认端口。
