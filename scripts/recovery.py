#!/usr/bin/env python3
"""Offline, fail-closed backup and restore for local app data files.

This is a file backup, not a QuickBooks export or a complete accounting backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

DEFAULT_NAMES = ("qbo_tokens.json", "qbo_operations.json", "qbo_push.json", "qbo_audit.json")


def _safe_names(names) -> list[str]:
    names = list(names)
    if any(not isinstance(name, str) or not name or name in (".", "..")
           or Path(name).name != name or "\\" in name or name == "manifest.json" for name in names):
        raise ValueError("backup contains an unsafe filename")
    if len(names) != len(set(names)):
        raise ValueError("backup contains duplicate filenames")
    return names


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup(source: Path, destination: Path, names=DEFAULT_NAMES) -> Path:
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("backup source and destination must not be symbolic links")
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ValueError("backup source must be an existing directory")
    contents = []
    for name in _safe_names(names):
        path = source / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"refusing to back up a link or non-file: {name}")
        if path.is_file():
            contents.append((name, path.read_bytes()))
    if not contents:
        raise ValueError("no local app data files found; Supabase data requires a database backup")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = []
    for name, data in contents:
        _write_private(destination / name, data)
        files.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {"format": 1, "source": str(source), "files": files}
    manifest_path = destination / "manifest.json"
    _write_private(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())
    _sync_directory(destination)
    return manifest_path


def restore(archive: Path, destination: Path, *, overwrite=False) -> list[str]:
    if archive.is_symlink() or destination.is_symlink():
        raise ValueError("restore archive and destination must not be symbolic links")
    archive, destination = archive.resolve(), destination.resolve()
    manifest_path = archive / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("backup manifest must not be a symbolic link")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise ValueError("unsupported backup manifest format")
    records = manifest.get("files")
    if not isinstance(records, list) or not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("backup manifest has no valid file list")
    _safe_names(record.get("name") for record in records)
    # Read and validate all bytes before creating the destination. This also
    # prevents a later change to an archive file from bypassing the digest check.
    contents = []
    for record in records:
        name = record["name"]
        source = archive / name
        target = destination / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"refusing to replace a link or non-file: {name}")
        if target.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing file: {target}")
        digest = record.get("sha256")
        if source.is_symlink() or not source.is_file() or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"backup integrity check failed: {name}")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest or type(record.get("bytes")) is not int or record["bytes"] != len(data):
            raise ValueError(f"backup integrity check failed: {name}")
        contents.append((name, data))
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    restored = []
    for name, data in contents:
        target = destination / name
        with tempfile.NamedTemporaryFile(dir=destination, prefix=f".{name}.", delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        try:
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
            if overwrite:
                os.replace(temp_path, target)
            else:
                # Atomic no-clobber even if another file appears after validation.
                os.link(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
        restored.append(name)
    _sync_directory(destination)
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup"); b.add_argument("source", type=Path); b.add_argument("destination", type=Path)
    r = sub.add_parser("restore"); r.add_argument("archive", type=Path); r.add_argument("destination", type=Path); r.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.command == "backup": print(backup(args.source, args.destination)); return 0
    print(json.dumps(restore(args.archive, args.destination, overwrite=args.overwrite))); return 0


if __name__ == "__main__":
    raise SystemExit(main())
