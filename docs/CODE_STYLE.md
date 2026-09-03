# 代码规范

## 总体原则

- 页面、API 和静态资源由同一个 FastAPI 服务与端口提供，但模板、样式、脚本和数据访问仍分目录维护。
- 所有文本文件统一使用 UTF-8 和 LF；缩进遵循根目录 `.editorconfig`。
- 环境差异通过 `.env` 管理，不在业务代码里写死部署参数。
- 注释解释模块职责和设计原因，不逐行复述代码。

## 后端

- 路由集中在 `app/main.py`，数据库连接与初始化集中在 `app/database.py`。
- SQL 必须使用参数绑定，不将用户输入拼接进 SQL。
- 密码只保存 PBKDF2-HMAC-SHA256 哈希；API 不返回密码哈希。
- JSON 字段使用 `camelCase`，数据库字段和 Python 函数使用 `snake_case`。
- 管理接口统一使用 `/api/admin/...`，并依赖管理员权限校验。
- 新接口或数据约束变更必须同步更新 README 或 API 文档。

## 前端

- HTML 模板只描述结构；交互放在 `static/js/`，样式放在 `static/css/`。
- API 返回内容进入页面时优先使用 `textContent`，不得直接拼接未转义数据到 `innerHTML`。
- 交互必须支持键盘焦点、窄屏布局和 `prefers-reduced-motion`。
- 卡片、按钮、表单使用已有设计 token，不为单个页面随意引入新颜色和尺寸。

