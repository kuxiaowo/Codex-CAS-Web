PRAGMA defer_foreign_keys=ON;
ALTER TABLE comments RENAME TO comments_before_moderation;
CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'visible' CHECK(status IN ('visible','hidden','deleted')), created_at TEXT NOT NULL);
INSERT INTO comments SELECT * FROM comments_before_moderation;
DROP TABLE comments_before_moderation;
CREATE INDEX idx_comments_gallery_status ON comments(gallery_id,status);
CREATE TABLE IF NOT EXISTS system_notifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 comment_id INTEGER NOT NULL,
 is_reply INTEGER NOT NULL,
 target_type TEXT NOT NULL,
 target_id INTEGER NOT NULL,
 target_title TEXT NOT NULL,
 reason_codes TEXT NOT NULL,
 reason_note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 read_at TEXT,
 UNIQUE(recipient_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_system_notifications_recipient ON system_notifications(recipient_id,id DESC);
