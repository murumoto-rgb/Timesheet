"""Test config: point the app at throwaway paths and disable the auth gate
before `main` is imported (it reads env at import time)."""
import os
import tempfile
import pytest

_tmp = tempfile.mkdtemp(prefix="qbo-test-")
os.environ["QBO_TOKENS_FILE"] = os.path.join(_tmp, "tok.json")
os.environ["APP_PASSWORD"] = ""
os.environ.setdefault("QBO_CLIENT_ID", "test-client")
os.environ.setdefault("QBO_CLIENT_SECRET", "test-secret")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)


@pytest.fixture(autouse=True)
def isolated_storage_and_network(tmp_path, monkeypatch):
    """Every test gets disposable storage; accidental HTTP is a test failure."""
    import main
    import requests
    monkeypatch.setattr(main, "SUPABASE_URL", "")
    monkeypatch.setattr(main, "SUPABASE_KEY", "")
    monkeypatch.setattr(main, "OP_FILE", str(tmp_path / "operations.json"))
    monkeypatch.setattr(main, "AUDIT_FILE", str(tmp_path / "audit.json"))
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "test-realm"})
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external HTTP request in an isolated backend test")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
