# 画廊评论互动与举报

Codex-CAS-Web 的在线读写仍使用 SQLite；D1 只接收业务表镜像。v6 增加评论根关系、点赞、举报、互动通知和登录会话的人机验证时间。既有 AI 审核任务与评论写入保持同一事务，AI 只临时隐藏违规内容。

## 接口

- `GET /api/galleries/{gallery_id}/comments?sort=hot|latest&page=1&pageSize=10`：按根评论分页，返回平铺的根评论与回复、`total` 和 `hasMore`。`GET /api/comments/{id}/context` 用于消息链接定位。
- `POST|DELETE /api/comments/{id}/like`：登录用户点赞或取消；重复操作幂等。
- `POST /api/comments/{id}/reports`：登录用户举报他人的可见留言，`reason` 为 1–300 字，`turnstileToken` 必须是独立的 `comment-report` 验证令牌。同一用户再次举报会更新理由。
- `GET /api/comment-notifications?kind=reply|like` 与 `POST /api/comment-notifications/read`：按用户隔离，隐藏、删除或不可访问的内容不返回正文与链接。
- `GET /api/admin/comment-reports` 与 `DELETE /api/admin/comment-reports/{id}/content`：管理员查看待处理举报，并以现有原因选择流程删除；删除和举报结案在同一事务完成。
- `GET /api/turnstile/comment-config`：返回当前登录会话是否仍处于固定 3600 秒的评论验证窗口。成功发布才写入首次验证时间，窗口内发布不续期；举报每次单独验证。

## 数据迁移与镜像

新库直接使用 v6 schema。未安装镜像捕获的本地 v5 库可在启动时升级。已安装捕获的库必须停止 CAS API 和镜像交付后，先备份，再运行：

`python -m scripts.migrate_comment_interactions --db <sqlite> --backup <new-backup>`

脚本保留 outbox、重建捕获触发器，并使交付保持未启用。按既有镜像流程检查 D1 实际结构及备份，应用 `sql/d1/006_comment_interactions.sql`，重新生成 SQLite 快照，按旧水位对账并核验，再 arm 和恢复交付。不要把 D1 切为在线库。迁移后检查 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`、根评论回填、举报与通知表及捕获触发器。
