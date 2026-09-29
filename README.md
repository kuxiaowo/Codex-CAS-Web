# Note Gallery

一个以图片为主要内容的前后端一体化图集站。首页按栏目展示真实图片封面，详情页以单列方式阅读扫描图和长图；管理员可管理图集、浏览受控资源目录并上传图片或整个文件夹。

## 技术栈与目录

- FastAPI + Jinja2：页面和 JSON API
- SQLite：账号、栏目、图集目录、公告和留言
- Pillow：仅在上传时校验图片并生成 WebP 缩略图
- Cloudflare R2：通过私有 HMAC Worker 保存图集原图、缩略图和目录摘要
- 原生 HTML / CSS / JavaScript：无需 Node 构建

```text
app/                 FastAPI、认证、数据库、资源扫描与缩略图
templates/           Jinja2 页面模板
static/              前端样式、脚本和站点静态资源
resources/           仅供 development/test 显式本地存储及迁移源使用
tests/               接口、迁移、上传和缩略图测试
```

## 本地运行

```powershell
conda create -n Codex-CAS-Web python=3.12 pip -y
conda activate Codex-CAS-Web
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# 在 NetHub-Accounts 注册客户端后，填入 OIDC_CLIENT_SECRET，并按部署域名设置 OIDC_REDIRECT_URI。
# 首个 CAS 管理员应把其中央 sub 写入 CAS_ADMIN_SUBS；之后也可用 python -m app.bootstrap_admin --sub <sub> 授权已登录成员。
python -m app.main
```

默认地址为 `http://127.0.0.1:3300/`，管理后台为 `/admin`。测试或临时预览必须通过 `APP_PORT` 使用其他端口，例如 `3311`。生产环境必须配置 `MEDIA_STORAGE_BACKEND=r2`、媒体网关地址和独立的 `MEDIA_HMAC_SECRET`；`local` 只允许在 `APP_ENV=development|test` 时显式启用。
回环地址的本地 OIDC 回调可以使用 HTTP；非回环地址必须使用 HTTPS。

Linux 初始化脚本支持 `--no-systemd` 和 `--no-start`，重复执行会复用 Conda 环境、`.env`、数据库和现有 systemd 服务配置。运行脚本前必须先填写 `OIDC_CLIENT_SECRET`。
Caddy 反代示例见 [`docs/Caddyfile.example`](docs/Caddyfile.example)。

在 Accounts 项目中注册生产客户端：

```bash
python -m app.cli register-client \
  --client-id cas \
  --name "Codex CAS" \
  --redirect-uri https://codex.nethub.wiki/api/auth/callback \
  --launch-uri https://codex.nethub.wiki/ \
  --backchannel-logout-uri https://codex.nethub.wiki/api/auth/backchannel-logout
```

客户端密钥只显示一次，应立即写入 CAS 的 `.env`，且不得提交到 Git。

## 图集与资源文件

- 图集只保存简短标题、栏目、资源目录、发布状态和精选状态。
- 图集图片由后台上传至 R2；每个虚拟资源文件夹最多绑定一个图集。页面首批读取 30 张，后续通过游标懒加载。
- 支持 JPG/JPEG、PNG、WebP、GIF。图库只列出所选虚拟目录当前层，并按文件名自然排序。
- 首页封面和详情页只加载压缩后的 WebP 缩略图；用户点击图片打开灯箱时才加载原图。
- 图集图片数量与封面摘要在进程内缓存五分钟；本站后台上传、删除图片会立即清除对应缓存。直接在 R2 外部修改对象时，首页摘要最多延迟五分钟更新。
- 原图写入 `galleries/`，缩略图写入 `thumbnails/`，目录摘要写入 `manifests/`。缩略图只在受控上传时生成；服务启动和运行期间不扫描媒体目录。
- 新建图集、上传单图或上传文件夹时会立即生成首批缩略图，不必等待定时任务。
- 后台可浏览目录、新建文件夹、上传单图或上传整个文件夹，并可删除单张图片；不提供文件夹删除与重命名，删除图集也不会删除实体图片。
- 发布图集前，目录必须存在并包含至少一张可识别图片；草稿允许使用空目录。

上传大小和缩略图参数可在 `.env` 调整：

```dotenv
UPLOAD_MAX_MB=50
THUMBNAIL_MAX_WIDTH=640
THUMBNAIL_MAX_HEIGHT=640
THUMBNAIL_WEBP_QUALITY=82
THUMBNAIL_WEBP_METHOD=6
MEDIA_STORAGE_BACKEND=r2
MEDIA_GATEWAY_URL=https://codex-media.nethub.wiki
```

## 数据迁移与导入导出

数据库结构版本为 v4。旧 v1 数据库首次启动时会删除旧笔记及其留言，保留栏目、公告、站点设置和访问限制，再建立图集表与新的留言关联。接入统一账号时会清除旧密码凭据并停用未绑定中央身份的开发账号；这些历史行仅用于保留业务外键，不会出现在本站成员列表中。

JSON 导入导出格式为 v2，只包含资源相对路径，不包含图片文件。旧 `resources/` 可先用 `python scripts/migrate_media_to_r2.py` 做本地 dry-run；只有显式添加 `--execute` 才会调用网关。迁移默认不覆盖、不删除对象，并使用断点清单、SHA-256、multipart 与最终流式回读校验。v1 笔记数据包不会被兼容导入。

媒体 Worker 的部署、Secret 和冻结协议见 `cloudflare/nethub-codex-media-gateway/README.md`。应用管理接口负责登录、管理员权限、同源 CSRF、扩展名、图片内容与大小校验；浏览器不会接触内部 HMAC Secret。

## 临时关闭评论

将数据库 `settings` 表的 `comments_enabled` 设为 `0`，图集页会隐藏留言表单，后端拒绝新留言和回复（包括管理员）。设为 `1` 即可恢复，无需重启；已有留言数据保留。

## 测试

```powershell
python -m pytest -q
```

测试使用临时数据库和临时资源目录，不启动默认端口。
