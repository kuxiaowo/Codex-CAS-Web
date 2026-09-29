ALTER TABLE comments ADD COLUMN root_id INTEGER REFERENCES comments(id) ON DELETE CASCADE;
ALTER TABLE comments ADD COLUMN reply_to_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE local_sessions ADD COLUMN turnstile_verified_at INTEGER;
WITH RECURSIVE roots(id,root_id) AS (
 SELECT id,id FROM comments WHERE parent_id IS NULL
 UNION ALL SELECT c.id,r.root_id FROM comments c JOIN roots r ON c.parent_id=r.id
) UPDATE comments SET root_id=(SELECT root_id FROM roots WHERE roots.id=comments.id);
UPDATE comments SET reply_to_user_id=(SELECT user_id FROM comments p WHERE p.id=comments.parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX idx_comments_gallery_roots ON comments(gallery_id,parent_id,created_at DESC,id DESC);
CREATE INDEX idx_comments_root ON comments(root_id,created_at,id);
CREATE TABLE comment_likes (comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(comment_id,user_id));
CREATE TABLE comment_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE, reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved','dismissed')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT, resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL, UNIQUE(comment_id,reporter_id));
CREATE INDEX idx_comment_reports_status ON comment_reports(status,created_at,id);
CREATE TABLE comment_notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK(kind IN ('reply','like')), recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, comment_id INTEGER NOT NULL, gallery_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, read_at TEXT, UNIQUE(kind,recipient_id,actor_id,comment_id));
CREATE INDEX idx_comment_notifications_recipient ON comment_notifications(recipient_id,kind,id DESC);
