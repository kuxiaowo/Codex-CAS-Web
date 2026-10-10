-- Offline mirror upgrade only; apply after backup and with delivery disarmed.
DROP TABLE IF EXISTS system_notifications;
DROP TABLE IF EXISTS comment_notifications;
DROP TABLE IF EXISTS comment_reports;
DROP TABLE IF EXISTS comment_likes;
DROP TABLE IF EXISTS comments;
DELETE FROM settings WHERE key IN ('comments_enabled','comment_per_minute');
ALTER TABLE local_sessions DROP COLUMN turnstile_verified_at;
