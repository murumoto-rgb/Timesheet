import json
import stat
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main


@pytest.mark.parametrize("rows", [False, [None], [3], [{}], [{"data": []}], [{"data": {}}, {"data": {}}]])
def test_malformed_remote_blobs_fail_closed(monkeypatch, rows):
    monkeypatch.setattr(main, "SUPABASE_URL", "https://synthetic.invalid")
    monkeypatch.setattr(main, "SUPABASE_KEY", "synthetic")
    monkeypatch.setattr(main.requests, "get", lambda *a, **kw: SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows))
    with pytest.raises(HTTPException) as error:
        main._load_blob("unused", 3)
    assert error.value.status_code == 503


@pytest.mark.parametrize("contents", ["", "{broken", "[]", "null"])
def test_unreadable_existing_blob_is_not_treated_as_an_empty_store(tmp_path, monkeypatch, contents):
    monkeypatch.setattr(main, "SUPABASE_URL", "")
    path = tmp_path / "qbo_audit.json"
    path.write_text(contents)
    monkeypatch.setattr(main, "AUDIT_FILE", str(path))
    with pytest.raises(HTTPException) as error:
        main._load_audit()
    assert error.value.status_code == 503
    assert main._audit("create", "synthetic-id", {"Id": "synthetic-id"}, None, scope="synthetic-company") is False
    assert path.read_text() == contents


def test_local_blob_failure_preserves_old_data_and_private_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_URL", "")
    path = tmp_path / "qbo_tokens.json"
    main._save_blob(str(path), 1, {"synthetic": "original"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        main._save_blob(str(path), 1, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == {"synthetic": "original"}
    assert list(tmp_path.glob(".timesheet-*")) == []
    main._save_blob(str(path), 1, {"synthetic": "replacement"})
    assert json.loads(path.read_text()) == {"synthetic": "replacement"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
