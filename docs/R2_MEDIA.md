# R2 媒体上线与回滚

应用只消费 `nethub-codex-media-gateway` 已冻结的 HMAC v1 接口。生产环境不允许回退到本地媒体目录。

## 上线顺序

1. 在 Cloudflare 创建私有 Standard/APAC bucket `nethub-codex-media`。
2. 在 Worker 目录执行测试和 `wrangler deploy --dry-run`，再配置独立 Secret：`wrangler secret put HMAC_SECRET`。
3. 部署 `nethub-codex-media-gateway`，绑定 `codex-media.nethub.wiki`，确认 bucket 没有公开 `r2.dev`。
4. 在应用服务器配置 `APP_ENV=production`、`MEDIA_STORAGE_BACKEND=r2`、`MEDIA_GATEWAY_URL=https://codex-media.nethub.wiki` 及同一份 `MEDIA_HMAC_SECRET`。
5. 先运行 `python scripts/migrate_media_to_r2.py --source resources` 查看 dry-run。核对对象清单后才可添加 `--execute`。
6. 检查断点清单和最终回读校验，再重启应用。启动过程只初始化数据库，不扫描 `resources`，也不会调用 Pillow。

迁移命令只校验 R2 媒体所需的 backend、网关 URL、HMAC Secret 和协议上限，不要求配置 OIDC 或数据库生产参数；应用启动仍执行包括 OIDC 在内的完整 fail-closed 校验。

迁移工具始终串行处理对象 key；已存在且 SHA-256 相同的对象会跳过，内容不同则立即停止。它没有覆盖或删除开关。源目录中的文件符号链接、目录链接、junction 或其他 reparse point 会使迁移停止，解析后的源文件必须仍位于所选源目录内。

## 对象布局

- `galleries/{图集路径}/{文件名}`：公开原图。
- `thumbnails/{图集路径}/{文件名}.webp`：公开缩略图。
- `manifests/{图集路径}.json`：内部目录摘要。

代码内的 Logo、CSS、JavaScript 和 favicon 仍随应用发布，不进入 R2。

## 已知协议边界

- multipart 完成后 Worker 不写对象 SHA-256 元数据；迁移工具会通过 `/media/` 流式回读原图和缩略图做最终哈希校验。
- manifest 没有内部 GET，无法做正文回读；小型 manifest 使用直接 PUT，并以 HEAD 返回的 SHA-256 元数据校验。
- Worker 禁止覆盖 manifest。应用更新摘要时只能按 key 在单进程内串行执行“删除旧对象后写入新对象”，多实例同时写同一目录时不是原子的。上线阶段应保持单写实例，或在后续协议版本加入条件替换/内部 GET。
- R2 multipart complete 没有原子 `If-None-Match`。应用和迁移工具都串行同 key，但跨实例并发仍必须由部署层避免。

## 回滚

应用配置回滚不删除任何 R2 对象。若尚未切换生产，可继续保留原 `resources`；生产配置不能改成 `local`。需要恢复数据时先停止写入，从 R2 导出并校验，禁止用迁移工具反向覆盖。
