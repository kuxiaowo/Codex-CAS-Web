"""Offline CAS v5 -> v6 migration with backup and D1 capture refresh.

Stop CAS API and mirror delivery first. Delivery remains disarmed until a new
snapshot and D1 baseline have been reconciled and verified.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from app.database import MIGRATE_V5_TO_V6
from scripts import d1_mirror


def statements(sql: str):
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def migrate(path: Path, backup: Path) -> dict:
    if backup.exists():
        raise FileExistsError(backup)
    connection = d1_mirror.connect(path, write=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 5:
            raise RuntimeError("CAS database must be at v5")
        destination = d1_mirror.connect(backup, write=True)
        try:
            connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
        finally:
            destination.close()
        backup.chmod(0o600)
        mirrored = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name='_sync_control'").fetchone())
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if mirrored:
                connection.execute("UPDATE _sync_control SET ready=0 WHERE id=1")
                triggers = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB '_sync_*'")]
                for name in triggers:
                    connection.execute("DROP TRIGGER " + d1_mirror.quote(name))
            for statement in statements(MIGRATE_V5_TO_V6):
                connection.execute(statement)
            if mirrored:
                for sql in d1_mirror.expected_triggers(connection).values():
                    connection.execute(sql)
                d1_mirror.verify_capture(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Foreign key check failed")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Migrated database integrity check failed")
        return {"version":6,"mirrorDisarmed":mirrored,"backup":str(backup)}
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.db,args.backup)))
