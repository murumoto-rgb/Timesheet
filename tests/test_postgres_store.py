import os
import secrets
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest
import certifi
from cryptography.fernet import Fernet
from fastapi import HTTPException

import postgres_store
from postgres_store import PostgresStore, TABLE


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=()):
        self.connection.queries.append((query, params))
        if query.startswith("SELECT data FROM"):
            data = self.connection.rows.get(params[0])
            self.result = None if data is None else (data,)
        elif query.startswith("SELECT id, data FROM"):
            self.all_results = sorted(self.connection.rows.items())
        elif query.startswith("SELECT EXISTS"):
            self.result = (bool(self.connection.rows),)
        elif query.startswith("INSERT INTO"):
            self.connection.rows[params[0]] = params[1]
        elif query.startswith("LOCK TABLE"):
            pass
        elif query.startswith("SELECT pg_advisory_lock"):
            self.connection.active_locks += 1
        elif query.startswith("SELECT pg_advisory_unlock"):
            self.connection.active_locks -= 1

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.all_results

    def executemany(self, query, rows):
        self.connection.queries.append((query, rows))
        for blob_id, data in rows:
            self.connection.rows[blob_id] = data


class FakeConnection:
    def __init__(self):
        self.rows = {}
        self.queries = []
        self.active_locks = 0
        self.closed = False
        self.transaction_count = 0

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    @contextmanager
    def transaction(self):
        before = self.rows.copy()
        self.transaction_count += 1
        try:
            yield
        except Exception:
            self.rows = before
            raise


def make_store(monkeypatch):
    connection = FakeConnection()
    connect_calls = []

    def connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return connection

    monkeypatch.setattr(postgres_store.psycopg, "connect", connect)
    store = PostgresStore("postgresql://db.example.test/timesheet", Fernet.generate_key())
    return store, connection, connect_calls


def test_save_encrypts_json_and_loads_object(monkeypatch):
    store, connection, connect_calls = make_store(monkeypatch)
    value = {"access_token": "private-token", "realm_id": "test-realm"}

    store.save(1, value)
    encrypted = connection.rows[1]
    assert b"private-token" not in encrypted
    assert b"access_token" not in encrypted
    assert store.load(1) == value
    assert len(connect_calls) == 2
    assert all(call[1]["sslmode"] == "verify-full" for call in connect_calls)
    assert all(call[1]["autocommit"] is True for call in connect_calls)
    assert connection.closed


def test_corrupt_or_wrong_key_ciphertext_fails_closed(monkeypatch):
    store, connection, _ = make_store(monkeypatch)
    connection.rows[1] = b"not-fernet-ciphertext"

    with pytest.raises(HTTPException) as error:
        store.load(1)
    assert error.value.status_code == 503
    assert "not-fernet-ciphertext" not in str(error.value.detail)
    assert "postgres" not in str(error.value.detail).lower()


def test_mutate_and_reentrant_lock_reuse_one_connection(monkeypatch):
    store, connection, connect_calls = make_store(monkeypatch)
    with store.lock("qbo-state"):
        with store.lock("qbo-state"):
            result = store.mutate(1, lambda old: ({"count": (old or {}).get("count", 0) + 1}, "saved"))
            assert result == "saved"
            assert store.load(1) == {"count": 1}
        assert connection.active_locks == 1
        assert len(connect_calls) == 1
    assert connection.active_locks == 0
    assert connection.closed


def test_remote_connections_require_tls_verification(monkeypatch):
    connection = FakeConnection()
    seen = []
    monkeypatch.setattr(postgres_store.psycopg, "connect", lambda *a, **kw: (seen.append(kw) or connection))
    store = PostgresStore("postgresql://db.example.test/timesheet?sslmode=disable", Fernet.generate_key())
    with store._connection():
        pass
    assert seen[0]["sslmode"] == "verify-full"
    assert seen[0]["sslrootcert"] == certifi.where()


def test_insecure_mode_is_explicit_and_loopback_only():
    key = Fernet.generate_key()
    local = PostgresStore("postgresql://localhost/timesheet", key, allow_insecure_local=True)
    assert local._sslmode == "disable"
    with pytest.raises(ValueError):
        PostgresStore("postgresql://db.example.test/timesheet", key, allow_insecure_local=True)
    with pytest.raises(ValueError):
        PostgresStore("postgresql://ep-example-pooler.region.neon.tech/timesheet", key)


def test_import_empty_exports_decrypted_blobs_and_refuses_existing_rows(monkeypatch):
    store, connection, _ = make_store(monkeypatch)
    values = {1: {"token": "synthetic-a"}, 3: {"audit": ["synthetic-b"]}}

    assert store.import_empty(values) == 2
    assert connection.transaction_count == 1
    assert store.export_blobs() == values
    assert all(b"synthetic" not in blob for blob in connection.rows.values())
    before = connection.rows.copy()
    with pytest.raises(HTTPException) as error:
        store.import_empty({4: {"token": "must-not-import"}})
    assert error.value.status_code == 409
    assert connection.rows == before


def test_save_stays_durable_when_outer_lock_body_raises(monkeypatch):
    store, connection, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        with store.lock("qbo-write"):
            store.save(1, {"reservation": "durable"})
            raise RuntimeError("synthetic failure after durable save")
    assert store._decode(connection.rows[1]) == {"reservation": "durable"}
    assert connection.active_locks == 0


@pytest.mark.skipif(not os.environ.get("TIMESHEET_TEST_DATABASE_URL"), reason="local Postgres URL not configured")
def test_real_local_postgres_round_trip_and_lock_mutation():
    url = os.environ["TIMESHEET_TEST_DATABASE_URL"]
    host = urlparse(url).hostname or ""
    assert host.lower() in {"localhost", "127.0.0.1", "::1"}, "integration test is restricted to loopback Postgres"
    store = PostgresStore(url, Fernet.generate_key(), allow_insecure_local=True)
    blob_id = secrets.randbelow(1_000_000_000) + 1_000_000_000
    try:
        store.save(blob_id, {"token": "synthetic-test-value"})
        with store.lock(f"integration:{blob_id}"):
            result = store.mutate(blob_id, lambda old: ({"count": 1, **old}, "ok"))
            assert result == "ok"
            assert store.load(blob_id) == {"count": 1, "token": "synthetic-test-value"}
            with store._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT data FROM {TABLE} WHERE id = %s", (blob_id,))
                    encrypted = cursor.fetchone()[0]
            assert b"synthetic-test-value" not in encrypted
            assert b'"token"' not in encrypted
    finally:
        with store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = %s", (blob_id,))
