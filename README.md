# CAS Notes

一个前后端一体化的学习笔记信息站。左侧固定导航包含品牌、搜索、栏目和账户信息；主要内容以卡片呈现。管理员后台支持用户、栏目、学习笔记、公告、留言审核、JSON 数据迁移及访问限制，不包含文件管理。

## 技术栈

- FastAPI + Jinja2：页面与 JSON API
- 原生 HTML / CSS / JavaScript：无需 Node 构建
- SQLite：单文件数据库，启动时自动创建结构与内置内容
- Conda：Python 环境；依赖使用 pip 的 `requirements.txt` 维护

## 目录

```text
app/                 FastAPI、认证、SQLite 和输入模型
templates/           Jinja2 页面模板
static/css/          前台与后台样式
static/js/           前台与后台交互
scripts/init_linux.sh
tests/
requirements.txt
requirements-dev.txt
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

默认访问地址为 `http://127.0.0.1:3300/`，管理后台为 `/admin`，接口文档为 `/docs`。服务页面、静态资源和 API 均使用同一端口。

测试时请通过环境变量覆盖 `APP_PORT`，不要占用默认端口。例如：

```powershell
$env:APP_PORT = "3311"
python -m app.main
```

## Linux 初始化

脚本不需要 root，会创建或复用 `.env` 中 `CONDA_ENV_NAME` 指定的环境，并安装一个 systemd 用户服务：

```bash
cp .env.example .env
bash scripts/init_linux.sh --admin admin --display-name "管理员"
```

脚本会补充新配置、生成独立认证密钥、安装 requirements、初始化 SQLite，并校验及启动用户服务。它不会创建默认密码，也不会暗中执行 `sudo`。

## 数据与安全约定

- 首次建库会创建“学习笔记”栏目和四条演示笔记，不创建任何账号。
- 首个管理员必须通过交互式命令创建；已有启用中的管理员后，引导命令会拒绝再次执行。
- Markdown 正文渲染后会经过 HTML 白名单清理。
- 登录、注册和留言使用数据库持久化限流，管理员可在后台调整额度。
- JSON 导入只补充不存在的栏目和笔记；同标识内容会跳过，不覆盖已有数据。

视觉方向参考了 [Linear Docs](https://linear.app/docs) 的侧栏层级、快捷搜索和卡片节奏，并针对中文学习内容重新设计了渐变色、卡片封面与阅读页。

