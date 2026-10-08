import hashlib
import json
import stat
import os
import time
import uuid
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from cryptography.fernet import Fernet

import scripts.neon_transfer as transfer
from scripts.recovery import backup, restore
from postgres_store import PostgresStore as RealPostgresStore
from write_journal import digest


def _blobs(*, environment="sandbox", scope=None):
    realm = "synthetic-realm-123"
    base = transfer.API_BASES[environment]
    operation = {
        "operationId": str(uuid.uuid4()),
        "action": "create",
        "status": "reserved",
        "payload": {"TxnDate": "2026-10-06"},
        "digest": digest({"TxnDate": "2026-10-06"}),
        "scope": scope if scope is not None else f"{base}|{realm}",
        "createdAt": "2026-10-06T12:00:00+00:00",
        "owner": "synthetic-owner",
        "leaseUntil": time.time() + 300,
    }
    return {
        "qbo_tokens.json": {
            "realm_id": realm,
            "access_token": "synthetic-access-secret",
            "refresh_token": "synthetic-refresh-secret",
            "access_expires_at": time.time() + 3600,
        },
        "qbo_push.json": {"subs": []},
        "qbo_audit.json": {"events": []},
        "qbo_operations.json": {"revision": 1, "operations": [operation]},
    }


def _make_backup(tmp_path, blobs=None):
    blobs = blobs or _blobs()
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    source.mkdir(parents=True)
    for name, value in blobs.items():
        (source / name).write_text(json.dumps(value, allow_nan=False) + "\n")
    archive = tmp_path / "backup"
    backup(source, archive, names=tuple(blobs))
    return archive


def _secrets(path):
    path.write_text(json.dumps({"runtime_url": "postgresql://db.example.test/db", "encryption_key": "synthetic-fernet-key"}))
    path.chmod(0o600)
    return path


def test_validate_backup_checks_full_manifest_and_returns_ids_without_database(tmp_path, monkeypatch):
    archive = _make_backup(tmp_path)
    monkeypatch.setattr(transfer, "PostgresStore", lambda *_a, **_k: pytest.fail("validation must not connect"))

    by_id, info = transfer.validate_backup(archive, "sandbox")
    assert set(by_id) == {1, 2, 3, 4}
    assert info["environment"] == "sandbox"
    assert info["realm_id"] == "synthetic-realm-123"
    summary = transfer.import_backup(archive, "sandbox")
    assert "mode=dry-run blobs=4" in summary
    assert "synthetic-realm" not in summary
    assert "synthetic-access-secret" not in summary
    assert str(archive) not in summary


def test_manifest_rejects_tampering_and_missing_blob(tmp_path):
    archive = _make_backup(tmp_path)
    (archive / "qbo_push.json").write_text('{"subscriptions":[]}\n')
    with pytest.raises(transfer.TransferError, match="integrity"):
        transfer.validate_backup(archive, "sandbox")


def test_missing_optional_blobs_get_explicit_empty_defaults(tmp_path):
    blobs = {"qbo_tokens.json": _blobs()["qbo_tokens.json"]}
    archive = _make_backup(tmp_path, blobs)
    by_id, info = transfer.validate_backup(archive, "sandbox")
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[2] == {"subs": []}
    assert by_id[3] == {"events": []}
    assert by_id[4] == {"revision": 0, "operations": []}
    assert set(info["defaulted"]) == {"qbo_push.json", "qbo_audit.json", "qbo_operations.json"}
    assert "defaulted=" in transfer.import_backup(archive, "sandbox")
    with pytest.raises(transfer.TransferError, match="accept-empty-optional"):
        transfer.import_backup(archive, "sandbox", secrets_file=tmp_path / "unused", execute=True,
                               source_stopped=True)


def test_token_and_journal_bindings_fail_closed(tmp_path):
    # Prior journal operations may belong to a previous company/environment;
    # their original scope is retained after token refresh or company switch.
    blobs = _blobs(scope="https://quickbooks.api.intuit.com|previous-realm")
    archive = _make_backup(tmp_path, blobs)
    by_id, _ = transfer.validate_backup(archive, "sandbox")
    assert by_id[4]["operations"][0]["scope"].endswith("|previous-realm")

    blobs = _blobs()
    blobs["qbo_tokens.json"]["access_expires_at"] = "not-finite"
    archive = _make_backup(tmp_path / "invalid-expiry", blobs)
    with pytest.raises(transfer.TransferError, match="invalid access expiry"):
        transfer.validate_backup(archive, "sandbox")

    blobs = _blobs(scope="https://unknown.example|some-realm")
    archive = _make_backup(tmp_path / "invalid-scope", blobs)
    with pytest.raises(transfer.TransferError, match="unverifiable environment/company"):
        transfer.validate_backup(archive, "sandbox")


def test_journal_with_legacy_unbound_scope_is_rejected(tmp_path):
    blobs = _blobs()
    del blobs["qbo_operations.json"]["operations"][0]["scope"]
    archive = _make_backup(tmp_path, blobs)
    with pytest.raises(transfer.TransferError, match="journal failed validation"):
        transfer.validate_backup(archive, "sandbox")


def test_execute_requires_source_stopped_before_reading_secrets(tmp_path, monkeypatch):
    archive = _make_backup(tmp_path)
    monkeypatch.setattr(transfer, "PostgresStore", lambda *_a, **_k: pytest.fail("must check source acknowledgment first"))
    with pytest.raises(transfer.TransferError, match="source-stopped"):
        transfer.import_backup(archive, "sandbox", secrets_file=tmp_path / "missing", execute=True)


