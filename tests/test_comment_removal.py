import sqlite3

from scripts.remove_comments import migrate


def test_offline_removal_preserves_gallery_and_backup(tmp_path):
    path, backup = tmp_path / "cas.db", tmp_path / "backup.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE galleries(id INTEGER PRIMARY KEY,title TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE local_sessions(id INTEGER PRIMARY KEY,turnstile_verified_at INTEGER);
        CREATE TABLE comments(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES comments(id));
        CREATE TABLE comment_likes(comment_id INTEGER REFERENCES comments(id));
        INSERT INTO galleries VALUES(1,'preserved');
        INSERT INTO comments VALUES(1,NULL),(2,1);
        INSERT INTO comment_likes VALUES(2);
        INSERT INTO settings VALUES('comments_enabled','1'),('site_name','preserved');
        PRAGMA user_version=6;
        """)
    assert migrate(path, backup) == {"version": 7, "mirrorDisarmed": False}
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 2
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT title FROM galleries").fetchone()[0] == "preserved"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%comment%'").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [r[1] for r in db.execute("PRAGMA table_info(local_sessions)")] == ["id"]


def test_all_comment_routes_are_absent():
    from app.main import app
    paths = [route.path for route in app.routes]
    assert not any(any(word in path for word in ("comment", "moderation", "messages", "turnstile")) for path in paths)
