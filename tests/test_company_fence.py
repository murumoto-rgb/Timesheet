"""A stale tab must never mutate overlapping IDs in another QBO company."""
import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import main


def body(company="company-a", **extra):
    return {"company_key": main._company_key(company), "operation_id": str(uuid.uuid4()),
            "employee_id": "55", "item_id": "5", "customer_id": "10", "hours": 1,
            "txn_date": "2026-08-30", "sync_token": "3", **extra}


class Request:
    client = None
    def __init__(self, payload): self.payload = payload
    async def json(self): return self.payload


def call(method, payload):
    if method == "DELETE": return asyncio.run(main.delete_time("77", Request(payload)))
    entry = main.TimeEntry(**payload)
    if method == "POST": return main.create_time(entry, None)
    return main.update_time("77", entry, None)


async def http_call(method, payload):
    """Exercise the actual ASGI HTTP route without another test dependency."""
    events, sent = [], False
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "method": method, "path": "/api/timeactivity" + ("/77" if method != "POST" else ""),
             "raw_path": b"/api/timeactivity", "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json")], "server": ("localhost", 8000), "client": ("127.0.0.1", 1234)}
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        await asyncio.Event().wait()
    async def send(message): events.append(message)
    await main.app(scope, receive, send)
    status = next(e["status"] for e in events if e["type"] == "http.response.start")
    data = json.loads(b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body"))
    return status, data


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize("missing", [False, True])
def test_stale_tab_and_missing_company_rejected_before_claim_or_qbo(monkeypatch, method, missing):
    # Both companies intentionally use the same entry/person/customer IDs.
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "company-b"})
    monkeypatch.setattr(main, "_journaled_qbo_write", lambda *a, **k: pytest.fail("must reject before journal claim"))
    monkeypatch.setattr(main, "_read_timeactivity", lambda *a, **k: pytest.fail("must reject before QBO read"))
    payload = body()
    if missing: payload.pop("company_key")
    with pytest.raises(HTTPException) as exc: call(method, payload)
    assert exc.value.status_code == (428 if missing else 409)
    assert exc.value.detail["code"] == ("COMPANY_REQUIRED" if missing else "COMPANY_CHANGED")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize("missing", [False, True])
def test_http_contract_requires_the_displayed_company(monkeypatch, method, missing):
    monkeypatch.setattr(main, "HOSTED", False)
    monkeypatch.setattr(main, "APP_PASSWORD", "")
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "company-b"})
    payload = body()
    if missing: payload.pop("company_key")
    status, data = asyncio.run(http_call(method, payload))
    assert status == (428 if missing else 409)
    assert data["detail"]["code"] == ("COMPANY_REQUIRED" if missing else "COMPANY_CHANGED")


def test_oauth_replacement_waits_for_original_company_write_and_audit_keeps_scope(monkeypatch):
    current = {"realm_id": "company-a"}
    entered, release, switching, switched = (threading.Event() for _ in range(4))
    scopes = []
    monkeypatch.setattr(main, "_load_tokens", lambda: current.copy())
    monkeypatch.setattr(main, "_save_blob", lambda _path, _id, data: (current.clear(), current.update(data)))
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [])
    monkeypatch.setattr(main, "_audit", lambda *a, **k: scopes.append(k["scope"]))
    def post(payload, params=None):
        assert params["_expected_scope"] == f"{main.API_BASE}|company-a"
        entered.set()
        assert release.wait(5)
        assert current["realm_id"] == "company-a"
        return {"Id": "77", **payload}
    monkeypatch.setattr(main, "_post_timeactivity", post)
    def reconnect():
        switching.set()
        main._save_tokens({"realm_id": "company-b"})
        switched.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(call, "POST", body())
        assert entered.wait(5)
        connecting = pool.submit(reconnect)
        assert switching.wait(5)
        assert not switched.wait(.05)
        release.set()
        assert saving.result(5)["Id"] == "77"
        connecting.result(5)
    assert current["realm_id"] == "company-b"
    assert scopes == [f"{main.API_BASE}|company-a"]
    assert main._load_operations()["operations"][0]["scope"] == scopes[0]


def test_refresh_inside_company_fence_is_reentrant_and_keeps_realm(monkeypatch):
    current = {"realm_id": "company-a", "access_token": "expired", "refresh_token": "refresh", "access_expires_at": 0}
    monkeypatch.setattr(main, "_load_tokens", lambda: current.copy())
    monkeypatch.setattr(main, "_save_blob", lambda _path, _id, data: (current.clear(), current.update(data)))
    monkeypatch.setattr(main, "_token_request", lambda data: {"access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600})
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [])
    monkeypatch.setattr(main, "_audit", lambda *a, **k: True)
    class Response:
        status_code = 200
        def json(self): return {"TimeActivity": {"Id": "77"}}
    def post(url, **kwargs):
        assert "/company/company-a/timeactivity" in url
        assert kwargs["headers"]["Authorization"] == "Bearer fresh"
        assert "_expected_scope" not in kwargs["params"]
        return Response()
    monkeypatch.setattr(main.requests, "post", post)
    assert call("POST", body())["Id"] == "77"
    assert current["refresh_token"] == "rotated"


def test_only_durable_terminal_failure_returns_a_matching_rejection_receipt(monkeypatch):
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "company-a"})
    reads = []
    monkeypatch.setattr(main, "_read_timeactivity", lambda entry_id: (reads.append(entry_id) or {"Id": "77", "SyncToken": "3", "BillableStatus": "HasBeenBilled"}))
    payload = body()
    for _ in range(2):
        with pytest.raises(HTTPException) as failed: call("PUT", payload)
        assert failed.value.status_code == 409
        assert failed.value.detail["operationId"] == payload["operation_id"]
        assert failed.value.detail["operationStatus"] == "failed"
    assert reads == ["77"], "a failed operation replays its stored rejection"
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "company-b"})
    with pytest.raises(HTTPException) as changed: call("PUT", payload)
    assert changed.value.detail["code"] == "COMPANY_CHANGED"
    assert "operationStatus" not in changed.value.detail, "pre-claim rejection does not certify the old outcome"


@pytest.mark.parametrize("workers", ["0", "2", "4", "invalid", ""])
def test_startup_refuses_unsupported_worker_counts(monkeypatch, workers):
    monkeypatch.setenv("WEB_CONCURRENCY", workers)
    monkeypatch.setattr(main, "HOSTED", False)
    async def startup():
        async with main._lifespan(main.app): pytest.fail("unsupported startup must not yield")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY must be 1"):
        asyncio.run(startup())
