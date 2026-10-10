"""Offline v1-v6 removal. Stop application and mirror delivery first."""

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

from app.database import (
    MIGRATE_REMOVE_COMMENTS,
    MIGRATE_V1_TO_V2,
    MIGRATE_V2_TO_V3,
    MIGRATE_V3_TO_V4,
)


def execute_sql(db, source):
    statement = ""
    for line in source.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def migrate(database_path, backup_path, mirror_module=None):
    database_path, backup_path = Path(database_path), Path(backup_path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    if backup_path.exists():
        raise FileExistsError(backup_path)
    db = sqlite3.connect(database_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in range(1, 7):
            raise RuntimeError(f"Expected schema 1–6, found {version}")
        mirrored = bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='_sync_control'"
            ).fetchone()
        )
        mirror = None
        if mirrored:
            if not mirror_module:
                raise RuntimeError(
                    "Mirrored database requires verified --mirror-module"
                )
            spec = importlib.util.spec_from_file_location(
                "governance_mirror", mirror_module
            )
            mirror = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mirror)
            mirror.verify_capture(db)
            if any(name.startswith("_private_") for name in mirror.tables(db)):
                raise RuntimeError("Mirror tool must exclude _private_ tables")
        # Exclusive creation avoids overwriting another backup, even in a race.
        with backup_path.open("xb"):
            pass
        with sqlite3.connect(backup_path) as backup:
            db.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
        backup_path.chmod(0o600)
        db.execute("BEGIN IMMEDIATE")
        try:
            if mirrored:
                db.execute("UPDATE _sync_control SET ready=0 WHERE id=1")
                names = [
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB '_sync_*'"
                    )
                ]
                for name in names:
                    db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            if version == 1:
                execute_sql(db, MIGRATE_V1_TO_V2)
            if version <= 2:
                execute_sql(db, MIGRATE_V2_TO_V3)
            if version <= 3:
                execute_sql(db, MIGRATE_V3_TO_V4)
            execute_sql(db, MIGRATE_REMOVE_COMMENTS)
            columns = {r[1] for r in db.execute("PRAGMA table_info(local_sessions)")}
            if "turnstile_verified_at" in columns:
                db.execute(
                    "ALTER TABLE local_sessions DROP COLUMN turnstile_verified_at"
                )
            if mirrored:
                for statement in mirror.expected_triggers(db).values():
                    db.execute(statement)
                mirror.verify_capture(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Migrated database foreign key check failed")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return {"version": 7, "mirrorDisarmed": mirrored}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--mirror-module", type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, args.backup, args.mirror_module)))
