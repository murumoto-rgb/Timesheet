"""Fail-closed preview and no-database health checks."""
import asyncio
import json
import pytest
from fastapi import HTTPException
from starlette.requests import Request
import main
async def http_call(method, payload, path):
    events, sent = [], False
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")], "server": ("localhost", 8000), "client": ("127.0.0.1", 1234)}
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        await asyncio.Event().wait()
    async def send(message): events.append(message)
    await main.app(scope, receive, send)
    status = next(e["status"] for e in events if e["type"] == "http.response.start")
    return status, json.loads(b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body"))


def test_health_does_not_wake_database(monkeypatch):
    monkeypatch.setattr(main, "_postgres", lambda: pytest.fail("health must not query Neon"))
    assert main.health()["ok"]


@pytest.mark.parametrize("method,path", [("POST", "/api/timeactivity"), ("PUT", "/api/timeactivity/1"), ("DELETE", "/api/timeactivity/1"), ("GET", "/connect"), ("GET", "/callback"), ("POST", "/api/push/test"), ("GET", "/api/cron/reminders")])
def test_preview_blocks_live_side_effects(monkeypatch, method, path):
    monkeypatch.setattr(main, "PREVIEW_READ_ONLY", True)
    monkeypatch.setattr(main, "HOSTED", False)
    status, data = asyncio.run(http_call(method, {}, path=path))
    assert status == 403
    assert "Preview" in data["detail"]


def test_cron_requires_secret(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        main.reminder_cron(Request({"type": "http", "headers": []}))
    assert exc.value.status_code == 401


def test_production_refresh_disabled_in_preview(monkeypatch):
    monkeypatch.setattr(main, "PREVIEW_READ_ONLY", True)
    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    with pytest.raises(HTTPException) as exc:
        main._token_request({"grant_type": "refresh_token"})
    assert exc.value.status_code == 403


def test_valid_production_token_cannot_bypass_preview(monkeypatch):
    monkeypatch.setattr(main, "PREVIEW_READ_ONLY", True)
    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    monkeypatch.setattr(main, "_load_tokens", lambda: pytest.fail("must reject before loading live credentials"))
    with pytest.raises(HTTPException) as exc:
        main.get_access_token()
    assert exc.value.status_code == 403


@pytest.mark.parametrize("environment,activated,locked", [("preview", "", True), ("production", "", True), ("production", "1", False)])
def test_vercel_config_defaults_fail_closed(environment, activated, locked):
    import subprocess, sys, os
    env = dict(os.environ, VERCEL="1", VERCEL_ENV=environment, TIMESHEET_PRODUCTION_ACTIVATED=activated)
    for name in ("TIMESHEET_PREVIEW_READ_ONLY", "APP_PASSWORD", "DATABASE_URL"):
        env.pop(name, None)
    result = subprocess.run([sys.executable, "-c", "import main; import json; print(json.dumps([main.PREVIEW_READ_ONLY, main.HOSTED, main.APP_PASSWORD]))"], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [locked, True, ""]