def test_import_execute_uses_atomic_four_blob_api_and_safe_output(tmp_path, monkeypatch):
    archive = _make_backup(tmp_path)
    secret_file = _secrets(tmp_path / "private-secrets.json")
    observed = {}

    class FakeStore:
        def __init__(self, url, key):
            observed["secrets"] = (url, key)

        def import_empty(self, blobs):
            observed["blobs"] = blobs
            return 4

    monkeypatch.setattr(transfer, "PostgresStore", FakeStore)
    summary = transfer.import_backup(archive, "sandbox", secrets_file=secret_file, execute=True, source_stopped=True)
    assert set(observed["blobs"]) == {1, 2, 3, 4}
    assert observed["secrets"] == ("postgresql://db.example.test/db", "synthetic-fernet-key")
    assert "synthetic-access-secret" not in summary
    assert "synthetic-realm" not in summary
    assert str(secret_file) not in summary


def test_secrets_file_must_be_private_and_outside_repository(tmp_path):
    secrets = _secrets(tmp_path / "private.json")
    assert transfer._read_secrets(secrets)["runtime_url"].endswith("/db")
    secrets.chmod(0o644)
    with pytest.raises(transfer.TransferError, match="permissions"):
        transfer._read_secrets(secrets)

    in_repo = transfer._REPO_ROOT / ".transfer-test-secrets.json"
    try:
        in_repo.write_text(json.dumps({"runtime_url": "x", "encryption_key": "y"}))
        in_repo.chmod(0o600)
        with pytest.raises(transfer.TransferError, match="outside the Git checkout"):
            transfer._read_secrets(in_repo)
    finally:
        in_repo.unlink(missing_ok=True)


def test_export_writes_private_recovery_manifest_and_hashes(tmp_path, monkeypatch):
    blobs = _blobs()
    by_id, _binding = transfer.validate_blobs(blobs, "sandbox")

    class FakeStore:
        def __init__(self, *_args):
            pass

        def export_blobs(self):
            return {**by_id, 5: {"synthetic": "oauth-state"}, 6: {"synthetic": "login-state"},
                    90: {"synthetic": "unrelated"}}

    monkeypatch.setattr(transfer, "PostgresStore", FakeStore)
    secret_file = _secrets(tmp_path / "secrets.json")
    destination = tmp_path / "private-export"
    summary = transfer.export_backup(destination, "sandbox", secret_file)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["format"] == 1
    assert {entry["name"] for entry in manifest["files"]} == set(transfer.BACKUP_FILES)
    for entry in manifest["files"]:
        target = destination / entry["name"]
        data = target.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "manifest.json").stat().st_mode) == 0o600
    assert "blobs=4" in summary
    assert "synthetic-access-secret" not in summary


@pytest.mark.skipif(not os.environ.get("TIMESHEET_TEST_DATABASE_URL"), reason="local Postgres URL not configured")
def test_real_local_import_export_restore_and_nonempty_refusal(tmp_path, monkeypatch):
    url = os.environ["TIMESHEET_TEST_DATABASE_URL"]
    if (urlparse(url).hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("destructive transfer test is restricted to loopback Postgres")
    if (urlparse(url).path or "").lstrip("/") != "timesheet_test":
        pytest.skip("destructive transfer test requires the dedicated timesheet_test database")
    key = Fernet.generate_key().decode("ascii")
    store = RealPostgresStore(url, key, allow_insecure_local=True)
    with store._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM timesheet_private.encrypted_blobs)")
            occupied = cursor.fetchone()[0]
    if occupied:
        pytest.skip("local transfer test requires an empty dedicated Postgres database")

    archive = _make_backup(tmp_path)
    secrets_file = _secrets(tmp_path / "local-secrets.json")
    secrets_file.write_text(json.dumps({"runtime_url": url, "encryption_key": key}))
    secrets_file.chmod(0o600)
    monkeypatch.setattr(transfer, "PostgresStore",
                        lambda connection_url, encryption_key: RealPostgresStore(
                            connection_url, encryption_key, allow_insecure_local=True))
    try:
        transfer.import_backup(archive, "sandbox", secrets_file=secrets_file, execute=True, source_stopped=True)
        before = store.export_blobs()
        assert set(before) >= {1, 2, 3, 4}
        destination = tmp_path / "local-export"
        transfer.export_backup(destination, "sandbox", secrets_file)
        restored = tmp_path / "restored"
        assert set(restore(destination, restored)) == set(transfer.BACKUP_FILES)
        with pytest.raises(transfer.TransferError, match="not empty"):
            transfer.import_backup(archive, "sandbox", secrets_file=secrets_file, execute=True,
                                   source_stopped=True)
        assert store.export_blobs() == before
    finally:
        with store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM timesheet_private.encrypted_blobs WHERE id IN (1, 2, 3, 4)")


@pytest.mark.skipif(not os.environ.get("TIMESHEET_TEST_DATABASE_URL"), reason="local Postgres URL not configured")
def test_real_local_nonempty_import_refuses_without_changing_rows():
    url = os.environ["TIMESHEET_TEST_DATABASE_URL"]
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"} or parsed.path.lstrip("/") != "timesheet_test":
        pytest.skip("nonempty refusal test is restricted to the dedicated loopback database")
    store = RealPostgresStore(url, Fernet.generate_key(), allow_insecure_local=True)
    with store._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM timesheet_private.encrypted_blobs")
            before = cursor.fetchone()[0]
    if not before:
        pytest.skip("nonempty refusal test requires pre-existing local rows")
    with pytest.raises(HTTPException) as error:
        store.import_empty({1: {"synthetic": "a"}, 2: {"synthetic": "b"},
                            3: {"synthetic": "c"}, 4: {"synthetic": "d"}})
    assert error.value.status_code == 409
    with store._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM timesheet_private.encrypted_blobs")
            after = cursor.fetchone()[0]
    assert after == before
