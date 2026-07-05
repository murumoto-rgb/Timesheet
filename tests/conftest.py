"""Test config: point the app at throwaway paths and disable the auth gate
before `main` is imported (it reads env at import time)."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="qbo-test-")
os.environ["QBO_TOKENS_FILE"] = os.path.join(_tmp, "tok.json")
os.environ["APP_PASSWORD"] = ""
os.environ.setdefault("QBO_CLIENT_ID", "test-client")
os.environ.setdefault("QBO_CLIENT_SECRET", "test-secret")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)
