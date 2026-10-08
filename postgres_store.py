"""Encrypted Postgres storage for the single-user Timesheet application.

Connections are short lived. A session-scoped advisory lock keeps one
connection open only for the explicit duration of ``lock()``, including any
QBO request the caller performs inside that context.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

import psycopg
import certifi
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


SCHEMA = "timesheet_private"
TABLE = f"{SCHEMA}.encrypted_blobs"
_UNAVAILABLE_MESSAGE = (
    "Stored app data could not be verified. Preserve it and review recovery before continuing."
)
_LOCK_TIMEOUT_MS = 10_000


def _unavailable() -> HTTPException:
    return HTTPException(503, _UNAVAILABLE_MESSAGE)


def _advisory_key(name: str) -> int:
    """Map arbitrary lock names to PostgreSQL's signed 64-bit lock key."""
    value = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    return value - (1 << 64) if value >= (1 << 63) else value


@dataclass
class _LockState:
    connection: psycopg.Connection
    locks: dict[str, int] = field(default_factory=dict)


class PostgresStore:
    """Persist encrypted JSON objects in the private Timesheet schema.

    ``allow_insecure_local`` exists only for explicit local integration tests.
    Even with it enabled, only loopback database URLs may disable TLS.
    """

    def __init__(self, url: str, encryption_key: str | bytes, *, allow_insecure_local: bool = False):
        if not isinstance(url, str) or not url:
            raise ValueError("A Postgres URL is required")
        try:
            key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            self._fernet = Fernet(key)
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ValueError("A valid Fernet encryption key is required") from None

        parsed = urlparse(url)
        host = parsed.hostname or ""
        if "-pooler" in host.lower():
            raise ValueError("A direct Postgres connection is required for session locks")
        local = host.lower() in {"localhost", "127.0.0.1", "::1"}
        if allow_insecure_local and not local:
            raise ValueError("Insecure Postgres connections are permitted only to loopback")
        self._url = url
        self._sslmode = "disable" if allow_insecure_local else "verify-full"
        self._sslrootcert = None if allow_insecure_local else certifi.where()
        self._lock_state: contextvars.ContextVar[_LockState | None] = contextvars.ContextVar(
            f"timesheet_postgres_lock_{id(self)}", default=None
        )

    def _connect(self) -> psycopg.Connection:
        # Explicit keyword settings override any weaker sslmode in the URL.
        options = {
            "sslmode": self._sslmode,
            "connect_timeout": 5,
            "application_name": "timesheet",
            # Session locks span the QBO request. Autocommit keeps each data
            # statement durable before an error can follow an accepted QBO write.
            "autocommit": True,
        }
        if self._sslrootcert is not None:
            options["sslrootcert"] = self._sslrootcert
        return psycopg.connect(self._url, **options)

    @contextlib.contextmanager
    def _connection(self):
        state = self._lock_state.get()
        if state is not None:
            yield state.connection
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_blob_id(blob_id: int) -> None:
        if type(blob_id) is not int:
            raise ValueError("blob_id must be an integer")

    def _decode(self, encrypted: bytes) -> dict | None:
        try:
            raw = self._fernet.decrypt(bytes(encrypted))
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (InvalidToken, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise _unavailable() from None

    def _encode(self, value: dict) -> bytes:
        if not isinstance(value, dict):
            raise ValueError("Stored values must be objects")
        try:
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
            return self._fernet.encrypt(raw)
        except (TypeError, ValueError, UnicodeEncodeError):
            # Invalid caller data is a programming/input error, not a storage
            # failure. Keep the exception free of any connection information.
            raise ValueError("Stored value must be finite JSON data") from None

    def load(self, blob_id: int) -> dict | None:
        self._validate_blob_id(blob_id)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT data FROM {TABLE} WHERE id = %s", (blob_id,))
                    row = cursor.fetchone()
            return None if row is None else self._decode(row[0])
        except HTTPException:
            raise
        except Exception:
            raise _unavailable() from None

    def save(self, blob_id: int, value: dict) -> None:
        self._validate_blob_id(blob_id)
        encrypted = self._encode(value)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {TABLE} (id, data) VALUES (%s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                        (blob_id, encrypted),
                    )
        except Exception:
            raise _unavailable() from None

    def mutate(self, blob_id: int, callback):
        """Atomically read, update, and save one blob under its advisory lock.

        The callback receives the old dictionary or ``None`` and returns a
        pair ``(new_dictionary, result)``. The result is returned unchanged.
        """
        self._validate_blob_id(blob_id)
        with self.lock(f"blob:{blob_id}"):
            current = self.load(blob_id)
            updated, result = callback(current)
            self.save(blob_id, updated)
            return result

    def import_empty(self, blobs: dict[int, dict]) -> int:
        """Atomically import encrypted blobs only when the table is empty."""
        if not isinstance(blobs, dict):
            raise ValueError("blobs must be a dictionary")
        encoded = []
        for blob_id, value in blobs.items():
            self._validate_blob_id(blob_id)
            encoded.append((blob_id, self._encode(value)))
        if not encoded:
            return 0

        try:
            with self.lock("bootstrap-import"):
                with self._connection() as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            # Exclude every concurrent writer between the empty
                            # check and commit, including ordinary blob saves.
                            cursor.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
                            cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {TABLE})")
                            if cursor.fetchone()[0]:
                                raise HTTPException(409, "Postgres storage is not empty; import was not applied.")
                            cursor.executemany(
                                f"INSERT INTO {TABLE} (id, data) VALUES (%s, %s)", encoded
                            )
            return len(encoded)
        except HTTPException:
            raise
        except Exception:
            raise _unavailable() from None

    def export_blobs(self) -> dict[int, dict]:
        """Read one consistent snapshot of every blob and decrypt in memory."""
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT id, data FROM {TABLE} ORDER BY id")
                    rows = cursor.fetchall()
            return {blob_id: self._decode(data) for blob_id, data in rows}
        except HTTPException:
            raise
        except Exception:
            raise _unavailable() from None

    @contextlib.contextmanager
    def lock(self, name: str):
        """Hold a bounded, session-scoped advisory lock for ``name``.

        Re-entering a lock with the same name in the same execution context
        increments a reference count and does not open another connection.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("A non-empty lock name is required")
        state = self._lock_state.get()
        if state is not None and name in state.locks:
            state.locks[name] += 1
            try:
                yield
            finally:
                state.locks[name] -= 1
            return

        outermost = state is None
        if outermost:
            try:
                connection = self._connect()
            except Exception:
                raise _unavailable() from None
            state = _LockState(connection)
            token = self._lock_state.set(state)
        assert state is not None
        lock_acquired = False
        try:
            try:
                with state.connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', %s, false)", (f"{_LOCK_TIMEOUT_MS}ms",))
                    cursor.execute("SELECT pg_advisory_lock(%s)", (_advisory_key(name),))
            except Exception:
                raise _unavailable() from None
            state.locks[name] = 1
            lock_acquired = True
            yield
        finally:
            if lock_acquired:
                state.locks.pop(name, None)
                try:
                    with state.connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (_advisory_key(name),))
                except Exception:
                    if outermost:
                        state.connection.close()
                        self._lock_state.reset(token)
                    raise _unavailable() from None
            if outermost:
                try:
                    state.connection.close()
                finally:
                    self._lock_state.reset(token)
