import hashlib
import json

import pytest

from scripts.report_capacity import CapacityError, _parse_rows, build_bundle


def test_rows_parser_accepts_requested_boundaries_and_rejects_oversize():
    assert _parse_rows("999,1000,1001,5000,10000,25000,100001") == [999, 1000, 1001, 5000, 10000, 25000, 100001]
    with pytest.raises(CapacityError):
        _parse_rows("100002")
    with pytest.raises(CapacityError):
        _parse_rows("-1")


def test_bundle_contains_only_tracked_runtime_and_safe_synthetic_wrapper(tmp_path):
    destination = tmp_path / "fresh-bundle"
    result = build_bundle(destination)
    assert result["files"] > 0
    manifest = json.loads((destination / "capacity_manifest.json").read_text())
    assert manifest["source_main_sha256"] == hashlib.sha256((destination / "capacity_source.py").read_bytes()).hexdigest()
    vercel = json.loads((destination / "vercel.json").read_text())
    assert vercel["crons"] == []
    assert not (destination / ".git").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "tests").exists()
    wrapper = (destination / "main.py").read_text()
    assert "capacity_source as _source" in wrapper
    assert "requests.sessions.Session.request = _blocked_post" in wrapper
    assert "CAPACITY-SYNTHETIC-REALM" in wrapper


def test_bundle_requires_fresh_destination_outside_repository(tmp_path):
    destination = tmp_path / "bundle"
    destination.mkdir()
    with pytest.raises(CapacityError, match="new and empty"):
        build_bundle(destination)

    inside_repository = __import__("scripts.report_capacity", fromlist=["REPO_ROOT"]).REPO_ROOT / ".capacity-test-bundle"
    try:
        with pytest.raises(CapacityError, match="outside the repository"):
            build_bundle(inside_repository)
    finally:
        inside_repository.rmdir() if inside_repository.exists() else None
