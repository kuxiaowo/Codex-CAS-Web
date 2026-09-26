import json
from io import BytesIO
import sqlite3
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import app.database as database_module
from app.database import D1GatewayAdapter, transaction


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_d1_execute_maps_rows_and_meta():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    with patch("app.database.urlopen", return_value=_Response({
        "results": [{"rows": [{"id": 1, "name": "demo"}], "meta": {"changes": 0, "last_row_id": None}}]
    })) as mocked:
        cursor = adapter.execute("SELECT * FROM users WHERE id = ?", (1,))
    row = cursor.fetchone()
    assert row["id"] == 1
    assert row[0] == 1
    assert row[1] == "demo"
    assert cursor.fetchone() is None
    request = mocked.call_args.args[0]
    assert request.get_header("X-db-signature")
    assert request.get_header("X-db-request-id")


def test_d1_batch_maps_changes_and_last_row_id():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    with patch("app.database.urlopen", return_value=_Response({
        "results": [{"rows": [], "meta": {"changes": 1, "last_row_id": 9}}]
    })):
        cursor = adapter.batch([{"sql": "INSERT INTO users(username) VALUES (?)", "params": ["a"]}])[0]
    assert cursor.rowcount == 1
    assert cursor.lastrowid == 9


def test_d1_batch_preserves_atomic_oidc_cleanup_and_insert_order():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    with patch("app.database.urlopen", return_value=_Response({
        "results": [
            {"rows": [], "meta": {"changes": 2, "last_row_id": None}},
            {"rows": [], "meta": {"changes": 1, "last_row_id": 7}},
        ]
    })) as mocked:
        adapter.batch([
            {"sql": "DELETE FROM oidc_login_states WHERE expires_at <= ?", "params": ["now"]},
            {"sql": "INSERT INTO oidc_login_states (state_hash) VALUES (?)", "params": ["hash"]},
        ])
    payload = json.loads(mocked.call_args.args[0].data)
    assert payload["mode"] == "batch"
    assert [item["sql"] for item in payload["statements"]] == [
        "DELETE FROM oidc_login_states WHERE expires_at <= ?",
        "INSERT INTO oidc_login_states (state_hash) VALUES (?)",
    ]


def test_d1_adapter_has_no_fake_commit_or_rollback():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    assert not hasattr(adapter, "commit")
    assert not hasattr(adapter, "rollback")


def test_d1_maps_gateway_constraint_conflict():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    error = HTTPError(
        adapter.url, 409, "Conflict", {},
        BytesIO(json.dumps({
            "error": "database_conflict",
            "message": "Database constraint rejected the write",
        }).encode()),
    )
    with patch("app.database.urlopen", side_effect=error):
        with pytest.raises(database_module.D1IntegrityError, match="constraint"):
            adapter.execute("INSERT INTO users(username) VALUES (?)", ("a",))


def test_d1_rejects_incomplete_batch_response():
    adapter = D1GatewayAdapter("https://db.example.test", "x" * 32)
    with patch("app.database.urlopen", return_value=_Response({"results": []})):
        with pytest.raises(database_module.D1DatabaseError, match="结果数量"):
            adapter.batch([{"sql": "DELETE FROM users WHERE id = ?", "params": [1]}])


def test_sqlite_transaction_still_rolls_back(tmp_path):
    path = tmp_path / "transaction.sqlite3"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    setup.commit()
    setup.close()

    with patch("app.database.connect", side_effect=lambda: sqlite3.connect(path)):
        with pytest.raises(RuntimeError, match="abort"):
            with transaction() as connection:
                connection.execute("INSERT INTO items(name) VALUES (?)", ("temporary",))
                raise RuntimeError("abort")

    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        check.close()
