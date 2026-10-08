#!/usr/bin/env python3
"""Validate and transfer the four encrypted-at-rest Timesheet blobs.

This offline utility never calls QuickBooks. Import dry runs validate files
without opening a Postgres connection. Run as ``python -m scripts.neon_transfer``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from fastapi import HTTPException

from postgres_store import PostgresStore
from write_journal import validate as validate_journal


BACKUP_FILES = {
    "qbo_tokens.json": 1,
    "qbo_push.json": 2,
    "qbo_audit.json": 3,
    "qbo_operations.json": 4,
}
REQUIRED_BACKUP_FILES = {"qbo_tokens.json"}
EMPTY_OPTIONAL_BLOBS = {
    "qbo_operations.json": {"revision": 0, "operations": []},
    "qbo_push.json": {"subs": []},
    "qbo_audit.json": {"events": []},
}
EXPORT_ORDER = ("qbo_tokens.json", "qbo_operations.json", "qbo_push.json", "qbo_audit.json")
API_BASES = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class TransferError(ValueError):
    """An expected, safe-to-display transfer validation failure."""


def _reject_constant(_value):
    raise ValueError("non-finite JSON value")


def _safe_backup_path(path: Path) -> Path:
    try:
        if path.is_symlink():
            raise TransferError("backup directory must not be a symbolic link")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise TransferError("backup path must be a directory")
        manifest = resolved / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise TransferError("backup manifest is missing or unsafe")
        return resolved
    except TransferError:
        raise
    except Exception:
        raise TransferError("backup path could not be read") from None


def _load_manifest(backup_dir: Path) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    backup_dir = _safe_backup_path(backup_dir)
    try:
        manifest = json.loads((backup_dir / "manifest.json").read_bytes(), parse_constant=_reject_constant)
    except Exception:
        raise TransferError("backup manifest is unreadable or invalid JSON") from None
    if not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest["format"] != 1:
        raise TransferError("unsupported backup manifest format")
    records = manifest.get("files")
    if not isinstance(records, list) or not records or any(not isinstance(item, dict) for item in records):
        raise TransferError("backup manifest has no valid file list")

    names = [item.get("name") for item in records]
    if any(not isinstance(name, str) or name not in BACKUP_FILES for name in names):
        raise TransferError("backup must contain only the four Timesheet data files")
    if len(names) != len(set(names)):
        raise TransferError("backup manifest contains duplicate filenames")
    missing = sorted(set(BACKUP_FILES) - set(names))
    missing_required = sorted(REQUIRED_BACKUP_FILES.intersection(missing))
    if missing_required:
        raise TransferError("backup is missing required files: " + ", ".join(missing_required))

    blobs: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    for record in records:
        name = record["name"]
        digest = record.get("sha256")
        source = backup_dir / name
        if (not isinstance(digest, str) or not _HASH_RE.fullmatch(digest)
                or source.is_symlink() or not source.is_file()):
            raise TransferError("backup integrity check failed for " + name)
        try:
            raw = source.read_bytes()
        except Exception:
            raise TransferError("backup integrity check failed for " + name) from None
        if hashlib.sha256(raw).hexdigest() != digest or type(record.get("bytes")) is not int or record["bytes"] != len(raw):
            raise TransferError("backup integrity check failed for " + name)
        try:
            value = json.loads(raw, parse_constant=_reject_constant)
        except Exception:
            raise TransferError("backup data is unreadable or invalid JSON") from None
        if not isinstance(value, dict):
            raise TransferError("each Timesheet blob must contain a JSON object")
        blobs[name] = value
        hashes[name] = digest
    return blobs, hashes, missing


def _realm(tokens: dict) -> str:
    realm = tokens.get("realm_id")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    expiry = tokens.get("access_expires_at")
    if not isinstance(realm, (str, int)) or isinstance(realm, bool) or not str(realm).strip():
        raise TransferError("token blob has no valid realm binding")
    if not isinstance(access, str) or not access.strip() or not isinstance(refresh, str) or not refresh.strip():
        raise TransferError("token blob is missing access or refresh credentials")
    try:
        finite_expiry = not isinstance(expiry, bool) and isinstance(expiry, (int, float)) and math.isfinite(float(expiry))
    except (OverflowError, ValueError):
        finite_expiry = False
    if not finite_expiry:
        raise TransferError("token blob has an invalid access expiry")
    return str(realm)


def validate_blobs(blobs: dict[str, dict], environment: str) -> tuple[dict[int, dict], dict[str, str]]:
    """Validate app invariants and map recovery filenames to integer IDs."""
    if environment not in API_BASES:
        raise TransferError("environment must be sandbox or production")
    if set(blobs) != set(BACKUP_FILES):
        missing = sorted(set(BACKUP_FILES) - set(blobs))
        extra = sorted(set(blobs) - set(BACKUP_FILES))
        if missing:
            raise TransferError("all four Timesheet blobs are required: " + ", ".join(missing))
        raise TransferError("backup contains unexpected blob names")
    tokens = blobs["qbo_tokens.json"]
    realm = _realm(tokens)
    journal = blobs["qbo_operations.json"]
    try:
        validate_journal(journal)
    except HTTPException:
        raise TransferError("write journal failed validation") from None
    except Exception:
        raise TransferError("write journal failed validation") from None
    for operation in journal["operations"]:
        scope = operation.get("scope")
        valid_binding = False
        if isinstance(scope, str):
            for base in API_BASES.values():
                prefix = base + "|"
                historic_realm = scope[len(prefix):] if scope.startswith(prefix) else ""
                if historic_realm and "|" not in historic_realm and historic_realm.strip():
                    valid_binding = True
                    break
        if not valid_binding:
            raise TransferError("write journal has a missing or unverifiable environment/company binding")
    # Journal entries can legitimately belong to an earlier QuickBooks company
    # or environment. --environment is the operator's explicit declaration of
    # where the currently connected token blob belongs; tokens carry no offline
    # field that can prove that declaration independently.

    push = blobs["qbo_push.json"]
    subscriptions = push.get("subs", [])
    if not isinstance(subscriptions, list) or any(not isinstance(item, dict) for item in subscriptions):
        raise TransferError("push blob has an invalid subscriptions list")
    vapid = push.get("vapid")
    if vapid is not None and (not isinstance(vapid, dict)
            or not isinstance(vapid.get("private_pem"), str)
            or not isinstance(vapid.get("app_key"), str)):
        raise TransferError("push blob has an invalid VAPID keypair")

    audit_events = blobs["qbo_audit.json"].get("events", [])
    if not isinstance(audit_events, list) or any(not isinstance(item, dict) for item in audit_events):
        raise TransferError("audit blob has an invalid events list")

    by_id = {BACKUP_FILES[name]: blobs[name] for name in BACKUP_FILES}
    return by_id, {"environment": environment, "realm_id": realm}


def validate_backup(backup: Path, environment: str) -> tuple[dict[int, dict], dict[str, object]]:
    blobs, hashes, missing = _load_manifest(backup)
    defaulted = [name for name in missing if name in EMPTY_OPTIONAL_BLOBS]
    for name in defaulted:
        blobs[name] = EMPTY_OPTIONAL_BLOBS[name].copy()
    by_id, binding = validate_blobs(blobs, environment)
    return by_id, {**hashes, **binding, "defaulted": defaulted}


def _read_secrets(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise TransferError("secrets file must not be a symbolic link")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(_REPO_ROOT)
        except ValueError:
            pass
        else:
            raise TransferError("secrets file must be outside the Git checkout")
        if not resolved.is_file():
            raise TransferError("secrets file is invalid")
        if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
            raise TransferError("secrets file permissions must be private (mode 600)")
        secrets = json.loads(resolved.read_bytes(), parse_constant=_reject_constant)
    except TransferError:
        raise
    except Exception:
        raise TransferError("secrets file is unreadable or invalid JSON") from None
    if (not isinstance(secrets, dict) or not isinstance(secrets.get("runtime_url"), str)
            or not secrets["runtime_url"] or not isinstance(secrets.get("encryption_key"), str)
            or not secrets["encryption_key"]):
        raise TransferError("secrets file must contain runtime_url and encryption_key")
    return secrets


def _summary(hashes: dict[str, str], count: int, mode: str, defaulted=()) -> str:
    parts = [f"{name}={hashes[name]}" for name in EXPORT_ORDER if name in hashes]
    rendered = f"mode={mode} blobs={count} sha256=" + ",".join(parts)
    if defaulted:
        rendered += " defaulted=" + ",".join(defaulted)
    return rendered


def import_backup(backup: Path, environment: str, *, secrets_file: Path | None = None,
                  execute: bool = False, source_stopped: bool = False,
                  accept_empty_optional: bool = False) -> str:
    by_id, info = validate_backup(backup, environment)
    hashes = {name: value for name, value in info.items() if name in BACKUP_FILES}
    defaulted = info["defaulted"]
    if not execute:
        return _summary(hashes, len(by_id), "dry-run", defaulted)
    if not source_stopped:
        raise TransferError("--execute requires --source-stopped acknowledgment")
    if defaulted and not accept_empty_optional:
        raise TransferError("--execute with missing optional blobs requires --accept-empty-optional acknowledgment")
    if secrets_file is None:
        raise TransferError("--execute requires --secrets-file")
    secrets = _read_secrets(secrets_file)
    try:
        store = PostgresStore(secrets["runtime_url"], secrets["encryption_key"])
        imported = store.import_empty(by_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise TransferError("Postgres storage is not empty; no import was applied") from None
        raise TransferError("Postgres import failed; preserve the source and review recovery") from None
    except Exception:
        raise TransferError("Postgres import failed; preserve the source and review recovery") from None
    return _summary(hashes, imported, "imported", defaulted)


def _private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def export_backup(destination: Path, environment: str, secrets_file: Path) -> str:
    secrets = _read_secrets(secrets_file)
    try:
        store = PostgresStore(secrets["runtime_url"], secrets["encryption_key"])
        by_id = store.export_blobs()
    except HTTPException:
        raise TransferError("Postgres export failed; preserve the database and review recovery") from None
    except Exception:
        raise TransferError("Postgres export failed; preserve the database and review recovery") from None

    expected_ids = set(BACKUP_FILES.values())
    if not isinstance(by_id, dict) or not expected_ids.issubset(by_id):
        raise TransferError("Postgres must contain all four expected Timesheet blobs")
    selected = {blob_id: by_id[blob_id] for blob_id in expected_ids}
    blobs = {name: selected[blob_id] for name, blob_id in BACKUP_FILES.items()}
    validated, binding = validate_blobs(blobs, environment)
    del validated

    try:
        if destination.is_symlink():
            raise TransferError("export destination must not be a symbolic link")
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(destination, 0o700)
        records = []
        serialized = {}
        for name in EXPORT_ORDER:
            raw = (json.dumps(blobs[name], indent=2, allow_nan=False) + "\n").encode("utf-8")
            serialized[name] = raw
            records.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        for name, raw in serialized.items():
            _private_write(destination / name, raw)
        manifest = {
            "format": 1,
            "source": "postgres:timesheet_private.encrypted_blobs",
            "files": records,
        }
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        _private_write(destination / "manifest.json", manifest_bytes)
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except TransferError:
        raise
    except Exception:
        raise TransferError("private export backup could not be written") from None
    hashes = {record["name"]: record["sha256"] for record in records}
    return _summary(hashes, len(blobs), f"exported environment={binding['environment']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "import"):
        item = sub.add_parser(command)
        item.add_argument("--backup", type=Path, required=True)
        item.add_argument("--environment", choices=tuple(API_BASES), required=True)
        if command == "import":
            item.add_argument("--secrets-file", type=Path)
            item.add_argument("--execute", action="store_true")
            item.add_argument("--source-stopped", action="store_true")
            item.add_argument("--accept-empty-optional", action="store_true")
    export = sub.add_parser("export")
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--secrets-file", type=Path, required=True)
    export.add_argument("--environment", choices=tuple(API_BASES), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            _by_id, info = validate_backup(args.backup, args.environment)
            hashes = {name: value for name, value in info.items() if name in BACKUP_FILES}
            print(_summary(hashes, len(BACKUP_FILES), "validated"))
        elif args.command == "import":
            print(import_backup(args.backup, args.environment, secrets_file=args.secrets_file,
                                execute=args.execute, source_stopped=args.source_stopped,
                                accept_empty_optional=args.accept_empty_optional))
        else:
            print(export_backup(args.destination, args.environment, args.secrets_file))
        return 0
    except TransferError as exc:
        print(f"transfer error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("transfer error: operation could not be completed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
